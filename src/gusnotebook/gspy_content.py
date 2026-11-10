"""Explicit content bindings and immutable execution receipts for GSPY.

Rendering only reads saved receipts. It never asks a kernel to evaluate code.
The journal owns run records, so exporting the journal also exports provenance.
"""

from copy import deepcopy
import keyword
import re
import time

import nbformat

from .persistence import ExternalChangeError


def identifier(value):
    return isinstance(value, str) and value.isidentifier() and not keyword.iskeyword(value)


def definition(cell):
    return deepcopy({"results": [], "layout": "flow", "components": [], "bindings": {},
                     **cell.get("metadata", {}).get("gusnotebook_content", {})})


def validate_definition(value):
    if not isinstance(value, dict) or set(value) - {"results", "layout", "components", "bindings"}:
        raise ValueError("Invalid content definition")
    names = value.get("results", [])
    if not isinstance(names, list) or len(names) > 32 or len(set(map(str, names))) != len(names) or not all(map(identifier, names)):
        raise ValueError("Publish up to 32 distinct Python variable names")
    if not isinstance(value.get("layout", "flow"), str) or value.get("layout", "flow") not in {"flow", "columns", "slide"}:
        raise ValueError("Unknown content layout")
    components = value.get("components", [])
    bindings = value.get("bindings", {})
    if not isinstance(components, list) or len(components) > 100 or not isinstance(bindings, dict) or len(bindings) > 100:
        raise ValueError("Too many content components or bindings")
    ids = set()
    for component in components:
        if not isinstance(component, dict) or not identifier(component.get("id")) or component["id"] in ids:
            raise ValueError("Content components require unique IDs")
        ids.add(component["id"])
        if component.get("kind") == "markdown":
            if not isinstance(component.get("source"), str) or len(component["source"]) > 200000:
                raise ValueError("Invalid paragraph")
        elif component.get("kind") == "output":
            if not isinstance(component.get("producer"), str) or not isinstance(component.get("result"), str):
                raise ValueError("Output placements need a producer and result name")
        else:
            raise ValueError("Unknown component type")
    for name, binding in bindings.items():
        if not identifier(name) or not isinstance(binding, dict) or not isinstance(binding.get("producer"), str) or not identifier(binding.get("result")):
            raise ValueError("Value references need a name, producer and published variable")
        path = binding.get("path", [])
        if not isinstance(path, list) or len(path) > 12 or not all(type(p) in {str, int} for p in path):
            raise ValueError("Value paths contain field names or array indices")
        if not isinstance(binding.get("format", ""), str) or not re.fullmatch(r"(?:\.[0-9]{1,2}[fge%])?", binding.get("format", "")):
            raise ValueError("Use a number format such as .2f, .3g or .1%")
    return deepcopy({"results": names, "layout": value.get("layout", "flow"),
                     "components": components, "bindings": bindings})


