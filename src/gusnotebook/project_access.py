"""Human-maintained input policy for project discovery and broker operations.

This is not an OS sandbox: terminal commands and arbitrary Python retain host
permissions. Never describe these checks as filesystem isolation.
"""

import os
from pathlib import Path
import stat

import yaml

from .project_files import ProjectError, digest, parent_fd, read_file, write_file

RELATIVE_PATH = 'data/access.yaml'
TEMPLATE = '''# Edit this file yourself to approve input data for this project.
# Agents must never edit it or expand the list on your behalf.
# Empty lists mean no approved raw inputs. Paths may be absolute or relative
# to the project root. Directories include their descendants; symlinks are not followed.
# This is a data-use policy with app checks, NOT a terminal/Python filesystem sandbox.
version: 1
files: []
directories: []

# Example (replace the empty lists above):
# files:
#   - /path/to/measurements.csv
# directories:
#   - /path/to/approved-study-data
'''


class PolicyLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ProjectError('YAML aliases are not supported in data/access.yaml')
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ProjectError('Use unique text keys in data/access.yaml')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def ensure(project):
    if not Path(project['path']).is_dir():
        raise ProjectError('Project folder is missing')
    path = Path(project['path']) / RELATIVE_PATH
    # Do not replace an existing policy, including a broken symlink.
    if not os.path.lexists(path):
        write_file(path, TEMPLATE)


def normalize(root, value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096 or '\x00' in value:
        raise ProjectError('Input paths must be nonempty text under 4096 characters')
    if any(char in value for char in '*?[]'):
        raise ProjectError('Use exact file or directory paths, not wildcard patterns')
    path = Path(value).expanduser()
    return Path(os.path.abspath(path if path.is_absolute() else root / path))


def load(project):
    root = Path(project['path'])
    try:
        raw = read_file(root / RELATIVE_PATH, 64 * 1024)
        policy = yaml.load(raw, Loader=PolicyLoader)
    except (OSError, ValueError, yaml.YAMLError, RecursionError) as exc:
        raise ProjectError('Cannot read data/access.yaml; no input access is approved: ' + str(exc)) from exc
    if (not isinstance(policy, dict) or set(policy) != {'version', 'files', 'directories'}
            or type(policy['version']) is not int or policy['version'] != 1):
        raise ProjectError('data/access.yaml requires version: 1, files: [], and directories: [] only')
    result = dict(path=str(root / RELATIVE_PATH), revision=digest(raw), filesystem_isolation=False)
    for kind in ('files', 'directories'):
        values = policy[kind]
        if not isinstance(values, list) or len(values) > 500:
            raise ProjectError(kind + ' must be a list of at most 500 paths in data/access.yaml')
        result[kind] = list(dict.fromkeys(str(normalize(root, value)) for value in values))
    result['status'] = 'configured' if result['files'] or result['directories'] else 'empty'
    return result


def status(project):
    try:
        return load(project)
    except ProjectError as exc:
        return dict(path=str(Path(project['path']) / RELATIVE_PATH), status='invalid', error=str(exc),
                    files=[], directories=[], filesystem_isolation=False)


def allows(policy, path):
    path = Path(path)
    return str(path) in policy['files'] or any(path.is_relative_to(Path(folder)) for folder in policy['directories'])


def require(project, path, policy=None):
    policy = load(project) if policy is None else policy
    path = normalize(Path(project['path']), path)
    if not allows(policy, path):
        raise ProjectError('Input is not approved in data/access.yaml: ' + str(path) +
                           '. Ask the user to edit the allowlist; do not search elsewhere.')
    return path


def discover(project, path=None, offset=0, limit=100):
    policy = load(project)
    if path is None:
        return policy
    try:
        offset, limit = int(offset), int(limit)
    except (TypeError, ValueError) as exc:
        raise ProjectError('Input listing offset and limit must be integers') from exc
    if not 0 <= offset <= 100000 or not 1 <= limit <= 200:
        raise ProjectError('Use offset 0–100000 and limit 1–200')
    path = require(project, path, policy)
    entries = []
    more = False
    with parent_fd(path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if stat.S_ISREG(info.st_mode):
                entries.append(dict(path=str(path), kind='file', size=info.st_size))
            elif stat.S_ISDIR(info.st_mode):
                # An entry under files must not implicitly authorize directory contents.
                if not any(path.is_relative_to(Path(folder)) for folder in policy['directories']):
                    raise ProjectError('List this path under directories in data/access.yaml')
                with os.scandir(fd) as children:
                    scanned = 0
                    for child in children:
                        if scanned < offset:
                            scanned += 1
                            continue
                        if scanned >= offset + limit:
                            more = True
                            break
                        scanned += 1
                        info = child.stat(follow_symlinks=False)
                        if stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode):
                            entries.append(dict(path=str(path / child.name),
                                                kind='directory' if stat.S_ISDIR(info.st_mode) else 'file',
                                                size=info.st_size if stat.S_ISREG(info.st_mode) else None))
            else:
                raise ProjectError('Approved input must be a regular file or directory')
        finally:
            os.close(fd)
    return dict(policy=policy, path=str(path), entries=entries,
                next_offset=offset + limit if more else None)
