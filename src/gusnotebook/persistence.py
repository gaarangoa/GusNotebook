"""Atomic document writes that preserve permissions and detect stale saves."""

import os
import stat
import tempfile
from pathlib import Path


ANY_VERSION = object()


class ExternalChangeError(OSError):
    """The file no longer matches the revision the editor loaded."""


def disk_version(path):
    try:
        info = Path(path).stat()
        return f"{info.st_mtime_ns}:{info.st_size}:{info.st_ino}"
    except FileNotFoundError:
        return None


def atomic_write(path, text, expected_version=ANY_VERSION, mode=None):
    """Replace a document after checking its loaded revision.

    Callers serialize writes to each document. The second version check also
    catches external edits made while a large replacement is being written.
    """
    path = Path(path)

    def check_version():
        if expected_version is not ANY_VERSION and disk_version(path) != expected_version:
            raise ExternalChangeError(
                f"{path.name} changed on disk; reload it before saving")

    check_version()
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            mode = 0o600
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        check_version()
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return disk_version(path)
