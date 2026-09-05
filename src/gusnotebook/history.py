"""Persistent, grouped snapshots of open documents around agent requests.

This records changes since a request, not proof of which process wrote them.
Only open notebook/text tabs are included; execution state is never restored.
"""

import difflib
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

from .persistence import ExternalChangeError, atomic_write, disk_version
from .textfile import kind_of

MAX_GROUPS = 20
MAX_GROUP_BYTES = 10 * 1024 * 1024


class History:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self.groups = []
        for path in self.directory.glob("*.json"):
            try:
                group = json.loads(path.read_text())
                if group.get("active"):
                    self._capture(group)
                    group.update(active=False, interrupted=True)
                    self._save(group)
                self.groups.append(group)
            except (OSError, ValueError, KeyError):
                continue
        self.groups.sort(key=lambda g: g["created"])
        self._prune()

    @staticmethod
    def _read(path):
        path = Path(path)
        if not path.exists():
            return None
        if path.stat().st_size > MAX_GROUP_BYTES:
            raise ValueError(f"{path.name} is too large to record")
        before = disk_version(path)
        text = path.read_text(encoding="utf-8")
        if disk_version(path) != before:
            raise ExternalChangeError(f"{path.name} changed while being recorded")
        return text

    def _save(self, group):
        atomic_write(self.directory / (group["id"] + ".json"), json.dumps(group), mode=0o600)

    def _prune(self):
        while len(self.groups) > MAX_GROUPS:
            group = self.groups[0]
            (self.directory / (group["id"] + ".json")).unlink(missing_ok=True)
            self.groups.remove(group)

    def begin(self, session, terminal, prompt, files):
        with self._lock:
            for group in self.groups:
                if group.get("active") and group["session"] == session and group["terminal"] == terminal:
                    self.finish(group["id"], session)
            documents, skipped, total = {}, [], 0
            for raw in dict.fromkeys(files):
                path = Path(raw)
                if kind_of(path) not in {"notebook", "text"} or path.name.startswith("."):
                    continue
                try:
                    source = self._read(path)
                    if source is None:
                        continue
                    size = len(source.encode("utf-8"))
                    if total + size > MAX_GROUP_BYTES:
                        skipped.append(str(path))
                        continue
                    total += size
                    documents[str(path)] = {"before": source, "after": source}
                except (OSError, ValueError):
                    skipped.append(str(path))
            group = {"id": uuid.uuid4().hex, "created": time.time(), "session": session,
                     "terminal": terminal, "prompt": (prompt or "Recorded changes")[:2000],
                     "active": True, "undone": False, "documents": documents, "skipped": skipped}
            self.groups.append(group)
            self._save(group)
            self._prune()
            return group["id"]

    def _capture(self, group):
        total = 0
        for path, document in group["documents"].items():
            try:
                content = self._read(path)
                total += len((content or "").encode("utf-8"))
                if total > MAX_GROUP_BYTES:
                    raise ValueError("Recorded documents exceed the 10 MB recording limit")
                document["after"] = content
                document.pop("error", None)
            except (OSError, ValueError) as exc:
                document["error"] = str(exc)

    def _get(self, group_id, session):
        for group in self.groups:
            if group["id"] == group_id and group["session"] == session:
                return group
        raise ValueError("No such change group in this workspace")

    def finish(self, group_id, session):
        with self._lock:
            group = self._get(group_id, session)
            if group["active"]:
                self._capture(group)
                group["active"] = False
                self._save(group)
            return self._view(group)

    @staticmethod
    def _diff_text(path, source):
        if source is None:
            return ""
        if path.endswith(".ipynb"):
            try:
                notebook = json.loads(source)
                parts = []
                for index, cell in enumerate(notebook["cells"]):
                    code = cell.get("source", "")
                    if isinstance(code, list):
                        code = "".join(code)
                    parts.append(f"Cell {index + 1} [{cell.get('cell_type', 'code')}]\n{code}\n")
                return "\n".join(parts)
            except (ValueError, KeyError, TypeError):
                pass
        return source

    @staticmethod
    def _revision(group):
        return hashlib.sha256(json.dumps(group["documents"], sort_keys=True).encode()).hexdigest()

    def _view(self, group):
        changes = []
        for path, document in group["documents"].items():
            if document["before"] == document["after"] and not document.get("error"):
                continue
            before = self._diff_text(path, document["before"])
            after = self._diff_text(path, document["after"])
            diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                               fromfile=Path(path).name + " (before)",
                                               tofile=Path(path).name + " (after)"))
            changes.append({"path": path, "diff": diff[:100000],
                            "truncated": len(diff) > 100000, "error": document.get("error"),
                            "metadata_only": not diff})
        return {**{key: value for key, value in group.items() if key != "documents"},
                "changes": changes, "revision": self._revision(group)}

    def list(self, session):
        with self._lock:
            result = []
            for group in reversed(self.groups):
                if group["session"] != session:
                    continue
                if group["active"]:
                    self._capture(group)
                    self._save(group)
                result.append(self._view(group))
            return result

    def undo(self, group_id, session, revision):
        with self._lock:
            group = self._get(group_id, session)
            if group["active"]:
                raise ValueError("Finish recording before undoing a group")
            if group["undone"]:
                raise ValueError("This group has already been undone")
            if revision != self._revision(group):
                raise ExternalChangeError("The recorded changes changed; review them again")
            planned = []
            # Validate every document before touching any of them.
            for path, document in group["documents"].items():
                if document["before"] == document["after"] and not document.get("error"):
                    continue
                version = disk_version(path)
                current = self._read(path)
                if document.get("error") or current != document["after"]:
                    raise ExternalChangeError(f"{Path(path).name} changed after recording; nothing was restored")
                if disk_version(path) != version:
                    raise ExternalChangeError(f"{Path(path).name} is changing; nothing was restored")
                planned.append((path, document, version))
            written = []
            try:
                for path, document, version in planned:
                    new_version = atomic_write(path, document["before"], version)
                    written.append((path, document, new_version))
            except OSError as original:
                failed = []
                for path, document, version in reversed(written):
                    try:
                        if document["after"] is not None:
                            atomic_write(path, document["after"], version)
                        elif disk_version(path) == version:
                            Path(path).unlink()
                        else:
                            raise ExternalChangeError("File changed during rollback")
                    except OSError:
                        failed.append(path)
                if failed:
                    raise OSError("Restore was interrupted. Check these files before retrying: " +
                                  ", ".join(failed)) from original
                raise
            group["undone"] = True
            self._save(group)
            return [path for path, _document, _version in planned]

    def close(self):
        with self._lock:
            for group in self.groups:
                if group["active"]:
                    self.finish(group["id"], group["session"])
