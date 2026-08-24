"""Notebook document store — a standard .ipynb on disk, edited by cell id.

Kept in nbformat v4 so the file stays openable in Jupyter / VS Code.
"""

import os
import pathlib
import tempfile
import threading
import time

import nbformat

from . import bus

_lock = threading.RLock()

# Marks a raw cell that is really an unfilled inline-LLM prompt. See _new_cell.
AI_ROLE = "ai-prompt"
VIS_ROLE = "vis-prompt"

# Per-cell undo, for sources replaced by something other than the user's own
# typing — Claude via `gusnb here`, or a skill's snippet. Kept in the
# cell's own metadata rather than in a process-wide stack so that undo is
# per-cell (as in JupyterLab), survives a restart, and travels with the cell if
# it moves. Jupyter ignores unknown metadata keys, so the file stays portable.
UNDO_KEY = "nb_undo"

# Deep enough to walk back an agent loop's few attempts, shallow enough that a
# long session can't bloat the .ipynb with dead source.
UNDO_DEPTH = 10

# What the user asked Claude, on a cell Claude then rewrote — the terminal's
# equivalent of the AI cell's `inline_prompt`, and shown the same way. Beside the
# undo stack rather than in a table somewhere: a cell that says who changed it
# and why should carry that with it, including into a file someone else opens.
CLAUDE_KEY = "claude_prompt"

# Long enough for a real request, short enough that a cell's metadata can't grow
# by a pasted stack trace. Truncated with an ellipsis, so a clipped prompt reads
# as clipped rather than as a differently-worded one.
PROMPT_MAX = 400


def watch(registry, interval=0.4):
    """Background thread: notify listeners when any open .ipynb changes on disk.

    Lets Claude edit a notebook with ordinary file tools and have the browser
    pick it up immediately.
    """
    def loop():
        last = {}
        while True:
            time.sleep(interval)
            for key, doc in registry.items():
                mtime = doc._disk_mtime()
                if not mtime:
                    continue
                if key in last and mtime > last[key] and mtime > doc._mtime:
                    doc.load()
                    bus.publish("notebook_changed", reason="external",
                                notebook=key)
                last[key] = mtime

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


class Registry:
    """Every open notebook, keyed by path. Documents are created on demand."""

    def __init__(self):
        self._docs = {}
        self._lock = threading.RLock()

    def get(self, path):
        key = str(path)
        with self._lock:
            doc = self._docs.get(key)
            if doc is None:
                doc = Notebook(pathlib.Path(key))
                doc.load()
                self._docs[key] = doc
            return doc

    def peek(self, path):
        return self._docs.get(str(path))

    def close(self, path):
        with self._lock:
            return self._docs.pop(str(path), None) is not None

    def items(self):
        with self._lock:
            return list(self._docs.items())

    def paths(self):
        with self._lock:
            return list(self._docs)


