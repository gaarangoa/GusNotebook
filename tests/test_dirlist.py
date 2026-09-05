"""Focused filesystem edge cases for the environment directory picker."""

import os
import pathlib
import tempfile
import unittest
from unittest import mock


from gusnotebook import venvs
from gusnotebook.app import create_app, close_app


class DirectoryPickerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({"WORK_DIR": self.temp.name,
                               "STATE_DIR": str(pathlib.Path(self.temp.name) / "state"),
                               "START_WATCHERS": False, "AUTH_TOKEN": "test"})
        self.client = self.app.test_client()
        self.client.post("/auth", headers={"Authorization": "Bearer test"})

    def tearDown(self):
        close_app(self.app)
        self.temp.cleanup()

    def test_inaccessible_child_does_not_abort_parent_listing(self):
        with tempfile.TemporaryDirectory() as root:
            root = pathlib.Path(root)
            (root / "project").mkdir()
            (root / ".Trash-0").mkdir()
            real_python_bin = venvs.python_bin

            def inspect(prefix):
                if pathlib.Path(prefix).name == ".Trash-0":
                    raise PermissionError(13, "Permission denied",
                                          str(pathlib.Path(prefix) / "bin/python3"))
                return real_python_bin(prefix)

            with mock.patch.object(venvs, "python_bin", side_effect=inspect):
                response = self.client.get(
                    "/api/dirlist", query_string={"path": str(root)})

            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual([entry["name"]
                              for entry in response.get_json()["entries"]],
                             ["project"])


if __name__ == "__main__":
    unittest.main()
