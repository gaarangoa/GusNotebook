from copy import deepcopy
import html
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import nbformat

from gusnotebook.app import close_app, create_app
from gusnotebook import cellhistory
from gusnotebook.notebook import Notebook


class CellHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = create_app({"WORK_DIR": str(self.root), "STATE_DIR": str(self.root / "state"),
                               "AUTH_TOKEN": "test-token", "START_WATCHERS": False})
        self.client = self.app.test_client()
        self.client.environ_base["HTTP_AUTHORIZATION"] = "Bearer test-token"
        self.state = self.app.extensions["gusnotebook"]
        self.path = self.state.notebook_path
        self.doc = self.state.notebooks.get(self.path)
        self.cell = self.doc.to_json()["cells"][0]["id"]
        self.client.post("/api/focus", json={"cell_id": self.cell})

    def tearDown(self):
        close_app(self.app)
        self.temp.cleanup()

    def events(self, cell=None):
        return self.doc.cell_history(cell or self.cell)["events"]

    def prompt(self, text, terminal="agent-a"):
        response = self.client.post("/api/prompt", json={"prompt": text},
                                    headers={"X-Terminal-Id": terminal})
        self.assertEqual(response.status_code, 200, response.json)

    def edit(self, source, terminal="agent-a", **extra):
        response = self.client.patch("/api/cells/" + self.cell,
            json={"source": source, "undoable": True, **extra}, headers={"X-Terminal-Id": terminal})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def test_full_requests_survive_edit_undo_restart_and_workspace_pruning(self):
        for index in range(24):
            self.prompt(f"Request {index}\n" + "Preserve this preference. " * 30)
            caption = self.edit(f"value = {index}")["claude_prompt"]
            self.assertLessEqual(len(caption), 400)
        self.client.post(f"/api/cells/{self.cell}/undo")
        reloaded = Notebook(self.path)
        requests = [e for e in reloaded.cell_history(self.cell)["events"] if e["kind"] == "request"]
        self.assertEqual(len(requests), 24)
        self.assertIn("Request 0\n", requests[0]["prompt"])
        self.assertGreater(len(requests[0]["prompt"]), 400)
        self.assertEqual(self.events()[-1]["kind"], "undo")
        self.assertEqual(len(self.state.history.groups), 20)

    def test_requests_without_edits_and_terminal_attribution(self):
        self.prompt("Use medians instead of means", "agent-a")
        self.prompt("Change the plot color", "agent-b")
        self.edit("median = 10", "agent-a")
        self.assertEqual(self.events()[-1]["prompt"], "Use medians instead of means")
        self.assertEqual(self.events()[-1]["terminal"], "agent-a")
        self.assertEqual(len([e for e in self.events() if e["kind"] == "request"]), 2)
        self.edit("manual = True", "unknown-terminal")
        self.assertNotIn("prompt", self.events()[-1])

    def test_browser_edit_does_not_inherit_an_agent_request(self):
        self.prompt("An earlier agent request")
        response = self.client.patch(f"/api/cells/{self.cell}", json={"source": "typed = 1", "undoable": True},
                                     headers={"X-Client-Id": "browser"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.events()[-1]["actor"], "User")
        self.assertNotIn("prompt", self.events()[-1])

    def test_preferences_capture_actual_terminal_launch_and_later_changes(self):
        launch = {"workspace_instructions": "Use medians", "session_instructions": "", "restrictions": {}}
        terminal = SimpleNamespace(kind="claude", audit_preferences=deepcopy(launch))
        with patch.object(self.state.terms, "get", return_value=terminal):
            self.client.post("/api/settings", json={"claude_instructions": "Use means"})
            self.prompt("Revisit the analysis")
        request = next(e for e in self.events() if e["kind"] == "request")
        self.assertEqual(request["preferences"], launch)
        self.assertEqual(request["preferences_source"], "Terminal launch")
        self.assertTrue(any(e["kind"] == "settings" and e["preferences"]["workspace_instructions"] == "Use means"
                            for e in self.events()))

    def test_external_edit_preserves_history_and_records_source_diff(self):
        self.prompt("Keep this decision")
        self.edit("before = 1")
        saved = nbformat.read(self.path, 4)
        saved.cells[0].metadata.clear()
        saved.cells[0].source = "after = 2"
        nbformat.write(saved, self.path)
        self.doc.load()
        self.assertEqual(self.events()[-1]["kind"], "external_edit")
        self.assertIn("after = 2", self.events()[-1]["diff"])
        self.assertTrue(any(e.get("prompt") == "Keep this decision" for e in self.events()))
        self.assertEqual(len(self.events()), len(Notebook(self.path).cell_history(self.cell)["events"]))

    def test_legacy_caption_is_honest_and_new_requests_do_not_duplicate_it(self):
        _, cell = self.doc.find(self.cell)
        cell.metadata["claude_prompt"] = "Old shortened request…"
        self.doc.save()
        self.assertEqual(self.events()[0]["kind"], "legacy_request")
        self.assertIsNone(self.events()[0]["at"])
        self.prompt("New request")
        self.edit("new = 1")
        self.assertEqual([e["kind"] for e in self.events()].count("request"), 1)
        self.assertEqual([e["kind"] for e in self.events()].count("legacy_request"), 1)

    def test_export_requires_a_copy_receipt_and_is_deduplicated_after_restart(self):
        snapshot = "a" * 32
        target = self.root / "report.html"
        source = (f'<figure data-gusnb-provenance="1" data-gusnb-notebook="{html.escape(str(self.path))}" '
                  f'data-gusnb-cell-id="{self.cell}" data-gusnb-snapshot="{snapshot}">Result</figure>')
        target.write_text(source)
        self.state.texts.get(target)
        self.state.observe_artifacts()
        self.assertFalse(any(e["kind"] == "export" for e in self.events()))
        copied = self.client.post(f"/api/cells/{self.cell}/history", json={
            "kind": "copy", "snapshot_id": snapshot, "output_mime": "text/html", "source": "value = 1"})
        self.assertEqual(copied.status_code, 200, copied.json)
        self.state.observe_artifacts()
        self.assertEqual(self.events()[-1]["destination"], str(target))
        self.assertEqual(self.events()[-1]["snapshot_id"], snapshot)
        self.state.artifact_versions.clear()
        self.state.observe_artifacts()
        self.assertEqual(sum(e["kind"] == "export" for e in self.events()), 1)
        reloaded = Notebook(self.path)
        reloaded.record_export(self.cell, snapshot, str(target))
        self.assertEqual(sum(e["kind"] == "export" for e in reloaded.cell_history(self.cell)["events"]), 1)

    def test_history_is_scoped_by_notebook_and_failed_saves_do_not_append(self):
        other = Notebook(self.root / "other.ipynb")
        other.load()
        other._nb.cells[0]["id"] = self.cell
        other.save()
        self.client.post(f"/api/cells/{self.cell}/history", json={"note": "Only this notebook"})
        response = self.client.get(f"/api/cells/{self.cell}/history", query_string={"notebook": str(other.path)})
        self.assertEqual(response.json["events"], [])
        count = len(self.events())
        with patch.object(self.doc, "_save", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                self.doc.record_history(self.cell, "note", note="Not saved")
        self.assertEqual(len(self.events()), count)

    def test_execution_records_source_and_output_digest_without_copying_large_outputs(self):
        outputs = [{"output_type": "stream", "name": "stdout", "text": "x" * 10000}]
        self.doc.set_outputs(self.cell, outputs, 1, executed_source="print(data)")
        event = self.events()[-1]
        self.assertEqual(event["kind"], "run")
        self.assertEqual(event["source"], "print(data)")
        self.assertEqual(event["output"]["count"], 1)
        self.assertNotIn("x" * 1000, str(event))
        self.doc.clear_outputs(self.cell)
        self.assertEqual(self.events()[-1]["kind"], "clear")
        nbformat.validate(nbformat.read(self.path, 4))


if __name__ == "__main__":
    unittest.main()
