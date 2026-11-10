"""Saved value provenance, run recovery and real-kernel capture."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from gusnotebook.notebook import Registry
from gusnotebook.kernel import Kernel
from gusnotebook.persistence import ExternalChangeError


class ContentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "document.gspy"
        self.doc = Registry().get(self.path)
        self.cid = self.doc.add_cell("code", "performance = {'estimate': 0.25}")["id"]
        self.configure(self.cid, results=["performance"], components=[{
            "id": "sentence", "kind": "markdown", "source": "Performance: {{estimate}}"}],
            bindings={"estimate": {"producer": self.cid, "result": "performance", "path": ["estimate"], "format": ".2f"}})

    def configure(self, cid, **changes):
        config = self.doc.cell_json(cid)["content"]
        self.doc.update_content(cid, {**config["definition"], **changes}, config["version"])

    def execute(self, cid=None, value=0.25, outputs=None, generation="kernel-one", status="ok"):
        cid = cid or self.cid
        record = self.doc.begin_run(cid, uuid.uuid4().hex, generation, "/python", self.doc.cell_json(cid)["source"])
        self.doc.finish_run(record, outputs or [], 1, {"status": status, "values": {"performance": {"estimate": value}}})
        return record["run_id"]

    def test_value_link_survives_reopen_and_later_variable_reuse(self):
        run_id = self.execute(self.cid)
        other = self.doc.add_cell("code", "performance = 99")["id"]
        self.execute(other, 99)
        reopened = Registry().get(self.path)
        value = reopened.cell_json(self.cid)["content"]["bindings"]["estimate"]
        self.assertEqual((value["text"], value["run_id"]), ("0.25", run_id))
        self.assertEqual(reopened.inspect_run(run_id)["source"], "performance = {'estimate': 0.25}")
        events = [json.loads(line) for line in reopened.export_history().splitlines()]
        self.assertTrue(any(e.get("values", {}).get("performance") == {"estimate": 0.25} for e in events))

    def test_failed_and_interrupted_reruns_keep_last_success(self):
        saved = self.execute(self.cid)
        for error in ("ValueError", "KeyboardInterrupt"):
            self.execute(self.cid, value=99, outputs=[{"output_type": "error", "ename": error, "evalue": "stopped", "traceback": []}])
            content = self.doc.cell_json(self.cid)["content"]
            self.assertEqual(content["bindings"]["estimate"]["text"], "0.25")
            self.assertEqual(content["run"]["run_id"], saved)
            self.assertEqual(content["run"]["state"], "stale")

    def test_order_upstream_reruns_source_and_restart_invalidate(self):
        self.execute(self.cid)
        other = self.doc.add_cell("code", "dependent = performance")["id"]
        self.execute(other)
        self.assertEqual(self.doc.run_states()[other]["state"], "fresh")
        self.execute(self.cid, 0.5)
        self.assertEqual(self.doc.run_states()[other]["state"], "stale")
        self.execute(other)
        self.doc.update_cell(self.cid, source="performance = {'estimate': 0.75}")
        self.assertTrue(all(s["state"] == "stale" for s in self.doc.run_states().values()))
        self.execute(self.cid)
        self.execute(other)
        self.doc._kernel_generation = "kernel-two"
        self.assertTrue(all(s["state"] == "stale" for s in self.doc.run_states().values()))
        self.execute(self.cid, generation="kernel-two")
        self.execute(other, generation="kernel-two")
        self.doc.move_cell(other, 0)
        self.assertTrue(all(s["state"] == "stale" for s in self.doc.run_states().values()))

    def test_source_changed_during_run_does_not_claim_current_revision(self):
        source = self.doc.cell_json(self.cid)["source"]
        record = self.doc.begin_run(self.cid, "during-edit", "kernel-one", "/python", source)
        self.doc.update_cell(self.cid, source="performance = 99")
        self.doc.finish_run(record, [], 1, {"status": "ok", "values": {"performance": {"estimate": 0.25}}})
        self.assertEqual(self.doc.inspect_run("during-edit")["source"], source)
        self.assertEqual(self.doc.run_states()[self.cid]["state"], "stale")

    def test_save_failure_retains_pending_result_and_retry_does_not_rerun(self):
        record = self.doc.begin_run(self.cid, "save-failure", "kernel-one", "/python", self.doc.cell_json(self.cid)["source"])
        with patch("gusnotebook.gspy.atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.doc.finish_run(record, [], 1, {"status": "ok", "values": {"performance": {"estimate": 0.25}}})
        reopened = Registry().get(self.path)
        self.assertIn("without a saved completion", reopened.run_states()[self.cid]["reason"])
        self.assertEqual(self.doc.run_states()[self.cid]["pending_run_id"], "save-failure")
        self.doc.retry_result("save-failure")
        self.assertEqual(self.doc.run_states()[self.cid]["state"], "fresh")
        self.assertEqual(len(self.doc._receipts()), 1)
        with self.assertRaises(ValueError):
            self.doc.begin_run(self.cid, "save-failure", "kernel-one", "/python", self.doc.cell_json(self.cid)["source"])

    def test_deleted_producer_and_named_outputs_are_explicit(self):
        output = {"output_type": "display_data", "data": {"text/plain": "PCA"}, "metadata": {"gusnotebook": {"result": "pca"}}}
        self.execute(self.cid, outputs=[{"output_type": "stream", "name": "stdout", "text": "extra"}, output])
        self.configure(self.cid, components=[{"id": "plot", "kind": "output", "producer": self.cid, "result": "pca"}])
        content = self.doc.cell_json(self.cid)["content"]
        self.assertEqual(content["components"][0]["outputs"], [output])
        self.assertEqual(content["run"]["state"], "fresh")  # presentation edits don't invalidate Python
        narrative = self.doc.add_cell("markdown", "Estimate {{value}}")["id"]
        self.configure(narrative, bindings={"value": {"producer": self.cid, "result": "performance", "path": ["estimate"]}})
        self.doc.delete_cell(self.cid)
        self.assertEqual(self.doc.cell_json(narrative)["content"]["bindings"]["value"]["state"], "missing")

    def test_completion_after_deletion_is_archived(self):
        record = self.doc.begin_run(self.cid, "deleted", "one", "/python", self.doc.cell_json(self.cid)["source"])
        self.doc.delete_cell(self.cid)
        self.doc.finish_run(record, [], 1, {"status": "ok", "values": {}})
        self.assertIsNone(self.doc.cell_json(self.cid))
        self.assertEqual(self.doc.inspect_run("deleted")["status"], "success")

    def test_configuration_conflict_and_invalid_selector_preserve_disk(self):
        config = self.doc.cell_json(self.cid)["content"]
        original = self.path.read_bytes()
        with self.assertRaises(ExternalChangeError):
            self.doc.update_content(self.cid, config["definition"], "old-version")
        config["definition"]["bindings"]["estimate"]["format"] = "10000000000f"
        with self.assertRaises(ValueError):
            self.doc.update_content(self.cid, config["definition"], config["version"])
        self.assertEqual(self.path.read_bytes(), original)

    def test_review_requires_exact_content_and_runs_and_is_logged(self):
        self.execute(self.cid)
        content = self.doc.cell_json(self.cid)["content"]
        self.assertTrue(content["review_pending"])
        self.doc.review_content(self.cid, content["review_version"])
        self.assertFalse(self.doc.cell_json(self.cid)["content"]["review_pending"])
        self.execute(self.cid, 0.5)
        with self.assertRaises(ExternalChangeError):
            self.doc.review_content(self.cid, content["review_version"])
        self.assertTrue(self.doc.cell_json(self.cid)["content"]["review_pending"])
        self.assertTrue(any(e["kind"] == "review" for e in self.doc.document_history()["events"]))

    def test_cleared_results_are_missing_until_run_again(self):
        self.execute(self.cid)  # captured variables need clearing even without display output
        self.doc.clear_outputs(self.cid)
        self.assertEqual(self.doc.cell_json(self.cid)["content"]["bindings"]["estimate"]["state"], "missing")


class KernelCaptureTests(unittest.TestCase):
    def test_real_kernel_captures_before_namespace_reuse_and_reports_unsupported_values(self):
        with tempfile.TemporaryDirectory() as root:
            kernel = Kernel(root)
            self.addCleanup(kernel.shutdown)
            capture = {"names": ["performance", "bad", "missing", "large", "np_scalar"]}
            count, outputs = kernel.execute("import numpy as np\nperformance = {'estimate': 0.25, 'ci': [0.25, 0.26]}\nbad = object()\nlarge = list(range(5000))\nnp_scalar = np.float64(0.26)\nprint('done')", capture=capture)
            self.assertEqual(count, 1)
            self.assertEqual(capture["status"], "ok")
            self.assertEqual(capture["values"]["performance"]["estimate"], 0.25)
            self.assertEqual(capture["values"]["np_scalar"], 0.26)
            self.assertEqual(set(capture["errors"]), {"bad", "missing", "large"})
            self.assertEqual(outputs[0]["text"], "done\n")
            kernel.execute("performance = 99")
            self.assertEqual(capture["values"]["performance"]["estimate"], 0.25)
            failed = {"names": ["performance"]}
            kernel.execute("raise ValueError('bad run')", capture=failed)
            self.assertEqual(failed["values"], {})
            previous_generation = kernel.generation
            kernel.restart()
            self.assertNotEqual(previous_generation, kernel.generation)

            cancelled = {"names": ["performance"]}
            _, outputs = kernel.execute("import time\nprint('ready', flush=True)\nwhile True: time.sleep(0.05)",
                                        capture=cancelled, on_output=lambda outputs: kernel.interrupt())
            self.assertTrue(any(o.get("ename") == "KeyboardInterrupt" for o in outputs))
            self.assertEqual(cancelled["values"], {})


if __name__ == "__main__":
    unittest.main()