class Notebook:
    def __init__(self, path):
        self.path = path
        self._nb = None
        self._mtime = 0

    # --- persistence ---

    def _blank(self):
        nb = nbformat.v4.new_notebook()
        nb.cells = [nbformat.v4.new_code_cell("")]
        nb.metadata.setdefault("kernelspec", {
            "display_name": "Python (.venv)",
            "language": "python",
            "name": "python3",
        })
        return nb

    def load(self):
        with _lock:
            if self.path.exists():
                try:
                    self._nb = nbformat.read(str(self.path), as_version=4)
                    self._ensure_ids()
                except Exception:
                    self._nb = self._blank()
                self._mtime = self._disk_mtime()
            else:
                self._nb = self._blank()
                self._save()
            return self._nb

    def _disk_mtime(self):
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0

    def _ensure_ids(self):
        """A hand-edited notebook may have cells without ids — assign them."""
        seen = set()
        if not self._nb.cells:
            self._nb.cells.append(nbformat.v4.new_code_cell(""))
        for cell in self._nb.cells:
            cid = cell.get("id")
            if not cid or cid in seen:
                cell["id"] = nbformat.v4.new_code_cell()["id"]
            seen.add(cell["id"])

    def _sync(self):
        """Reload if the file changed underneath us (Claude editing it directly)."""
        if self._nb is None:
            self.load()
        elif self._disk_mtime() > self._mtime:
            self.load()
        return self._nb

    @property
    def nb(self):
        return self._sync()

    def _save(self):
        """Atomic write so a concurrent reader never sees a half-written file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = nbformat.writes(self._nb)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, str(self.path))
            self._mtime = self._disk_mtime()
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def save(self):
        with _lock:
            self._save()

    # --- interpreter (persisted in the .ipynb, so it survives restarts) ---

    def get_python(self):
        """The interpreter this notebook wants, or None for the app default."""
        with _lock:
            meta = self.nb.metadata.get("kernelspec") or {}
            return meta.get("notebook_python") or None

    def set_python(self, python, label=None, version=None):
        """Remember the interpreter in kernelspec metadata.

        `notebook_python` is our own key; the surrounding kernelspec fields stay
        Jupyter-standard so the file still opens elsewhere.
        """
        with _lock:
            nb = self.nb
            spec = dict(nb.metadata.get("kernelspec") or {})
            spec["name"] = "python3"
            spec["language"] = "python"
            spec["display_name"] = label or spec.get("display_name") or "Python"
            spec["notebook_python"] = str(python)
            nb.metadata["kernelspec"] = spec
            if version:
                info = dict(nb.metadata.get("language_info") or {})
                info["name"] = "python"
                info["version"] = version
                nb.metadata["language_info"] = info
            self._save()
            return spec

    # --- reads ---

    def to_json(self):
        with _lock:
            nb = self.nb
            return {
                "cells": [self._cell_json(c) for c in nb.cells],
                "path": str(self.path),
                "python": (nb.metadata.get("kernelspec") or {})
                          .get("notebook_python"),
            }

    @staticmethod
    def _cell_json(cell):
        meta = cell.get("metadata") or {}
        cell_type = cell.get("cell_type", "code")
        if cell_type == "raw" and meta.get("cell_role") == AI_ROLE:
            cell_type = "ai"          # a prompt awaiting generation
        elif cell_type == "raw" and meta.get("cell_role") == VIS_ROLE:
            cell_type = "vis"         # a prompt sent to an agent for HTML viz
        return {
            "id": cell.get("id"),
            "cell_type": cell_type,
            "source": cell.get("source", ""),
            "execution_count": cell.get("execution_count"),
            "outputs": [dict(o) for o in cell.get("outputs", [])],
            # What the user asked the inline LLM for, if this cell came from it.
            "prompt": meta.get("inline_prompt"),
            # What the user asked Claude, if a terminal rewrote this cell. A
            # separate field from `prompt` because they're different models doing
            # different jobs, and the strips say so — one is re-runnable, the
            # other is a record of what happened.
            "claude_prompt": meta.get(CLAUDE_KEY),
            # How many replaced sources this cell can walk back. The browser
            # shows an undo only when there's something to undo.
            "undo_depth": len(meta.get(UNDO_KEY) or []),
        }

    def find(self, cell_id):
        with _lock:
            for i, c in enumerate(self.nb.cells):
                if c.get("id") == cell_id:
                    return i, c
            return None, None

    def cell_json(self, cell_id):
        _, cell = self.find(cell_id)
        return self._cell_json(cell) if cell is not None else None

    # --- mutations ---

    def _new_cell(self, cell_type, source=""):
        if cell_type == "markdown":
            return nbformat.v4.new_markdown_cell(source)
        if cell_type == "raw":
            return nbformat.v4.new_raw_cell(source)
        if cell_type == "ai":
            # There is no "ai" cell type in nbformat, and inventing one would
            # make the file unopenable elsewhere. An unfilled AI cell is a raw
            # cell holding a prompt, flagged in metadata; once generated it
            # becomes an ordinary code cell.
            cell = nbformat.v4.new_raw_cell(source)
            cell["metadata"]["cell_role"] = AI_ROLE
            return cell
        if cell_type == "vis":
            cell = nbformat.v4.new_raw_cell(source)
            cell["metadata"]["cell_role"] = VIS_ROLE
            return cell
        return nbformat.v4.new_code_cell(source)

    def add_cell(self, cell_type="code", source="", index=None, after=None):
        with _lock:
            cell = self._new_cell(cell_type, source)
            cells = self.nb.cells
            if after is not None:
                i, _ = self.find(after)
                index = (i + 1) if i is not None else len(cells)
            if index is None or index > len(cells):
                index = len(cells)
            cells.insert(max(0, index), cell)
            self._save()
            bus.publish("notebook_changed", reason="add",
                        cell_id=cell["id"], notebook=str(self.path))
            return self._cell_json(cell)

    def update_cell(self, cell_id, source=None, cell_type=None, undoable=False):
        """Change a cell's source and/or type.

        `undoable` pushes the source being replaced onto the cell's own undo
        stack. Off by default: the browser PATCHes on every keystroke pause, and
        recording each of those would bury the one entry that matters — the
        wholesale replacement done by an agent or a snippet, which the user did
        not type and may want back.
        """
        with _lock:
            i, cell = self.find(cell_id)
            if cell is None:
                return None
            if undoable and source is not None and source != cell.get("source", ""):
                self._push_undo(cell)
            if source is not None:
                cell["source"] = source
            # Compare against the type the client sees, so asking for "ai" on a
            # cell that's already an AI prompt is a no-op rather than a rebuild.
            if cell_type is not None and cell_type != self._cell_json(cell)["cell_type"]:
                new = self._new_cell(cell_type, cell.get("source", ""))
                new["id"] = cell_id
                # Carry metadata over — a type switch shouldn't lose the
                # inline-LLM prompt or anything else attached to the cell.
                carried = dict(cell.get("metadata") or {})
                if cell_type not in ("ai", "vis"):
                    carried.pop("cell_role", None)   # no longer a pending prompt
                new["metadata"].update(carried)
                if cell_type == "ai":
                    new["metadata"]["cell_role"] = AI_ROLE
                elif cell_type == "vis":
                    new["metadata"]["cell_role"] = VIS_ROLE
                self.nb.cells[i] = new
                cell = new
            self._save()
            bus.publish("notebook_changed", reason="update",
                        cell_id=cell_id, notebook=str(self.path))
            return self._cell_json(cell)

    @staticmethod
    def _push_undo(cell):
        """Remember the source about to be overwritten. Caller holds the lock."""
        meta = cell.setdefault("metadata", {})
        history = list(meta.get(UNDO_KEY) or [])
        history.append(cell.get("source", ""))
        meta[UNDO_KEY] = history[-UNDO_DEPTH:]

    def undo_cell(self, cell_id):
        """Restore the source replaced by the last undoable change.

        One step per call, and independent per cell: undoing here doesn't touch
        what an agent did three cells down. Returns None if the cell is gone and
        the unchanged cell if there's nothing to undo, so a caller can tell "no
        such cell" from "nothing to undo" by whether `undo_depth` moved.
        """
        with _lock:
            _, cell = self.find(cell_id)
            if cell is None:
                return None
            meta = cell.setdefault("metadata", {})
            history = list(meta.get(UNDO_KEY) or [])
            if not history:
                return self._cell_json(cell)
            cell["source"] = history.pop()
            if history:
                meta[UNDO_KEY] = history
            else:
                meta.pop(UNDO_KEY, None)
            # Outputs belong to the source that produced them; keeping them
            # against restored source would show a result the code never gave.
            if cell.get("cell_type") == "code":
                cell["outputs"] = []
                cell["execution_count"] = None
            # And the caption belongs to the source just walked back. Left in
            # place it would credit a request for code that is no longer here.
            if not history:
                meta.pop(CLAUDE_KEY, None)
            self._save()
            bus.publish("notebook_changed", reason="update",
                        cell_id=cell_id, notebook=str(self.path))
            return self._cell_json(cell)

    def set_prompt(self, cell_id, prompt):
        """Remember the request that generated a cell's code.

        Kept in cell metadata so the .ipynb stays a plain notebook: Jupyter
        ignores the key, and we can show the prompt above the code it produced.
        """
        with _lock:
            _, cell = self.find(cell_id)
            if cell is None:
                return None
            meta = dict(cell.get("metadata") or {})
            if prompt:
                meta["inline_prompt"] = prompt
            else:
                meta.pop("inline_prompt", None)
            cell["metadata"] = meta
            self._save()
            bus.publish("notebook_changed", reason="update",
                        cell_id=cell_id, notebook=str(self.path))
            return self._cell_json(cell)

    def set_claude_prompt(self, cell_id, prompt):
        """Caption a cell with the request that made Claude rewrite it.

        Same shape as set_prompt() and the same reasoning — metadata, so the
        .ipynb stays a plain notebook and the caption travels with the cell — but
        a different key, because the two strips say different things: the inline
        LLM's prompt can be re-run, this one is a record of what happened.
        """
        with _lock:
            _, cell = self.find(cell_id)
            if cell is None:
                return None
            text = " ".join((prompt or "").split())      # a prompt is one line here
            if len(text) > PROMPT_MAX:
                text = text[:PROMPT_MAX - 1].rstrip() + "…"
            meta = dict(cell.get("metadata") or {})
            if text:
                meta[CLAUDE_KEY] = text
            else:
                meta.pop(CLAUDE_KEY, None)
            cell["metadata"] = meta
            self._save()
            bus.publish("notebook_changed", reason="update",
                        cell_id=cell_id, notebook=str(self.path))
            return self._cell_json(cell)

    def delete_cell(self, cell_id):
        with _lock:
            i, cell = self.find(cell_id)
            if cell is None:
                return False
            del self.nb.cells[i]
            if not self.nb.cells:
                self.nb.cells.append(self._new_cell("code"))
            self._save()
            bus.publish("notebook_changed", reason="delete",
                        cell_id=cell_id, notebook=str(self.path))
            return True

    def move_cell(self, cell_id, index):
        with _lock:
            i, cell = self.find(cell_id)
            if cell is None:
                return False
            cells = self.nb.cells
            del cells[i]
            cells.insert(max(0, min(index, len(cells))), cell)
            self._save()
            bus.publish("notebook_changed", reason="move",
                        cell_id=cell_id, notebook=str(self.path))
            return True

    def set_outputs(self, cell_id, outputs, execution_count=None):
        with _lock:
            _, cell = self.find(cell_id)
            if cell is None or cell.get("cell_type") != "code":
                return
            cell["outputs"] = [nbformat.from_dict(o) for o in outputs]
            cell["execution_count"] = execution_count
            self._save()

    def clear_outputs(self, cell_id=None):
        with _lock:
            targets = self.nb.cells if cell_id is None else [self.find(cell_id)[1]]
            for cell in targets:
                if cell is not None and cell.get("cell_type") == "code":
                    cell["outputs"] = []
                    cell["execution_count"] = None
            self._save()
            bus.publish("notebook_changed", reason="clear",
                        cell_id=cell_id, notebook=str(self.path))
            return True
