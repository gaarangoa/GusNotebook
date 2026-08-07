"""Filesystem listing for the file browser.

Navigation is unrestricted — you can walk up out of the project like Jupyter's
file browser lets you. The only writes are the explicit ones: `new_path`
validates a name the user typed before the caller creates a file or folder.
"""

import os
from pathlib import Path

# Files we can open in the notebook pane vs. ones we just show.
NOTEBOOK_SUFFIX = ".ipynb"

# Directories that are never interesting to browse into.
SKIP_DIRS = {".git", "__pycache__", ".ipynb_checkpoints", "node_modules"}


def normalize(path):
    """Realpath the parent but keep the filename, so paths compare equal to
    listing entries without requiring the file to exist."""
    p = Path(path).expanduser()
    try:
        return Path(os.path.realpath(p.parent)) / p.name
    except OSError:
        return p


def _kind(entry_path, is_dir):
    if is_dir:
        return "dir"
    if entry_path.suffix == NOTEBOOK_SUFFIX:
        return "notebook"
    return "file"


def listdir(path, show_hidden=False):
    """One directory level: {path, parent, entries[]}.

    Raises ValueError if the path isn't a readable directory.
    """
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError as e:
        raise ValueError(f"cannot resolve {path}: {e}")

    if not p.exists():
        raise ValueError(f"no such directory: {p}")
    if not p.is_dir():
        raise ValueError(f"not a directory: {p}")

    entries = []
    try:
        with os.scandir(p) as it:
            for e in it:
                if e.name.startswith(".") and not show_hidden:
                    continue
                try:
                    is_dir = e.is_dir()
                except OSError:
                    continue
                if is_dir and e.name in SKIP_DIRS:
                    continue
                ep = Path(e.path)
                try:
                    st = e.stat()
                    size, mtime = (None if is_dir else st.st_size), st.st_mtime
                except OSError:
                    size, mtime = None, 0
                entries.append({
                    "name": e.name,
                    "path": str(ep),
                    "kind": _kind(ep, is_dir),
                    "size": size,
                    "mtime": mtime,
                })
    except PermissionError:
        raise ValueError(f"permission denied: {p}")

    # Directories first, then files; case-insensitive within each group.
    entries.sort(key=lambda d: (d["kind"] != "dir", d["name"].lower()))

    return {
        "path": str(p),
        "parent": str(p.parent) if p.parent != p else None,
        "home": str(Path.home()),
        "entries": entries,
    }


def new_path(directory, name):
    """Where a "New file"/"New folder" named `name` should go.

    Names are single path components: a typed name is a name, not a way to
    reach another directory, so a "/" or ".." in it is a mistake worth
    reporting rather than silently resolving.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("a name is required")
    if "/" in name or name in (".", ".."):
        raise ValueError("name cannot contain '/' — pick a plain file name")

    parent = Path(directory).expanduser()
    try:
        parent = parent.resolve()
    except OSError as e:
        raise ValueError(f"cannot resolve {directory}: {e}")
    if not parent.is_dir():
        raise ValueError(f"no such directory: {parent}")

    target = parent / name
    if target.exists():
        raise ValueError(f"{name} already exists")
    return target


def resolve_notebook(path):
    """Validate a path the browser wants to open as a notebook.

    Resolved so it compares equal to the paths `listdir` returns — on macOS
    /tmp is a symlink to /private/tmp, and an unresolved path would never match.
    """
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ValueError("notebook path must be absolute")
    if p.suffix != NOTEBOOK_SUFFIX:
        raise ValueError(f"not a notebook: {p.name}")
    p = normalize(p)
    if p.exists() and not p.is_file():
        raise ValueError(f"not a file: {p}")
    if not p.parent.is_dir():
        raise ValueError(f"no such directory: {p.parent}")
    return p
