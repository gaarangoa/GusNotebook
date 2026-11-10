"""Notebook formats shared by file discovery, storage, and routing."""

from pathlib import Path

NOTEBOOK_SUFFIXES = {".ipynb", ".gspy"}


def notebook_format(path):
    suffix = Path(path).suffix.lower()
    return suffix[1:] if suffix in NOTEBOOK_SUFFIXES else None


def same_notebook_format(source, destination):
    """Renaming a file is not a format conversion."""
    before, after = notebook_format(source), notebook_format(destination)
    if before != after and (before or after):
        raise ValueError("Keep the notebook's file extension when renaming or copying")
