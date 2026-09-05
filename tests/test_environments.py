import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from gusnotebook.app import create_app, close_app
from gusnotebook.environments import EnvironmentManager, inspect_packages


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.manager = EnvironmentManager(self.root / "environments.json", uv=sys.executable)

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def request(self, **values):
        return {"name": "analysis", "location": str(self.root), **values}

    def finish(self, job):
        self.manager.jobs[job["id"]]["_thread"].join(timeout=10)
        result = self.manager.get(job["id"], "workspace")
        self.assertIn(result["status"], {"ready", "failed", "cancelled"}, result)
        return result

    def fake_run(self, job, command, log=True):
        if "venv" in command:
            target = Path(job["path"])
            (target / "bin").mkdir()
            (target / "bin/python").symlink_to(sys.executable)
            (target / "pyvenv.cfg").write_text("home = test\n")
            return "Created environment\n"
        return json.dumps({"version": "3.12.0", "packages": [{"name": "ipykernel", "version": "7.0"}]})

    def test_creation_preserves_requirements_and_repository_arguments(self):
        repository = self.root / "local repo's source"
        repository.mkdir()
        (repository / "pyproject.toml").write_text('[project]\nname="sample"\nversion="1.0"\n')
        with patch.object(self.manager, "_run", side_effect=self.fake_run) as runner:
            job = self.manager.create(self.request(packages="numpy>=1,<3\nrequests[socks]==2.32.0",
                repositories=str(repository), editable=True), "workspace")
            result = self.finish(job)
        self.assertEqual(result["status"], "ready", result)
        command = runner.call_args_list[1].args[1]
        self.assertIn("numpy>=1,<3", command)
        self.assertIn("requests[socks]==2.32.0", command)
        self.assertEqual(command[command.index("--editable") + 1], str(repository))
        self.assertIn("ipykernel", command)
        self.assertEqual(command[command.index("--python") + 1], str(self.root / "analysis/bin/python"))
        reloaded = EnvironmentManager(self.root / "environments.json", uv=sys.executable)
        self.assertEqual(reloaded.registered()[0]["prefix"], str(self.root / "analysis"))

    def test_existing_directory_and_invalid_inputs_are_untouched(self):
        existing = self.root / "existing"
        existing.mkdir()
        sentinel = existing / "valuable.txt"
        sentinel.write_text("keep this")
        for body in (self.request(name="existing"), self.request(name="../escape"),
                     self.request(packages="--target /tmp/elsewhere"),
                     self.request(location="relative"),
                     self.request(repositories=str(self.root))):
            with self.subTest(body=body), self.assertRaises((ValueError, OSError)):
                self.manager.create(body, "workspace")
        self.assertEqual(sentinel.read_text(), "keep this")
        self.assertFalse((self.root / "analysis").exists())

    def test_install_failure_removes_only_the_new_environment(self):
        sentinel = self.root / "keep.txt"
        sentinel.write_text("keep this")

        def run(job, command, log=True):
            if "pip" in command:
                raise RuntimeError("No matching version was found")
            return self.fake_run(job, command, log)

        with patch.object(self.manager, "_run", side_effect=run):
            result = self.finish(self.manager.create(self.request(), "workspace"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("No matching version", result["error"])
        self.assertFalse((self.root / "analysis").exists())
        self.assertEqual(sentinel.read_text(), "keep this")
        self.assertEqual(self.manager.registered(), [])

    def test_cancel_stops_the_process_and_cleans_its_environment(self):
        executable = self.root / "fake-uv"
        executable.write_text(f"#!{sys.executable}\nimport time\nprint('started', flush=True)\ntime.sleep(60)\n")
        executable.chmod(0o755)
        self.manager.uv = str(executable)
        job = self.manager.create(self.request(), "workspace")
        deadline = time.monotonic() + 5
        while "started" not in self.manager.get(job["id"], "workspace")["log"]:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        with self.assertRaises(ValueError):
            self.manager.cancel(job["id"], "other-workspace")
        self.manager.cancel(job["id"], "workspace")
        self.assertEqual(self.finish(job)["status"], "cancelled")
        self.assertFalse((self.root / "analysis").exists())

    def test_packages_can_be_inspected_without_uv_or_pip(self):
        packages = inspect_packages(sys.executable)
        self.assertEqual(packages["version"], ".".join(map(str, sys.version_info[:3])))
        self.assertTrue(any(p["name"].lower() == "ipykernel" and p["version"] for p in packages["packages"]))
        folder = inspect_packages(str(Path(sys.executable).parent))
        self.assertEqual(folder["packages"], packages["packages"])


class EnvironmentApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.app = create_app({"WORK_DIR": str(self.root), "STATE_DIR": str(self.root / "state"),
                               "START_WATCHERS": False, "AUTH_TOKEN": "test-token"})
        self.client = self.app.test_client()
        self.client.post("/auth", headers={"Authorization": "Bearer test-token"})

    def tearDown(self):
        close_app(self.app)
        self.temp.cleanup()

    def test_environment_endpoints_require_authentication(self):
        anonymous = self.app.test_client()
        response = anonymous.post("/api/environments", json={"name": "test", "location": str(self.root)})
        self.assertEqual(response.status_code, 401)
        self.assertFalse((self.root / "test").exists())
        self.assertEqual(anonymous.get("/api/environments/packages", query_string={"python": sys.executable}).status_code, 401)
        self.assertEqual(self.client.get("/api/environments").status_code, 200)
        packages = self.client.get("/api/environments/packages", query_string={"python": sys.executable})
        self.assertEqual(packages.status_code, 200, packages.json)
        self.assertTrue(packages.json["packages"])

    def test_running_notebook_cannot_switch_environment(self):
        state = self.app.extensions["gusnotebook"]
        path = state.notebook_path
        before = path.read_bytes()
        lock = state.exec_locks[str(path)] = threading.Lock()
        lock.acquire()
        try:
            response = self.client.post("/api/venv", json={"python": sys.executable})
            self.assertEqual(response.status_code, 409, response.json)
            self.assertEqual(path.read_bytes(), before)
        finally:
            lock.release()


if __name__ == "__main__":
    unittest.main()
