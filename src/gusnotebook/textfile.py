"""Plain-text documents for non-notebook tabs (.py, .csv, .md, ...).

Text tabs are a simple read/save editor — no kernel involved. Files that aren't
decodable text, or are very large, are reported as such instead of being loaded.
"""

import os
import tempfile
import threading
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024      # refuse to open more than 2 MB in a textarea

# Only these open as editable text; anything else is described, not loaded.
MARKUP_SUFFIXES = {".html", ".htm", ".svg"}

TEXT_SUFFIXES = {
    ".py", ".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml",
    ".cfg", ".ini", ".sh", ".bash", ".zsh", ".sql", ".css", ".js",
    ".ts", ".jsx", ".tsx", ".xml", ".log", ".env", ".gitignore", ".r", ".R",
    ".rst", ".tex", ".c", ".h", ".cpp", ".java", ".go", ".rs", ".rb", ".pl",
} | MARKUP_SUFFIXES

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

_lock = threading.RLock()
_NO_EXPECTATION = object()


class ExternalChangeError(OSError):
    """The file no longer matches the disk revision the editor loaded."""


def disk_version(path):
    """Cheap identity for conflict detection, including atomic replacements."""
    try:
        stat = Path(path).stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}:{stat.st_ino}"
    except OSError:
        return None


def kind_of(path):
    p = Path(path)
    suf = p.suffix.lower()
    if suf == ".ipynb":
        return "notebook"
    if suf in IMAGE_SUFFIXES:
        return "image"
    if suf in TEXT_SUFFIXES or p.name.startswith("."):
        return "text"
    return "unknown"


class TextFile:
    """One text document. Mirrors Notebook's mtime-based external-edit check."""

    def __init__(self, path):
        self.path = Path(path)
        self._text = None
        self._version = None

    def disk_version(self):
        return disk_version(self.path)

    def load(self):
        with _lock:
            if not self.path.exists():
                self._text = ""
                self._version = None
                return self._text
            # Check binary-ness before size: "this is binary" is a more useful
            # message than "too large" for a 3 MB .bin.
            with open(self.path, "rb") as f:
                if b"\0" in f.read(8192):
                    raise ValueError(f"{self.path.name} is a binary file")

            size = self.path.stat().st_size
            if size > MAX_BYTES:
                raise ValueError(
                    f"{self.path.name} is {size // 1024} KB — too large to edit here")
            try:
                self._text = self.path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raise ValueError(f"{self.path.name} is not UTF-8 text")
            self._version = self.disk_version()
            return self._text

    def to_json(self):
        with _lock:
            if self._text is None or self.disk_version() != self._version:
                self.load()
            data = {
                "path": str(self.path),
                "kind": "text",
                "text": self._text,
                "language": self.path.suffix.lstrip(".").lower(),
                "readonly": False,
                "disk_version": self._version,
            }
            return data

    def save(self, text, expected_version=_NO_EXPECTATION):
        """Atomic write, matching Notebook._save."""
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        size = len(text.encode("utf-8"))
        if size > MAX_BYTES:
            raise ValueError(
                f"edited document is {size // 1024} KB — maximum is "
                f"{MAX_BYTES // 1024} KB")
        with _lock:
            actual = self.disk_version()
            if expected_version is not _NO_EXPECTATION and actual != expected_version:
                raise ExternalChangeError(
                    f"{self.path.name} changed on disk; reload it before saving")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(tmp, str(self.path))
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            self._text = text
            self._version = self.disk_version()
            return {"path": str(self.path), "saved": True,
                    "disk_version": self._version}


class TextRegistry:
    def __init__(self):
        self._docs = {}
        self._lock = threading.RLock()

    def get(self, path):
        """The document for `path`, loading it on first use.

        A file that won't load (binary, too big) is not retained — otherwise it
        would linger in the open-tab list and come back on the next reload.
        """
        key = str(path)
        with self._lock:
            doc = self._docs.get(key)
            if doc is not None:
                return doc
            doc = TextFile(key)
            doc.load()
            self._docs[key] = doc
            return doc

    def close(self, path):
        with self._lock:
            return self._docs.pop(str(path), None) is not None

    def paths(self):
        with self._lock:
            return list(self._docs)
