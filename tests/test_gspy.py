"""GSPY portability, durable history, conflicts, and mixed-format routing."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import nbformat

from gusnotebook.app import close_app, create_app
from gusnotebook import cellhistory, gspy
from gusnotebook.notebook import NotebookReadError, Registry
from gusnotebook.persistence import ExternalChangeError


class GspyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "analysis.gspy"
        self.doc = Registry().get(self.path)
        self.cell = self.doc.add_cell("code", "performance = 0.25")["id"]

    def events(self):
        return self.doc.document_history(limit=200)["events"]

    def test_formats_share_nbformat_but_keep_separate_metadata(self):
        ordinary = Registry().get(self.root / "ordinary.ipynb")
        self.assertEqual(ordinary.to_json()["format"], "ipynb")
        self.assertNotIn(gspy.KEY, ordinary.nb.metadata)
        self.assertEqual(self.doc.to_json()["format"], "gspy")
        self.assertEqual(self.doc.nb.cells[0].cell_type, "markdown")
        nbformat.validate(nbformat.read(self.path, 4))
        self.assertTrue(all(c.metadata[cellhistory.KEY] == [] for c in self.doc.nb.cells))

    def test_full_request_deduplicates_across_edit_caption_and_restart(self):
        prompt = "Remember every word.\n" * 100
        context = {"request_id": "request-one", "prompt": prompt, "actor": "Agent"}
        self.doc.record_history(self.cell, "request", context=context)
        self.doc.update_cell(self.cell, source="performance = 0.26", audit_context=context)
        self.doc.set_claude_prompt(self.cell, prompt, record=False)
        reopened = Registry().get(self.path)
        history = reopened.cell_history(self.cell)
        self.assertEqual([e["prompt"] for e in history["events"] if e["kind"] == "request"], [prompt])
        self.assertEqual(history["summary"]["requests"], 1)

    def test_output_versions_survive_clear_delete_and_restart_in_complete_log(self):
        for value in ("first\noutput", "second\noutput", "second\noutput"):
            self.doc.set_outputs(self.cell, [{"output_type": "stream", "name": "stdout", "text": value}], 1)
        self.doc.clear_outputs(self.cell)
        self.doc.delete_cell(self.cell)
        reopened = Registry().get(self.path)
        records = [json.loads(line) for line in reopened.export_history().splitlines()]
        artifacts = {r["id"]: r["data"] for r in records if r["record"] == "artifact"}
        revisions = {r["id"]: r["data"] for r in records if r["record"] == "revision"}
        events = [r for r in records if r["record"] == "event"]
        outputs = [artifacts[revisions[e["executed_revision"]]["outputs_ref"]]
                   for e in events if e["kind"] == "run"]
        self.assertEqual([o[0]["text"] for o in outputs], ["first\noutput", "second\noutput", "second\noutput"])
        self.assertEqual(len(artifacts), 3)  # empty plus two distinct output bundles
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        self.assertTrue(reopened.cell_history(self.cell)["events"])
        self.assertIsNone(reopened.cell_json(self.cell))

    def test_restore_deleted_revision_retains_history_without_restoring_kernel_outputs(self):
        self.doc.set_outputs(self.cell, [{"output_type": "stream", "name": "stdout", "text": "old"}], 1)
        self.doc.delete_cell(self.cell)
        deleted = self.events()[-1]
        restored = self.doc.restore_revision(deleted["id"], self.doc.document_history()["revision"])
        self.assertEqual(restored["id"], self.cell)
        self.assertEqual(restored["source"], "performance = 0.25")
        self.assertEqual(restored["outputs"], [])
        self.assertEqual(self.events()[-1]["kind"], "restore")
        self.assertIn("delete", [e["kind"] for e in self.events()])

    def test_restore_run_uses_executed_source_even_if_source_changed_during_run(self):
        self.doc.update_cell(self.cell, source="performance = 0.99")
        self.doc.set_outputs(self.cell, [], 1, executed_source="performance = 0.25")
        run = self.events()[-1]
        restored = self.doc.restore_revision(run["id"], self.doc.document_history()["revision"])
        self.assertEqual(restored["source"], "performance = 0.25")

    def test_execution_finishing_after_deletion_keeps_its_result_without_resurrecting_block(self):
        self.doc.delete_cell(self.cell)
        outputs = [{"output_type": "stream", "name": "stdout", "text": "Finished later"}]
        self.doc.set_outputs(self.cell, outputs, 1, executed_source="print('Finished later')")
        self.assertIsNone(self.doc.cell_json(self.cell))
        event = self.events()[-1]
        self.assertEqual(event["kind"], "run")
        recorded = gspy.materialize(gspy.metadata(self.doc.nb), event["executed_revision"])
        self.assertEqual(recorded.source, "print('Finished later')")
        self.assertEqual(recorded.outputs, outputs)

    def test_restore_requires_current_revision(self):
        event = self.events()[-1]
        revision = self.doc.document_history()["revision"]
        self.doc.update_cell(self.cell, source="newer = True")
        with self.assertRaises(ExternalChangeError):
            self.doc.restore_revision(event["id"], revision)
        self.assertEqual(self.doc.cell_json(self.cell)["source"], "newer = True")

    def test_moves_and_deleted_last_block_are_recorded(self):
        original = [c["id"] for c in self.doc.nb.cells]
        self.doc.move_cell(self.cell, 0)
        self.assertEqual(self.events()[-1]["before_order"], original)
        for cid in original:
            self.doc.delete_cell(cid)
        self.assertEqual(sum(e["kind"] == "delete" for e in self.events()), 2)
        self.assertEqual(len(self.doc.nb.cells), 1)

    def test_failed_saves_roll_back_all_mutations_and_do_not_leak_into_next_save(self):
        operations = [lambda: self.doc.update_cell(self.cell, source="not saved"),
                      lambda: self.doc.delete_cell(self.cell),
                      lambda: self.doc.move_cell(self.cell, 0),
                      lambda: self.doc.add_cell("markdown", "not saved"),
                      lambda: self.doc.record_history(self.cell, "note", note="not saved"),
                      lambda: self.doc.set_outputs(self.cell, [], 99),
                      lambda: self.doc.set_python("/unavailable/python")]
        for action in operations:
            before = self.path.read_bytes()
            view = self.doc.to_json()
            with patch("gusnotebook.gspy.atomic_write", side_effect=OSError("Disk full")):
                with self.assertRaises(OSError):
                    action()
            self.assertEqual(self.doc.to_json(), view)
            self.assertEqual(self.path.read_bytes(), before)
            self.doc.save()
            self.assertNotIn("not saved", self.path.read_text())

    def test_concurrent_disk_edit_preserved_after_stale_save(self):
        original_write = gspy.atomic_write

        def write_after_external_edit(path, text, expected_version):
            external = nbformat.read(path, 4)
            external.cells[1].source = "external = 42"
            nbformat.write(external, path)
            return original_write(path, text, expected_version)

        with patch("gusnotebook.gspy.atomic_write", side_effect=write_after_external_edit):
            with self.assertRaises(ExternalChangeError):
                self.doc.update_cell(self.cell, source="lost = True")
        self.doc.load()
        self.assertEqual(self.doc.cell_json(self.cell)["source"], "external = 42")
        self.assertNotIn("lost = True", self.doc.export_history())
        self.assertEqual(self.events()[-1]["kind"], "external_edit")

    def test_external_rollback_merges_history_and_records_deletion(self):
        older = nbformat.read(self.path, 4)
        self.doc.record_history(self.cell, "note", note="Keep this decision")
        older.cells = older.cells[:1]
        nbformat.write(older, self.path)
        self.doc.load()
        self.assertTrue(any(e.get("note") == "Keep this decision" for e in self.events()))
        self.assertEqual(self.events()[-1]["kind"], "delete")
        count = len(self.events())
        self.doc.load()
        self.assertEqual(len(self.events()), count)

    def test_damaged_or_unsupported_files_are_never_replaced(self):
        valid = json.loads(self.path.read_text())
        candidates = []
        for version in (2, True, "1"):
            data = deepcopy(valid)
            data["metadata"][gspy.KEY]["schema_version"] = version
            candidates.append(data)
        data = deepcopy(valid)
        data["metadata"][gspy.KEY]["journal"][0]["seq"] = 999
        candidates.append(data)
        data = deepcopy(valid)
        data["metadata"][gspy.KEY]["revisions"].clear()
        candidates.append(data)
        data = deepcopy(valid)
        data["cells"][1]["id"] = data["cells"][0]["id"]
        candidates.append(data)
        data = deepcopy(valid)
        del data["cells"][1]["execution_count"]
        candidates.append(data)
        for data in candidates:
            self.path.write_text(json.dumps(data))
            before = self.path.read_bytes()
            with self.assertRaises(NotebookReadError):
                Registry().get(self.path)
            self.assertEqual(self.path.read_bytes(), before)

    def test_modified_known_event_is_rejected_without_losing_last_valid_state(self):
        data = json.loads(self.path.read_text())
        data["metadata"][gspy.KEY]["journal"][0]["actor"] = "Altered attribution"
        self.path.write_text(json.dumps(data))
        before = self.path.read_bytes()
        with self.assertRaises(NotebookReadError):
            self.doc.load()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotEqual(gspy.metadata(self.doc._nb)["journal"][0]["actor"], "Altered attribution")

    def test_history_pagination_does_not_prune_and_exports_include_every_event(self):
        for i in range(55):
            self.doc.record_history(self.cell, "note", note=f"Decision {i}")
        page = self.doc.document_history(limit=20)
        seen = list(page["events"])
        while page["before"]:
            page = self.doc.document_history(page["before"], limit=20)
            seen.extend(page["events"])
        self.assertEqual(sum(e["kind"] == "note" for e in seen), 55)
        self.assertEqual(len({e["id"] for e in seen}), len(seen))

    def test_duplicate_is_independent_and_retains_origin(self):
        target = self.root / "copy.gspy"
        self.doc.duplicate(target)
        duplicate = Registry().get(target)
        self.assertNotEqual(self.doc.to_json()["document_id"], duplicate.to_json()["document_id"])
        event = duplicate.document_history()["events"][-1]
        self.assertEqual(event["origin_document_id"], self.doc.to_json()["document_id"])
        before = target.read_bytes()
        with self.assertRaises(ExternalChangeError):
            self.doc.duplicate(target)
        self.assertEqual(target.read_bytes(), before)

    def test_creation_and_restore_record_positions_for_history_replay(self):
        inserted = self.doc.add_cell("markdown", "Between two blocks", index=1)
        created = self.events()[-1]
        self.assertEqual(created["index"], 1)
        self.doc.delete_cell(inserted["id"])
        deletion = self.events()[-1]
        self.doc.restore_revision(deletion["id"], self.doc.document_history()["revision"])
        self.assertEqual(self.doc.nb.cells[1]["id"], inserted["id"])
        self.assertEqual(self.events()[-1]["index"], 1)

    def test_copy_and_report_receipts_survive_deletion_and_do_not_duplicate(self):
        self.doc.record_history(self.cell, "copy", snapshot_id="snapshot-one")
        self.doc.delete_cell(self.cell)
        self.doc.record_export(self.cell, "snapshot-one", "report.html")
        self.doc.record_export(self.cell, "snapshot-one", "report.html")
        self.assertEqual(sum(e["kind"] == "export" for e in self.events()), 1)
        self.assertEqual(self.events()[-1]["destination"], "report.html")


class GspyApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.app = create_app({"WORK_DIR": str(self.root), "STATE_DIR": str(self.root / "state"),
                               "AUTH_TOKEN": "test-token", "START_WATCHERS": False})
        self.addCleanup(close_app, self.app)
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_AUTHORIZATION"] = "Bearer test-token"
        self.state = self.app.extensions["gusnotebook"]
        self.path = self.root / "analysis.gspy"
        self.query = {"notebook": str(self.path)}

    def test_create_discover_open_rename_copy_and_session_restore(self):
        response = self.client.post("/api/files/new", json={"directory": str(self.root), "name": self.path.name})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["kind"], "notebook")
        listing = self.client.get("/api/files", query_string={"path": str(self.root)}).json
        self.assertEqual(next(e for e in listing["entries"] if e["name"] == self.path.name)["kind"], "notebook")
        opened = self.client.post("/api/open", json={"path": str(self.path)}).json
        self.assertEqual(opened["format"], "gspy")
        renamed = self.root / "renamed.gspy"
        response = self.client.post("/api/files/rename", json={"path": str(self.path), "name": renamed.name})
        self.assertEqual(response.status_code, 200, response.json)
        restored = self.client.post("/api/open", json={"path": str(renamed), "restore": True}).json
        self.assertEqual(restored["document_id"], opened["document_id"])
        self.assertIn(str(renamed), self.state.store.current().tabs)
        response = self.client.post("/api/files/copy", json={"path": str(renamed), "directory": str(self.root), "name": "copy.gspy"})
        self.assertEqual(response.status_code, 200, response.json)
        copy = self.state.notebooks.get(self.root / "copy.gspy")
        self.assertNotEqual(copy.to_json()["document_id"], opened["document_id"])
        self.assertEqual(self.client.post("/api/files/rename", json={"path": str(renamed), "name": "bad.ipynb"}).status_code, 400)
        self.assertTrue(renamed.exists())

    def test_deleted_cell_history_restore_and_authenticated_log_download(self):
        doc = self.state.notebooks.get(self.path)
        cid = doc.add_cell("code", "answer = 42")["id"]
        response = self.client.delete("/api/cells/" + cid, query_string=self.query)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(f"/api/cells/{cid}/history", query_string=self.query).status_code, 200)
        page = self.client.get("/api/notebook/history", query_string=self.query).json
        event = page["events"][-1]
        route = f"/api/notebook/history/{event['id']}/restore"
        response = self.client.post(route, query_string=self.query, json={"expected_revision": page["revision"]})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["source"], "answer = 42")
        self.assertEqual(self.client.post(route, query_string=self.query, json={"expected_revision": page["revision"]}).status_code, 409)
        response = self.client.get("/api/notebook/history", query_string={**self.query, "format": "jsonl"})
        self.assertIn("analysis.history.jsonl", response.headers["Content-Disposition"])
        records = [json.loads(line) for line in response.text.splitlines()]
        self.assertTrue(any(e.get("kind") == "restore" for e in records))
        unauthenticated = self.app.test_client()
        self.assertEqual(unauthenticated.get("/api/notebook/history", query_string=self.query).status_code, 401)

    def test_regular_notebook_history_api_is_unchanged(self):
        regular = self.state.notebooks.get(self.state.notebook_path)
        cid = regular.nb.cells[0]["id"]
        response = self.client.get(f"/api/cells/{cid}/history")
        self.assertEqual(response.json["events"], [])
        self.assertEqual(self.client.get("/api/notebook/history").status_code, 400)

    def test_folder_copy_assigns_new_identity_to_nested_scientific_notebooks(self):
        source = self.root / "project"
        source.mkdir()
        original = self.state.notebooks.get(source / "results.gspy")
        (source / "readme.txt").write_text("Study notes")
        response = self.client.post("/api/files/copy", json={
            "path": str(source), "directory": str(self.root), "name": "project-copy"})
        self.assertEqual(response.status_code, 200, response.json)
        clone = Registry().get(self.root / "project-copy" / "results.gspy")
        self.assertNotEqual(clone.to_json()["document_id"], original.to_json()["document_id"])
        self.assertEqual((self.root / "project-copy" / "readme.txt").read_text(), "Study notes")

    def test_workspace_reopens_scientific_tab_after_server_restart(self):
        opened = self.client.post("/api/open", json={"path": str(self.path)}).json
        close_app(self.app)
        restarted = create_app({"WORK_DIR": str(self.root), "STATE_DIR": str(self.root / "state"),
                                "AUTH_TOKEN": "test-token", "START_WATCHERS": False})
        self.addCleanup(close_app, restarted)
        state = restarted.extensions["gusnotebook"]
        self.assertIn(str(self.path), state.store.current().tabs)
        self.assertEqual(state.notebooks.get(self.path).to_json()["document_id"], opened["document_id"])


if __name__ == "__main__":
    unittest.main()