class ContentNotebook:
    """Mixed into GspyNotebook; all mutations use its existing atomic journal."""

    def _receipts(self):
        from .gspy import metadata
        return [e for e in metadata(self.nb)["journal"] if e.get("run_id") and e["kind"] == "run"]

    def _successful(self):
        from .gspy import metadata
        result = {}
        for event in metadata(self.nb)["journal"]:
            if event["kind"] == "clear":
                result.pop(event.get("cell_id"), None)
            elif event["kind"] == "run" and event.get("run_id") and event.get("status") == "success":
                result[event["cell_id"]] = event
        return result

    def _signature(self, cell):
        from .gspy import digest
        return digest({"source": cell.source, "results": definition(cell)["results"]})

    def run_states(self):
        from .gspy import metadata
        latest = {e["cell_id"]: e for e in self._receipts()}
        successful = self._successful()
        started = {e["cell_id"]: e for e in metadata(self.nb)["journal"] if e["kind"] == "run_started"}
        restored = {e["cell_id"]: e["seq"] for e in metadata(self.nb)["journal"] if e["kind"] in {"restore", "undo"}}
        states, upstream = {}, {}
        upstream_fresh = True
        for cell in self.nb.cells:
            if cell.cell_type != "code" or (not cell.source.strip() and cell.id not in successful):
                continue
            run = successful.get(cell.id)
            attempt = latest.get(cell.id)
            start = started.get(cell.id)
            reason = "Not run"
            state = "missing"
            if run:
                state, reason = "saved", "Saved result; kernel state has not been verified"
                if getattr(self, "_kernel_generation", None):
                    state, reason = "fresh", "Current result"
                if run["signature"] != self._signature(cell):
                    state, reason = "stale", "Code or published variables changed"
                elif run["upstream"] != upstream or not run["prerequisites_valid"] or not upstream_fresh:
                    state, reason = "stale", "Earlier blocks changed or were not run in order"
                elif getattr(self, "_kernel_generation", None) and run["kernel_generation"] != self._kernel_generation:
                    state, reason = "stale", "Kernel restarted; run in order to refresh"
                elif attempt and attempt["status"] != "success":
                    state, reason = "stale", "Last run failed; showing the last successful result"
                elif restored.get(cell.id, 0) > run["seq"]:
                    state, reason = "stale", "Source restored; run again to refresh the kernel and results"
            if start and (not attempt or start["seq"] > attempt["seq"]):
                state, reason = "stale", "Run started without a saved completion"
            if any(v["cell_id"] == cell.id for v in getattr(self, "_pending_results", {}).values()):
                state, reason = "stale", "Execution completed but saving failed; retry saving without rerunning"
            states[cell.id] = {"state": state, "reason": reason, "run_id": run and run["run_id"],
                               "attempt_id": attempt and attempt["run_id"],
                               "error": (attempt or {}).get("error", ""),
                               "pending_run_id": next((key for key, v in getattr(self, "_pending_results", {}).items() if v["cell_id"] == cell.id), None)}
            upstream[cell.id] = run["run_id"] if run else None
            upstream_fresh &= state in {"fresh", "saved"}
        return states

    def begin_run(self, cell_id, run_id, generation, python, source):
        from .gspy import append, metadata, snapshot
        with self._lock:
            self._kernel_generation = generation
            meta = metadata(self.nb)
            if any(e.get("run_id") == run_id for e in meta["journal"]):
                raise ValueError("This run ID has already been dispatched; inspect or retry saving it instead")
            _, cell = self.find(cell_id)
            if cell is None or cell.cell_type != "code" or cell.source != source:
                raise ExternalChangeError("Block changed while queued; refresh before running")
            states = self.run_states()
            upstream = {}
            valid = True
            for prior in self.nb.cells:
                if prior.id == cell_id:
                    break
                if prior.id in states:
                    upstream[prior.id] = states[prior.id]["run_id"]
                    valid &= states[prior.id]["state"] == "fresh"
            record = {"kind": "run_started", "cell_id": cell_id, "run_id": run_id,
                      "actor": "Kernel", "summary": "Execution started",
                      "executed_revision": snapshot(meta, cell), "signature": self._signature(cell),
                      "declarations": definition(cell)["results"], "kernel_generation": generation,
                      "python": python, "upstream": upstream, "prerequisites_valid": valid,
                      "started_at": time.time()}
            append(meta, record)
            self._save()
            return record

    def finish_run(self, record, outputs, count, capture):
        from .gspy import append, digest, materialize, metadata, snapshot
        with self._lock:
            record = deepcopy(record)
            if capture.get("generation") and capture["generation"] != record["kernel_generation"]:
                record["kernel_generation"] = capture["generation"]
                record["prerequisites_valid"] = not record["upstream"]
            self._kernel_generation = record["kernel_generation"]
            run_id = record["run_id"]
            self._pending_results = getattr(self, "_pending_results", {})
            self._pending_results[run_id] = {"cell_id": record["cell_id"], "args": deepcopy((record, outputs, count, capture))}
            meta = metadata(self.nb)
            if any(e.get("run_id") == run_id for e in self._receipts()):
                self._pending_results.pop(run_id, None)
                return
            error = next((o for o in outputs if o.get("output_type") == "error"), None)
            status = "cancelled" if error and error.get("ename") == "KeyboardInterrupt" else "error" if error else "success"
            if capture.get("status") != "ok" and not error:
                status = "unknown"
            executed = materialize(meta, record["executed_revision"])
            executed.outputs, executed.execution_count = [nbformat.from_dict(o) for o in outputs], count
            _, current = self.find(record["cell_id"])
            if current is not None and current.cell_type == "code":
                current.outputs, current.execution_count = [nbformat.from_dict(o) for o in outputs], count
            artifact = digest(outputs)
            meta["artifacts"].setdefault(artifact, deepcopy(outputs))
            append(meta, {**record, "kind": "run", "summary": "Execution " + status,
                          "status": status, "finished_at": time.time(), "execution_count": count,
                          "executed_revision": snapshot(meta, executed), "outputs_ref": artifact,
                          "values": capture.get("values", {}) if status == "success" else {},
                          "capture_errors": capture.get("errors", {}),
                          "error": (error.get("ename", "Error") + ": " + error.get("evalue", "")) if error else ""})
            self._save()
            self._pending_results.pop(run_id, None)

    def retry_result(self, run_id):
        with self._lock:
            pending = getattr(self, "_pending_results", {}).get(run_id)
            if not pending:
                raise ValueError("No pending result in this server process")
            self.finish_run(*pending["args"])

    def inspect_run(self, run_id):
        from .gspy import metadata
        with self._lock:
            run = next((e for e in self._receipts() if e["run_id"] == run_id), None)
            if not run:
                raise ValueError("Run not found")
            return {**deepcopy(run), "source": metadata(self.nb)["revisions"][run["executed_revision"]]["source"],
                    "outputs": deepcopy(metadata(self.nb)["artifacts"][run["outputs_ref"]])}

    def clear_outputs(self, cell_id=None):
        from . import cellhistory
        with self._lock:
            successful = self._successful()
            targets = self.nb.cells if cell_id is None else [self.find(cell_id)[1]]
            for cell in targets:
                if cell is not None and cell.id in successful and not cell.get("outputs"):
                    cellhistory.append(cell, "clear", context={"actor": "User"})
            return super().clear_outputs(cell_id)

    def update_content(self, cell_id, content, expected):
        from .gspy import digest
        with self._lock:
            _, cell = self.find(cell_id)
            if cell is None:
                raise ValueError("Block not found")
            if expected != digest(definition(cell)):
                raise ExternalChangeError("Content changed; reopen the content editor")
            cell.metadata["gusnotebook_content"] = validate_definition(content)
            cell.metadata["gusnotebook_review"] = self._supporting_runs(cell)
            self._save()
            self.publish("notebook_changed", notebook=str(self.path), cell_id=cell_id, reason="content")
            return self._cell_json(cell)

    def _supporting_runs(self, cell):
        config = definition(cell)
        producers = {value["producer"] for value in config["bindings"].values()}
        if any(c["kind"] == "markdown" and c["source"].strip() for c in config["components"]):
            producers.update(c["producer"] for c in config["components"] if c["kind"] == "output")
        successful = self._successful()
        return {cid: successful[cid]["run_id"] if cid in successful else None for cid in sorted(producers)}

    def review_content(self, cell_id, expected):
        from . import cellhistory
        from .gspy import digest
        with self._lock:
            _, cell = self.find(cell_id)
            if cell is None:
                raise ValueError("Block not found")
            evidence = self._supporting_runs(cell)
            if expected != digest({"source": cell.source, "content": definition(cell), "runs": evidence}):
                raise ExternalChangeError("Content or supporting results changed; review the updated content")
            cell.metadata["gusnotebook_review"] = evidence
            cellhistory.append(cell, "review", context={"actor": "User"},
                               summary="Interpretation reviewed", supporting_runs=evidence)
            self._save()
            return self._cell_json(cell)

    def content_json(self, cell):
        from .gspy import digest, metadata
        config = definition(cell)
        successful, states = self._successful(), self.run_states()
        order = [c.id for c in self.nb.cells]
        def receipt(producer):
            if producer not in states or producer not in order or order.index(producer) > order.index(cell.id):
                return None
            return successful.get(producer)
        bindings = {}
        for name, binding in config["bindings"].items():
            run = receipt(binding["producer"])
            resolved = {"producer": binding["producer"], "run_id": run and run["run_id"],
                        "state": states.get(binding["producer"], {}).get("state", "missing")}
            try:
                if not run:
                    raise ValueError("Producer missing, later in the document, or not successfully run")
                value = run["values"][binding["result"]]
                for field in binding.get("path", []):
                    value = value[field]
                if type(value) not in {str, int, float, bool, type(None)}:
                    raise ValueError("Choose a scalar field from the published result")
                resolved["text"] = format(value, binding.get("format", "")) if value is not None else "null"
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                resolved.update(text="missing", state="missing", error=str(exc))
            bindings[name] = resolved
        components = deepcopy(config["components"])
        if not components:
            components = ([{"id": "source", "kind": "markdown", "source": cell.source}] if cell.cell_type == "markdown"
                          else [{"id": "outputs", "kind": "output", "producer": cell.id, "result": "*"}])
        for component in components:
            if component["kind"] != "output":
                continue
            run = receipt(component["producer"])
            outputs = deepcopy(metadata(self.nb)["artifacts"][run["outputs_ref"]]) if run else []
            if component["result"] != "*":
                outputs = [o for o in outputs if o.get("metadata", {}).get("gusnotebook", {}).get("result") == component["result"]]
            state = states.get(component["producer"], {}).get("state", "missing") if run else "missing"
            if component["result"] != "*" and not outputs:
                state = "missing"
            component.update(outputs=outputs, run_id=run and run["run_id"],
                             state=state)
            # Stage-one documents can show their ordinary saved outputs until
            # their first captured run, without claiming a run identity.
            if not run and component["producer"] == cell.id and component["result"] == "*":
                component["outputs"] = deepcopy(cell.get("outputs", []))
        run = successful.get(cell.id)
        supporting = self._supporting_runs(cell)
        return {"definition": config, "version": digest(config), "bindings": bindings, "components": components,
                "run": states.get(cell.id), "published": list((run or {}).get("values", {})),
                "review_pending": bool(supporting) and supporting != cell.metadata.get("gusnotebook_review", {}),
                "review_version": digest({"source": cell.source, "content": config, "runs": supporting}),
                "capture_errors": (run or {}).get("capture_errors", {})}
