"""Atomic rich-document edits over the existing ordered, audited cells."""

from copy import deepcopy
import re

import nbformat

from .persistence import ExternalChangeError

RICH_KEY = "gusnotebook_rich_text"


def validate_delta(ops):
    if not isinstance(ops, list) or len(ops) > 20000:
        raise ValueError("Invalid rich-text document")
    for op in ops:
        if not isinstance(op, dict) or set(op) - {"insert", "attributes"}:
            raise ValueError("Only saved rich-text inserts are allowed")
        if not isinstance(op.get("insert"), str):
            # Python stays in actual code cells; pasted editor embeds cannot
            # smuggle executable source into a narrative paragraph.
            raise ValueError("Narrative content must contain text")
        attrs = op.get("attributes", {})
        if not isinstance(attrs, dict) or set(attrs) - {
            "bold", "italic", "underline", "strike", "code", "link", "header",
            "list", "indent", "align", "blockquote", "color", "background", "size"}:
            raise ValueError("Unsupported rich-text format")
        if any(type(value) not in {str, bool, int} for value in attrs.values()):
            raise ValueError("Invalid rich-text format")
    if sum(len(op["insert"]) for op in ops) > 1000000:
        raise ValueError("Narrative section is too large")


class EditorNotebook:
    def editor_version(self):
        from .gspy import digest
        # Execution and code edits may finish during prose typing. They do not
        # invalidate its save, and the save never writes their source/outputs.
        return digest([{"id": c.id, "type": c.cell_type,
                        "content": c.metadata.get("gusnotebook_content"),
                        **({"source": c.source, "rich": c.metadata.get(RICH_KEY)}
                           if c.cell_type == "markdown" else {})}
                       for c in self.nb.cells])

    def save_document(self, blocks, expected_version, audit_context=None):
        from . import cellhistory
        from .gspy import digest, materialize, metadata
        with self._lock:
            if expected_version != self.editor_version():
                raise ExternalChangeError("Document text or order changed elsewhere. Your draft is kept; reload to review the saved document.")
            if not isinstance(blocks, list) or not 1 <= len(blocks) <= 10000:
                raise ValueError("A document must contain between 1 and 10,000 sections")
            previous = {c.id: c for c in self.nb.cells}
            candidate, ids = [], set()
            for block in blocks:
                cid = block.get("id") if isinstance(block, dict) else None
                if not isinstance(cid, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", cid) or cid in ids:
                    raise ValueError("Document sections require unique stable IDs")
                ids.add(cid)
                kind = block.get("cell_type")
                if kind not in {"markdown", "code", "raw"}:
                    raise ValueError("Unsupported document section")
                cell = deepcopy(previous.get(cid))
                if cell is not None and cell.cell_type != kind:
                    raise ValueError("Use the cell type action to change an existing section")
                if cell is None:
                    # Undo may put back a Python embed after its deletion was
                    # already saved. Restore its real archived source, never a
                    # browser copy that could predate an agent's last edit.
                    for event in reversed(metadata(self.nb)["journal"]):
                        if event.get("cell_id") != cid:
                            continue
                        ref = event.get("after_revision") or event.get("before_revision")
                        if ref:
                            cell = materialize(metadata(self.nb), ref)
                            if cell.cell_type != kind:
                                raise ValueError("Archived section has a different type")
                            if kind == "code":
                                cell.outputs, cell.execution_count = [], None
                            cellhistory.append(cell, "restore", context={"actor": "User"}, summary="Section restored by editor undo")
                            break
                    if cell is None:
                        factory = {"code": nbformat.v4.new_code_cell, "markdown": nbformat.v4.new_markdown_cell,
                                   "raw": nbformat.v4.new_raw_cell}[kind]
                        cell = factory("")
                        cell.id = cid
                        if kind == "code":
                            cell.metadata["gusnotebook_display"] = "output"
                if kind == "markdown":
                    source, delta = block.get("source"), block.get("delta")
                    if not isinstance(source, str) or len(source) > 2000000:
                        raise ValueError("Invalid narrative HTML")
                    validate_delta(delta)
                    cell.source = source
                    cell.metadata[RICH_KEY] = {"source_hash": digest(source), "ops": deepcopy(delta)}
                    if "bindings" in block:
                        from .gspy_content import definition, validate_definition
                        config = definition(cell)
                        config["bindings"] = block["bindings"]
                        cell.metadata["gusnotebook_content"] = validate_definition(config)
                elif kind == "code" and block.get("extract_text"):
                    from .gspy_content import definition
                    config = definition(cell)
                    config["components"] = [c for c in config["components"] if c["kind"] != "markdown"]
                    cell.metadata["gusnotebook_content"] = config
                candidate.append(cell)
            # Validation above is side-effect free. Commit content, deletions,
            # new sections and ordering in one existing journal transaction.
            self.nb.cells = candidate
            self._actor = (audit_context or {}).get("actor", "User")
            self._audit_context = audit_context or {}
            try:
                self._save()
            finally:
                self._actor, self._audit_context = "GusNotebook", {}
            self.publish("notebook_changed", notebook=str(self.path), reason="document_edit")
            return self.to_json()

    def set_display_mode(self, cell_id, mode, audit_context=None):
        from . import cellhistory
        with self._lock:
            _, cell = self.find(cell_id)
            if cell is None or cell.cell_type != "code" or mode not in {"output", "background"}:
                raise ValueError("Choose output or background for a Python step")
            cell.metadata["gusnotebook_display"] = mode
            cellhistory.append(cell, "edit", context=audit_context or {"actor": "User"},
                               summary="Python display mode changed to " + mode)
            self._save()
            self.publish("notebook_changed", notebook=str(self.path), cell_id=cell_id, reason="display_mode")
            return self._cell_json(cell)
