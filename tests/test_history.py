from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gusnotebook.history import History
from gusnotebook.persistence import ExternalChangeError, atomic_write


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.history = History(self.root / "history")
        self.one, self.two = self.root / "one.py", self.root / "two.html"
        self.one.write_text("before one\n")
        self.two.write_text("<p>before two</p>\n")

    def tearDown(self):
        self.temp.cleanup()

    def record(self):
        group_id = self.history.begin("workspace", "agent", "Edit both documents", [self.one, self.two])
        self.one.write_text("after one\n")
        self.two.write_text("<p>after two</p>\n")
        return self.history.finish(group_id, "workspace")

    def test_group_restores_multiple_documents_and_survives_restart(self):
        group = self.record()
        self.assertEqual(len(group["changes"]), 2)
        self.assertIn("-before one", group["changes"][0]["diff"])
        reloaded = History(self.root / "history")
        reloaded.undo(group["id"], "workspace", group["revision"])
        self.assertEqual(self.one.read_text(), "before one\n")
        self.assertEqual(self.two.read_text(), "<p>before two</p>\n")
        self.assertTrue(reloaded.list("workspace")[0]["undone"])

    def test_conflict_in_second_document_prevents_any_restore(self):
        group = self.record()
        self.two.write_text("newer user edit")
        with self.assertRaises(ExternalChangeError):
            self.history.undo(group["id"], "workspace", group["revision"])
        self.assertEqual(self.one.read_text(), "after one\n")
        self.assertEqual(self.two.read_text(), "newer user edit")

    def test_session_scope_and_active_recording_are_enforced(self):
        group_id = self.history.begin("workspace", "agent", "Edit", [self.one])
        self.assertEqual(self.history.list("other"), [])
        with self.assertRaises(ValueError):
            self.history.finish(group_id, "other")
        with self.assertRaises(ValueError):
            self.history.undo(group_id, "workspace", "revision")

    def test_failed_second_write_rolls_back_the_first(self):
        group = self.record()

        def fail_second(path, *args, **kwargs):
            if Path(path) == self.two:
                raise OSError("Simulated disk failure")
            return atomic_write(path, *args, **kwargs)

        with patch("gusnotebook.history.atomic_write", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "Simulated disk failure"):
                self.history.undo(group["id"], "workspace", group["revision"])
        self.assertEqual(self.one.read_text(), "after one\n")
        self.assertEqual(self.two.read_text(), "<p>after two</p>\n")
        self.assertFalse(self.history.list("workspace")[0]["undone"])

    def test_next_request_finishes_previous_recording_for_that_terminal(self):
        first = self.history.begin("workspace", "agent", "First", [self.one])
        self.one.write_text("first result")
        self.history.begin("workspace", "agent", "Second", [self.one])
        self.one.write_text("second result")
        first_view = next(g for g in self.history.list("workspace") if g["id"] == first)
        self.assertFalse(first_view["active"])
        self.assertIn("first result", first_view["changes"][0]["diff"])
        self.assertNotIn("second result", first_view["changes"][0]["diff"])


if __name__ == "__main__":
    unittest.main()
