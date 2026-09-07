"""Portable cell timelines and links from copied results to saved reports.

Entries live in cell metadata, independently of the bounded undo stack and
workspace recordings. They describe actions the app observes, not a transcript
of an agent's private reasoning or work performed outside the app.
"""

from copy import deepcopy
import difflib
import hashlib
from html.parser import HTMLParser
import json
import time
import uuid

KEY = "gusnotebook_history"
LABELS = {
    "created": "Cell created", "edit": "Cell edited", "external_edit": "External edit observed",
    "request": "Agent request", "legacy_request": "Earlier request",
    "undo": "Cell source restored", "run": "Cell executed", "clear": "Outputs cleared",
    "copy": "Output copied", "export": "Output added to HTML", "note": "Note added",
    "settings": "Agent preferences updated",
}


def entries(cell):
    raw = (cell.get("metadata") or {}).get(KEY)
    if isinstance(raw, list):
        return [event for event in raw if isinstance(event, dict) and isinstance(event.get("id"), str)]
    # The previous release retained only a shortened caption. Do not invent a
    # timestamp or claim to recover earlier conversation turns.
    meta = cell.get("metadata") or {}
    return [{"id": "legacy-" + name, "kind": "legacy_request", "at": None,
             "actor": "Agent", "prompt": meta[name], "summary": "Previously saved request"}
            for name in ("claude_prompt", "inline_prompt") if meta.get(name)]


def append(cell, kind, *, context=None, **details):
    events = entries(cell)
    context = deepcopy(context or {})
    # A request can arrive through the focus hook and later through a write.
    if kind == "request" and context.get("request_id") and any(
            e.get("kind") == kind and e.get("request_id") == context["request_id"] for e in events):
        return None
    event = {**context, **deepcopy(details), "id": uuid.uuid4().hex,
             "kind": kind, "at": time.time()}
    event.setdefault("summary", LABELS.get(kind, kind))
    events.append(event)
    cell.setdefault("metadata", {})[KEY] = events
    return event


def summary(cell):
    events = entries(cell)
    counts = {name: sum(e.get("kind") in kinds for e in events) for name, kinds in {
        "requests": {"request", "legacy_request"}, "edits": {"edit", "external_edit", "undo"},
        "copies": {"copy"}, "exports": {"export"}, "notes": {"note", "settings"},
    }.items()}
    return {"count": len(events), **counts,
            "latest": events[-1].get("summary", "") if events else ""}


def revision(cell):
    return {"source": cell.get("source", ""), "cell_type": cell.get("cell_type", "code"),
            "cell_role": (cell.get("metadata") or {}).get("cell_role")}


def changed(cell, before, *, kind="edit", context=None):
    after = revision(cell)
    if before == after:
        return
    if context and context.get("prompt"):
        append(cell, "request", context=context)
    append(cell, kind, context=context, before=before, after=after)


def view(cell):
    result = deepcopy(entries(cell))
    for event in result:
        if all(isinstance(event.get(key), dict) and isinstance(event[key].get("source"), str)
               for key in ("before", "after")):
            event["diff"] = "".join(difflib.unified_diff(
                event["before"]["source"].splitlines(keepends=True),
                event["after"]["source"].splitlines(keepends=True),
                fromfile="Before", tofile="After"))
    return {"cell_id": cell["id"], "summary": summary(cell), "events": result}


def output_summary(outputs):
    encoded = json.dumps(outputs, sort_keys=True, ensure_ascii=False).encode()
    return {"count": len(outputs), "sha256": hashlib.sha256(encoded).hexdigest(),
            "types": sorted({o.get("output_type", "unknown") for o in outputs}),
            "errors": [str(o.get("ename", "Error")) for o in outputs if o.get("output_type") == "error"]}


def reconcile(previous, current):
    """Keep known history when external tools or workspace undo rewrite a cell."""
    old = {c.get("id"): c for c in previous.cells}
    dirty = False
    for cell in current.cells:
        before = old.get(cell.get("id"))
        if before is None:
            append(cell, "created", context={"actor": "External file edit"}, after=revision(cell))
            dirty = True
            continue
        known = entries(before)
        existing = entries(cell)
        ids = {e["id"] for e in known}
        merged = known + [e for e in existing if e["id"] not in ids]
        if merged != existing:
            cell.setdefault("metadata", {})[KEY] = deepcopy(merged)
            dirty = True
        if revision(before) != revision(cell):
            changed(cell, revision(before), kind="external_edit", context={"actor": "External file edit"})
            dirty = True
    return dirty


class SnapshotLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = set()

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag != "figure" or data.get("data-gusnb-provenance") != "1":
            return
        values = tuple(data.get(key, "") for key in
                       ("data-gusnb-notebook", "data-gusnb-cell-id", "data-gusnb-snapshot"))
        if all(values):
            self.links.add(values)


def record_links(registry, destination, source):
    """Link only registered notebooks and copies whose receipts we can verify.

    HTML is untrusted data: its attributes must never cause arbitrary files to
    be opened or modified. Source notebooks can be reopened to discover links.
    """
    if destination.suffix.lower() not in {".html", ".htm"} or "data-gusnb-snapshot" not in source:
        return
    parser = SnapshotLinks()
    parser.feed(source)
    for path, cell_id, snapshot in parser.links:
        doc = registry.peek(path)
        if doc:
            doc.record_export(cell_id, snapshot, str(destination))
        else:
            # A notebook may have moved since the snapshot was copied. Receipt
            # IDs, not HTML-supplied paths, establish the source cell.
            for _, candidate in registry.items():
                candidate.record_export(cell_id, snapshot, str(destination))
