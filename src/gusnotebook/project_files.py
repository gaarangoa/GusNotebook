"""Bounded, symlink-resistant file access for the project broker (never a worker)."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid


class ProjectError(ValueError):
    pass


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
        raise ProjectError("Use a name starting with a letter, with lowercase letters, digits or underscores")
    return value


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


@contextmanager
def parent_fd(path, create=False):
    path = Path(os.path.abspath(os.path.expanduser(str(path))))
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, path.name
    finally:
        os.close(fd)


def read_file(path, limit=8 * 1024 * 1024):
    with parent_fd(path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or (limit is not None and before.st_size > limit):
                raise ProjectError("Expected a regular file within the size limit")
            data = stream.read() if limit is None else stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if ((limit is not None and len(data) > limit) or
                    signature(before) != signature(after) or signature(after) != signature(current)):
                raise ProjectError("File changed while reading; try again")
            return data


def write_file(path, data):
    if isinstance(data, str):
        data = data.encode()
    with parent_fd(path, create=True) as (parent, name):
        temporary = ".gusnb-" + uuid.uuid4().hex
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # Replaces a symlink itself, never its target.
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
