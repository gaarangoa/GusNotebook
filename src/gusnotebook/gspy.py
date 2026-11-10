"""GSPY v1 storage: ordered nbformat cells and a portable document journal.

Cell-history metadata is an in-memory inbox for the shared notebook operations.
On commit it is drained into the document journal. The journal is the only
persisted history; deleted cells and previous output versions remain addressable.
"""

from copy import deepcopy
import hashlib
import json
import time
import uuid

import nbformat

from . import cellhistory
from .notebook import Notebook, NotebookReadError
from .persistence import ExternalChangeError, atomic_write, disk_version
from .gspy_content import ContentNotebook, definition, validate_definition

KEY = "gusnotebook"
SCHEMA_VERSION = 1
LABELS = {**cellhistory.LABELS, "document_created": "Scientific notebook created",
          "delete": "Block deleted", "reorder": "Blocks reordered",
          "restore": "Block restored", "metadata": "Notebook settings changed",
          "duplicate": "Scientific notebook duplicated"}


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def metadata(nb):
    return nb.metadata[KEY]


def validate(nb):
    """Reject damaged/unknown schemas without normalizing away evidence."""
    meta = nb.get("metadata", {}).get(KEY)
    if not isinstance(meta, dict) or meta.get("format") != "gspy":
        raise ValueError("Missing GSPY format definition; changing an extension is not a conversion")
    if type(meta.get("schema_version")) is not int or meta["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"Unsupported GSPY schema version: {meta.get('schema_version')}")
    if not isinstance(meta.get("document_id"), str) or not meta["document_id"]:
        raise ValueError("Missing GSPY document ID")
    if not isinstance(meta.get("journal"), list):
        raise ValueError("Invalid GSPY journal")
    for name in ("revisions", "artifacts"):
        if not isinstance(meta.get(name), dict):
            raise ValueError(f"Invalid GSPY {name}")
        for key, value in meta[name].items():
            if digest(value) != key:
                raise ValueError(f"Damaged GSPY {name} record: {key}")
    for revision in meta["revisions"].values():
        if (not isinstance(revision, dict) or not isinstance(revision.get("id"), str)
                or not isinstance(revision.get("source"), str)
                or revision.get("cell_type") not in {"code", "markdown", "raw"}):
            raise ValueError("Invalid block revision")
        if revision.get("outputs_ref") is not None and revision["outputs_ref"] not in meta["artifacts"]:
            raise ValueError("Block revision references missing outputs")
        cell = deepcopy(revision)
        if "outputs_ref" in cell:
            cell["outputs"] = meta["artifacts"][cell.pop("outputs_ref")]
        nbformat.validate(cell, ref=cell["cell_type"] + "_cell", version=4, version_minor=5)
    ids = set()
    for seq, event in enumerate(meta["journal"], 1):
        if (not isinstance(event, dict) or not isinstance(event.get("id"), str)
                or not event["id"] or event["id"] in ids
                or type(event.get("seq")) is not int or event["seq"] != seq
                or not isinstance(event.get("kind"), str)
                or not isinstance(event.get("document_id"), str)):
            raise ValueError("Invalid GSPY event identity or sequence")
        ids.add(event["id"])
        for name in ("before_revision", "after_revision", "executed_revision"):
            if name in event and event[name] not in meta["revisions"]:
                raise ValueError("History references a missing block revision")
    cell_ids = [c.get("id") for c in nb.get("cells", [])]
    if any(not isinstance(cid, str) or not cid for cid in cell_ids) or len(set(cell_ids)) != len(cell_ids):
        raise ValueError("GSPY blocks require unique stable IDs")
    for cell in nb.get("cells", []):
        if "gusnotebook_content" in cell.get("metadata", {}):
            validate_definition(cell["metadata"]["gusnotebook_content"])
    for event in meta["journal"]:
        if event["kind"] == "run_started" or (event.get("run_id") and event["kind"] == "run"):
            if (not isinstance(event.get("run_id"), str) or not event["run_id"]
                    or not isinstance(event.get("cell_id"), str)
                    or not isinstance(event.get("kernel_generation"), str)
                    or not isinstance(event.get("upstream"), dict)
                    or type(event.get("prerequisites_valid")) is not bool
                    or event.get("executed_revision") not in meta["revisions"]):
                raise ValueError("Invalid captured run identity or execution context")
            revision = meta["revisions"][event["executed_revision"]]
            if event.get("signature") != digest({"source": revision["source"], "results": definition(revision)["results"]}):
                raise ValueError("Run does not match its producing revision")
            if event["kind"] == "run":
                if event.get("outputs_ref") not in meta["artifacts"]:
                    raise ValueError("Run references missing outputs")
                if (event.get("status") not in {"success", "error", "cancelled", "unknown"}
                        or not isinstance(event.get("values"), dict)
                        or not isinstance(event.get("capture_errors"), dict)):
                    raise ValueError("Invalid captured result")
                encode(event["values"])
    nbformat.validate(nb)


def snapshot(meta, cell):
    """Store a complete block revision, sharing identical output bundles."""
    state = deepcopy(dict(cell))
    state.setdefault("metadata", {}).pop(cellhistory.KEY, None)
    if "outputs" in state:
        outputs = state.pop("outputs")
        output_id = digest(outputs)
        meta["artifacts"].setdefault(output_id, outputs)
        state["outputs_ref"] = output_id
    revision_id = digest(state)
    meta["revisions"].setdefault(revision_id, state)
    return revision_id


def materialize(meta, revision_id):
    state = deepcopy(meta["revisions"][revision_id])
    if "outputs_ref" in state:
        state["outputs"] = deepcopy(meta["artifacts"][state.pop("outputs_ref")])
    state.setdefault("metadata", {})[cellhistory.KEY] = []
    return nbformat.from_dict(state)


def append(meta, event):
    event = deepcopy(event)
    # A request can arrive via both a prompt hook and an edit.
    if event.get("kind") == "request" and event.get("request_id") and any(
            e.get("kind") == "request" and e.get("cell_id") == event.get("cell_id")
            and e.get("request_id") == event["request_id"] for e in meta["journal"]):
        return
    if any(e["id"] == event.get("id") for e in meta["journal"]):
        return
    event.setdefault("id", uuid.uuid4().hex)
    event.setdefault("at", time.time())
    event.setdefault("actor", "GusNotebook")
    event.setdefault("summary", LABELS.get(event["kind"], event["kind"]))
    event["seq"] = len(meta["journal"]) + 1
    event["document_id"] = meta["document_id"]
    meta["journal"].append(event)


def reconcile(previous, current, actor="GusNotebook", context=None):
    """Commit pending events and observed state changes to one journal."""
    meta = metadata(current)
    context = context or {}
    if previous is not None:
        known = metadata(previous)
        if meta["document_id"] != known["document_id"]:
            raise ValueError("Document identity changed on disk; reopen the file explicitly")
        # An external editor/workspace undo may restore an older document.
        # Retain known history and artifacts, rejecting conflicting event IDs.
        for name in ("revisions", "artifacts"):
            meta[name] = {**deepcopy(known[name]), **meta[name]}
        old_events = {e["id"]: e for e in known["journal"]}
        incoming = meta["journal"]
        for event in incoming:
            if event["id"] in old_events and event != old_events[event["id"]]:
                raise ValueError("An existing history event was modified on disk")
        meta["journal"] = deepcopy(known["journal"])
        for event in incoming:
            if event["id"] not in old_events:
                append(meta, event)
    else:
        append(meta, {"kind": "document_created", "actor": actor})

    old = {c["id"]: c for c in previous.cells} if previous is not None else {}
    new = {c["id"]: c for c in current.cells}
    for cell_id in dict.fromkeys([*old, *new]):
        before, after = old.get(cell_id), new.get(cell_id)
        refs = {}
        if before is not None:
            refs["before_revision"] = snapshot(meta, before)
        if after is not None:
            refs["after_revision"] = snapshot(meta, after)
            refs["index"] = list(new).index(cell_id)
        pending = (after.get("metadata", {}).get(cellhistory.KEY) or []) if after is not None else []
        pending = deepcopy(pending)
        if after is not None:
            after.setdefault("metadata", {})[cellhistory.KEY] = []
        mutated = False
        for event in pending:
            event.update(cell_id=cell_id, **refs)
            if event.get("kind") == "run":
                executed = deepcopy(after)
                executed["source"] = event.get("source", executed.get("source", ""))
                event["executed_revision"] = snapshot(meta, executed)
            append(meta, event)
            mutated |= event.get("kind") in {"created", "edit", "external_edit", "undo", "run", "clear", "restore"}
        if before is None and not mutated:
            append(meta, {"kind": "created", "cell_id": cell_id, "actor": actor, **refs})
        elif after is None:
            append(meta, {**context, "kind": "delete", "cell_id": cell_id, "actor": actor,
                          "index": list(old).index(cell_id), **refs})
        elif refs.get("before_revision") != refs["after_revision"] and not mutated:
            append(meta, {"kind": "external_edit" if actor == "External file edit" else "edit",
                          "cell_id": cell_id, "actor": actor, **refs})
    if [cid for cid in old if cid in new] != [cid for cid in new if cid in old]:
        append(meta, {**context, "kind": "reorder", "actor": actor,
                      "before_order": list(old), "after_order": list(new)})
    if previous is not None:
        before = {k: v for k, v in previous.metadata.items() if k != KEY}
        after = {k: v for k, v in current.metadata.items() if k != KEY}
        if before != after:
            append(meta, {"kind": "metadata", "actor": actor, "before": before, "after": after})


class GspyNotebook(ContentNotebook, Notebook):
    def __init__(self, path, publish=None):
        super().__init__(path, publish)
        self._committed = None
        self._committed_version = None
        self._actor = "GusNotebook"
        self._audit_context = {}

    def _blank(self):
        nb = super()._blank()
        nb.cells = [nbformat.v4.new_markdown_cell("")]
        nb.metadata[KEY] = {"format": "gspy", "schema_version": SCHEMA_VERSION,
                            "document_id": uuid.uuid4().hex, "journal": [],
                            "revisions": {}, "artifacts": {}}
        return nb

    def load(self):
        with self._lock:
            version = disk_version(self.path)
            if version is None:
                if self._nb is not None:
                    raise NotebookReadError(f"{self.path.name} was removed from disk; restore it before saving")
                self._nb = self._blank()
                self._save()
                return self._nb
            try:
                candidate = nbformat.from_dict(json.loads(self.path.read_text(encoding="utf-8")))
                # nbformat disk sources may be line arrays; normalize only after
                # validating the serialized schema and identities.
                validate(candidate)
                candidate = nbformat.reads(json.dumps(candidate), as_version=4)
                if disk_version(self.path) != version:
                    raise ValueError("file changed while it was being read")
                if self._committed is not None:
                    reconcile(self._committed, candidate, "External file edit")
                validate(candidate)
            except (ValueError, TypeError, KeyError, AttributeError, nbformat.ValidationError) as exc:
                raise NotebookReadError(f"Cannot read {self.path.name}: {exc}. The original file has been preserved.") from exc
            changed = self._committed is not None and candidate != self._committed
            self._nb = candidate
            self._version = version
            if changed:
                self._save(reconciled=True)
            else:
                self._committed = deepcopy(candidate)
                self._committed_version = version
                self._mtime = self._disk_mtime()
            return self._nb

    def _save(self, reconciled=False):
        try:
            if not reconciled:
                reconcile(self._committed, self._nb, self._actor, self._audit_context)
            validate(self._nb)
            version = atomic_write(self.path, nbformat.writes(self._nb), self._version)
        except Exception:
            self._nb = deepcopy(self._committed)
            self._version = self._committed_version
            raise
        self._version = self._committed_version = version
        self._mtime = self._disk_mtime()
        self._committed = deepcopy(self._nb)
        counts = {}
        for event in metadata(self._nb)["journal"]:
            cid = event.get("cell_id")
            if cid:
                counts[cid] = counts.get(cid, 0) + 1
        for cid, count in counts.items():
            if count != self._history_counts.get(cid):
                self.publish("cell_history_changed", cell_id=cid, notebook=str(self.path),
                             summary=self.cell_history(cid)["summary"])
        self._history_counts = counts
        self.publish("document_history_changed", notebook=str(self.path))

    def _cell_json(self, cell):
        result = super()._cell_json(cell)
        result["history_summary"] = self.cell_history(cell["id"])["summary"]
        result["content"] = self.content_json(cell)
        return result

    def to_json(self):
        with self._lock:
            result = super().to_json()
            meta = metadata(self.nb)
            result.update(document_id=meta["document_id"], history_revision=len(meta["journal"]))
            return result

    def cell_history(self, cell_id):
        with self._lock:
            meta = metadata(self.nb)
            events = [e for e in meta["journal"] if e.get("cell_id") == cell_id]
            return cellhistory.view({"id": cell_id, "metadata": {cellhistory.KEY: events}})

    def record_history(self, cell_id, kind, *, context=None, **details):
        with self._lock:
            _, cell = self.find(cell_id)
            if cell is None:
                return None
            cellhistory.append(cell, kind, context=context, **details)
            self._save()
            return self.cell_history(cell_id)

    def _structural_change(self, operation, cell_id, *args, audit_context=None):
        with self._lock:
            self._audit_context = {**(audit_context or {}), "cell_id": cell_id}
            self._actor = self._audit_context.get("actor", "GusNotebook")
            try:
                return operation(cell_id, *args)
            finally:
                self._actor = "GusNotebook"
                self._audit_context = {}

    def delete_cell(self, cell_id, audit_context=None):
        return self._structural_change(super().delete_cell, cell_id, audit_context=audit_context)

    def move_cell(self, cell_id, index, audit_context=None):
        return self._structural_change(super().move_cell, cell_id, index, audit_context=audit_context)

    def record_export(self, cell_id, snapshot_id, destination):
        with self._lock:
            events = self.cell_history(cell_id)["events"]
            copied = next((e for e in events if e.get("kind") == "copy" and e.get("snapshot_id") == snapshot_id), None)
            if not copied or any(e.get("kind") == "export" and e.get("snapshot_id") == snapshot_id
                                 and e.get("destination") == destination for e in events):
                return
            # A report can still refer to a subsequently deleted block.
            append(metadata(self.nb), {"kind": "export", "cell_id": cell_id,
                                      "actor": "Observed in saved HTML", "snapshot_id": snapshot_id,
                                      "destination": destination, "copied_at": copied.get("at")})
            self._save()

    def set_outputs(self, cell_id, outputs, execution_count=None, executed_source=None):
        with self._lock:
            _, current = self.find(cell_id)
            if current is not None and current.cell_type == "code":
                return super().set_outputs(cell_id, outputs, execution_count, executed_source)
            # A running block can be deleted or converted to Markdown before
            # the kernel finishes. Archive its result without resurrecting it
            # or putting Python outputs onto the newly edited narrative.
            meta = metadata(self.nb)
            previous = None
            for event in reversed(meta["journal"]):
                if event.get("cell_id") != cell_id:
                    continue
                for key in ("after_revision", "before_revision", "executed_revision"):
                    ref = event.get(key)
                    if ref and meta["revisions"][ref]["cell_type"] == "code":
                        previous = materialize(meta, ref)
                        break
                if previous is not None:
                    break
            if previous is None:
                return
            previous.source = executed_source if executed_source is not None else previous.source
            previous.outputs = deepcopy(outputs)
            previous.execution_count = execution_count
            append(meta, {"kind": "run", "cell_id": cell_id, "actor": "Kernel",
                          "source": previous.source, "execution_count": execution_count,
                          "executed_revision": snapshot(meta, previous),
                          "output": cellhistory.output_summary(outputs),
                          "note": "Execution finished after the block was deleted or its type changed."})
            self._save()

    def document_history(self, before=None, limit=50):
        with self._lock:
            meta = metadata(self.nb)
            end = min(before - 1, len(meta["journal"])) if before is not None else len(meta["journal"])
            start = max(0, end - limit)
            events = deepcopy(meta["journal"][start:end])
            for event in events:
                ref = event.get("executed_revision") or event.get("after_revision") or event.get("before_revision")
                event["restorable"] = bool(ref) and event["kind"] in {
                    "created", "edit", "external_edit", "undo", "restore", "run", "delete", "clear"}
                if ref:
                    event["source"] = meta["revisions"][ref].get("source", "")
            return {"document_id": meta["document_id"], "revision": len(meta["journal"]),
                    "events": events, "before": start + 1 if start else None}

    def export_history(self):
        with self._lock:
            meta = metadata(self.nb)
            records = [{"record": "document", "format": "gspy-history", "schema_version": SCHEMA_VERSION,
                        "document_id": meta["document_id"], "path": str(self.path),
                        "revision": len(meta["journal"]), "journal_sha256": digest(meta["journal"]),
                        "cell_order": [cell["id"] for cell in self.nb.cells],
                        "notebook_metadata": {k: v for k, v in self.nb.metadata.items() if k != KEY}}]
            for name in ("revisions", "artifacts"):
                records.extend({"record": name[:-1], "id": key, "data": value} for key, value in meta[name].items())
            records.extend({**e, "record": "event"} for e in meta["journal"])
            return "".join(encode(record) + "\n" for record in records)

    def restore_revision(self, event_id, expected_revision):
        with self._lock:
            meta = metadata(self.nb)
            if type(expected_revision) is not int or expected_revision != len(meta["journal"]):
                raise ExternalChangeError("Notebook history changed; refresh before restoring")
            event = next((e for e in meta["journal"] if e["id"] == event_id), None)
            ref = event and (event.get("executed_revision") or event.get("after_revision") or event.get("before_revision"))
            if not ref:
                raise ValueError("This event has no block revision to restore")
            restored = materialize(meta, ref)
            cid = restored["id"]
            index, current = self.find(cid)
            if current is None:
                self.nb.cells.insert(min(event.get("index", len(self.nb.cells)), len(self.nb.cells)), restored)
            else:
                self.nb.cells[index] = restored
            # Restoring source does not restore the kernel. Historical outputs
            # remain in the journal and can be inspected/downloaded there.
            if restored.cell_type == "code":
                restored.outputs = []
                restored.execution_count = None
            cellhistory.append(restored, "restore", context={"actor": "User"}, restored_from=event_id,
                               summary=LABELS["restore"])
            self._save()
            self.publish("notebook_changed", notebook=str(self.path), cell_id=cid, reason="restore")
            return self._cell_json(restored)

    def duplicate(self, destination):
        with self._lock:
            candidate = deepcopy(self.nb)
            meta = metadata(candidate)
            original = meta["document_id"]
            meta["document_id"] = uuid.uuid4().hex
            append(meta, {"kind": "duplicate", "origin_document_id": original,
                          "origin_path": str(self.path), "actor": "User"})
            validate(candidate)
            atomic_write(destination, nbformat.writes(candidate), expected_version=None)
