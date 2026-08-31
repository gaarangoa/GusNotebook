"""Focused filesystem edge cases for the environment directory picker."""

import os
import pathlib
import tempfile
import unittest
from unittest import mock


# Importing the app creates its persistent stores, so keep this test isolated
# from the user's real GusNotebook state.
_STATE = tempfile.TemporaryDirectory()
os.environ["GUSNOTEBOOK_HOME"] = _STATE.name

from gusnotebook import venvs  # noqa: E402
from gusnotebook.app import app  # noqa: E402


class DirectoryPickerTests(unittest.TestCase):
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
                response = app.test_client().get(
                    "/api/dirlist", query_string={"path": str(root)})

            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual([entry["name"]
                              for entry in response.get_json()["entries"]],
                             ["project"])


if __name__ == "__main__":
    unittest.main()
