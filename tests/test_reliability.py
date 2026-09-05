"""Persistence, authorization and lifecycle regressions without external services."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import nbformat

from gusnotebook.app import create_app, close_app
from gusnotebook.notebook import Notebook, NotebookReadError
from gusnotebook.persistence import ExternalChangeError
from gusnotebook.textfile import TextFile


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_corrupt_notebook_is_never_replaced(self):
        path = self.root / "work.ipynb"
        original = '{"cells": [{"source": "valuable work"}'
        path.write_text(original)
        doc = Notebook(path)
        with self.assertRaises(NotebookReadError):
            doc.load()
        with self.assertRaises(NotebookReadError):
            doc.add_cell(source="new")
        self.assertEqual(path.read_text(), original)

    def test_failed_reload_preserves_last_valid_document_and_can_recover(self):
        path = self.root / "work.ipynb"
        doc = Notebook(path)
        doc.load()
        doc.add_cell(source="precious = 1")
        original = path.read_text()
        path.write_text("partial external write")
        with self.assertRaises(NotebookReadError):
            doc.save()
        self.assertEqual(doc._nb.cells[-1].source, "precious = 1")
        self.assertEqual(path.read_text(), "partial external write")
        path.write_text(original)
        doc.load()
        doc.add_cell(source="recovered = True")
        self.assertEqual(nbformat.read(path, 4).cells[-1].source, "recovered = True")

    def test_deleted_notebook_is_not_recreated_by_autosave(self):
        doc = Notebook(self.root / "work.ipynb")
        doc.load()
        doc.path.unlink()
        with self.assertRaises(NotebookReadError):
            doc.add_cell(source="new")
        self.assertFalse(doc.path.exists())

    def test_atomic_save_preserves_file_permissions(self):
        path = self.root / "tool.py"
        path.write_text("print(1)")
        path.chmod(0o755)
        doc = TextFile(path)
        doc.load()
        doc.save("print(2)")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)

    def test_stale_text_save_preserves_external_edit(self):
        path = self.root / "tool.py"
        path.write_text("base")
        doc = TextFile(path)
        version = doc.to_json()["disk_version"]
        path.write_text("external")
        with self.assertRaises(ExternalChangeError):
            doc.save("browser", version)
        self.assertEqual(path.read_text(), "external")

    def test_stale_cell_edit_and_unchanged_save(self):
        doc = Notebook(self.root / "work.ipynb")
        doc.load()
        cell = doc.add_cell(source="base")
        version = doc._version
        doc.update_cell(cell["id"], source="base", expected_source="base")
        self.assertEqual(version, doc._version)
        doc.update_cell(cell["id"], source="agent", expected_source="base")
        with self.assertRaises(ExternalChangeError):
            doc.update_cell(cell["id"], source="browser", expected_source="base")
        self.assertEqual(doc.cell_json(cell["id"])["source"], "agent")

    def test_import_and_help_have_no_filesystem_or_thread_side_effects(self):
        env = {**os.environ, "GUSNOTEBOOK_HOME": str(self.root / "state")}
        subprocess.run([sys.executable, "-c", "import gusnotebook.app"],
                       cwd=self.root, env=env, check=True)
        subprocess.run([sys.executable, "-m", "gusnotebook", "--help"],
                       cwd=self.root, env=env, check=True, capture_output=True)
        self.assertEqual(list(self.root.iterdir()), [])


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.app = self.make_app(self.root)
        self.state = self.app.extensions["gusnotebook"]
        self.client = self.app.test_client()
        self.client.post("/auth", headers={"Authorization": "Bearer test-token"})

    def make_app(self, root, **config):
        root.mkdir(exist_ok=True)
        return create_app({"WORK_DIR": str(root), "STATE_DIR": str(root / "state"),
                           "START_WATCHERS": False, "AUTH_TOKEN": "test-token", **config})

    def tearDown(self):
        close_app(self.app)
        self.temp.cleanup()

    def test_authentication_host_and_origin_are_required(self):
        self.assertEqual(self.app.test_client().get("/api/files").status_code, 401)
        self.assertEqual(self.client.get("/api/files", headers={"Host": "evil.invalid"}).status_code, 400)
        response = self.client.post("/api/files/new", json={"directory": str(self.root), "name": "bad.py"},
                                    headers={"Origin": "https://evil.invalid"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse((self.root / "bad.py").exists())
        self.assertEqual(self.client.get("/api/tabs").status_code, 200)

    def test_forwarded_headers_are_not_trusted_by_default(self):
        response = self.client.get("/", headers={"X-Forwarded-Host": "evil.invalid",
                                                "X-Forwarded-Prefix": "/evil"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'/evil/static', response.data)

    def test_url_prefix_supports_browser_auth_static_and_api(self):
        for trust_proxy in (False, True):
            with self.subTest(trust_proxy=trust_proxy):
                with patch.dict(os.environ, {"APP_BASE_URL": "/some/prefix/"}):
                    app = self.make_app(self.root / f"prefix-{trust_proxy}", TRUST_PROXY=trust_proxy)
                try:
                    headers = {"X-Forwarded-Prefix": "/some/prefix"} if trust_proxy else {}
                    client = app.test_client()
                    for path in ("/some/prefix", "/some/prefix/"):
                        page = client.get(path, headers=headers)
                        self.assertEqual(page.status_code, 200)
                        self.assertIn(b'data-base="/some/prefix"', page.data)
                        self.assertIn(b'/some/prefix/static/js/auth.js', page.data)
                    self.assertEqual(client.get("/some/prefix/api/tabs", headers=headers).status_code, 401)
                    login = client.post("/some/prefix/auth", headers={
                        **headers, "Authorization": "Bearer test-token"})
                    self.assertEqual(login.status_code, 200)
                    self.assertIn("Path=/some/prefix;", login.headers["Set-Cookie"])
                    page = client.get("/some/prefix/", headers=headers)
                    self.assertIn(b'const BASE = "/some/prefix";', page.data)
                    with client.get("/some/prefix/static/js/core.js", headers=headers) as asset:
                        self.assertEqual(asset.status_code, 200)
                    self.assertEqual(client.get("/some/prefix/api/tabs", headers=headers).status_code, 200)
                    # Internal CLI callers can continue using the unprefixed URL.
                    self.assertEqual(client.get("/api/tabs", headers={
                        "Authorization": "Bearer test-token"}).status_code, 200)
                finally:
                    close_app(app)

    def test_url_prefix_only_matches_complete_path_segments(self):
        app = self.make_app(self.root / "prefix-boundary", APP_BASE_URL="/some/prefix")
        app.add_url_rule("/some/prefix-extra", "sibling", lambda: "Sibling route")
        try:
            response = app.test_client().get("/some/prefix-extra", headers={
                "Authorization": "Bearer test-token"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, "Sibling route")
        finally:
            close_app(app)

    def test_rewritten_proxy_prefix_reaches_generated_urls(self):
        app = self.make_app(self.root / "rewritten-prefix", TRUST_PROXY=True, APP_BASE_URL="")
        try:
            response = app.test_client().get("/", headers={"X-Forwarded-Prefix": "/some/prefix"})
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'data-base="/some/prefix"', response.data)
        finally:
            close_app(app)

    def test_focused_notebook_cell_is_available_to_an_agent(self):
        notebook = self.client.get("/api/notebook").json
        cell = notebook["cells"][0]
        response = self.client.post("/api/focus", json={"notebook": notebook["path"],
                                                        "cell_id": cell["id"]})
        self.assertEqual(response.status_code, 200)
        here = self.client.get("/api/here").json
        self.assertEqual(here["cell_id"], cell["id"])
        self.assertEqual(here["notebook"], notebook["path"])
        self.assertEqual(self.state.markup_focuses, {})

    def test_prompt_starts_a_group_for_the_current_workspace(self):
        response = self.client.post("/api/prompt", json={"prompt": "Update the analysis"},
                                    headers={"X-Terminal-Id": "test-agent"})
        self.assertEqual(response.status_code, 200)
        groups = self.client.get("/api/history").json["groups"]
        self.assertEqual(groups[0]["prompt"], "Update the analysis")
        self.assertEqual(groups[0]["terminal"], "test-agent")

    def test_rename_preserves_kernel_document_and_focus(self):
        old = self.state.notebook_path
        new = old.with_name("renamed.ipynb")
        doc = self.state.notebooks.peek(old)
        kernel = Mock(python=sys.executable, status="idle")
        self.state.kernels._kernels[str(old)] = kernel
        self.state.focuses["test"] = {"notebook": str(old), "cell_id": "cell"}
        response = self.client.post("/api/files/rename", json={"path": str(old), "name": new.name})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIs(self.state.kernels.peek(new), kernel)
        self.assertIs(self.state.notebooks.peek(new), doc)
        self.assertEqual(doc.path, new)
        self.assertEqual(self.state.focuses["test"]["notebook"], str(new))
        kernel.shutdown.assert_not_called()
        doc.add_cell(source="still the same document")
        self.assertFalse(old.exists())

    def test_running_notebook_cannot_be_renamed(self):
        old = self.state.notebook_path
        lock = self.state.exec_locks[str(old)] = threading.Lock()
        lock.acquire()
        try:
            response = self.client.post("/api/files/rename", json={"path": str(old), "name": "new.ipynb"})
            self.assertEqual(response.status_code, 409)
            self.assertTrue(old.exists())
        finally:
            lock.release()

    def test_app_instances_do_not_share_documents_events_or_credentials(self):
        other = self.make_app(self.root / "other")
        try:
            second = other.extensions["gusnotebook"]
            self.assertNotEqual(self.state.notebooks.paths(), second.notebooks.paths())
            listener = second.bus.subscribe()
            self.state.bus.publish("test")
            self.assertTrue(listener.empty())
            saved = self.client.post("/api/settings", json={"gateway_key": "private-key"})
            self.assertEqual(saved.status_code, 200, saved.data)
            client = other.test_client()
            response = client.get("/api/settings", headers={"Authorization": "Bearer test-token"})
            self.assertEqual(response.json["settings"]["gateway_key"], "")
        finally:
            close_app(other)


if __name__ == "__main__":
    unittest.main()
