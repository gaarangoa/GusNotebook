"""Real uv preparation, replay and package tests: GUSNOTEBOOK_TEST_UV=1."""

import json
import os
from pathlib import Path
import tempfile
import subprocess
import time
import unittest

from gusnotebook.projects import ProjectManager


@unittest.skipUnless(os.environ.get("GUSNOTEBOOK_TEST_UV") == "1", "downloads a uv project environment")
class UvProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        cls.manager = ProjectManager(cls.root / "state")
        cls.pid = cls.manager.create("uv_test", str(cls.root))["id"]
        source = cls.root / "measurements.csv"
        source.write_text("x,y,z\n1,8,3\n2,4,9\n3,7,5\n4,3,1\n5,2,8\n6,6,4\n")
        cls.manager.sources(cls.pid, [{"id": "measurements", "path": str(source)}])
        cls.manager.prepare(cls.pid)
        cls.manager.jobs[cls.pid]["_thread"].join(timeout=300)
        job = cls.manager.view(cls.pid)["job"]
        if job["status"] != "completed":
            cls.manager.close()
            cls.temporary.cleanup()
            raise AssertionError(job)

    @classmethod
    def tearDownClass(cls):
        cls.manager.close()
        cls.temporary.cleanup()

    def wait(self):
        self.manager.jobs[self.pid]["_thread"].join(timeout=60)
        return self.manager.view(self.pid)

    def test_scientific_replay_and_installed_package(self):
        self.manager.save_analysis(self.pid, "pca", {"source": "measurements", "columns": ["x", "y", "z"]}, ["measurements"])
        self.manager.run(self.pid, "pca")
        project = self.wait()
        self.assertEqual(project["job"]["status"], "completed", project["job"])
        baseline = project["runs"][-1]["id"]
        # A linked .ipynb must execute its visible code with its selected uv
        # interpreter, including from the per-run notebook directory.
        for path in (self.root / "uv_test/notebooks/pca.ipynb",
                     self.root / f"uv_test/results/runs/{baseline}/notebook.ipynb"):
            notebook = json.loads(path.read_text())
            source = "".join(notebook["cells"][1]["source"])
            execution = subprocess.run([notebook["metadata"]["kernelspec"]["notebook_python"], "-c",
                                        source + "\nimport json\nprint(json.dumps(result))"],
                                       cwd=path.parent, capture_output=True, text=True, timeout=30)
            self.assertEqual(execution.returncode, 0, execution.stderr)
            self.assertEqual(json.loads(execution.stdout), json.loads(self.manager.artifact(self.pid, baseline, "result.json")))
        self.manager.accept(self.pid, baseline, project["report_revision"])
        self.manager.run(self.pid, "pca", replay=baseline)
        replay = self.wait()
        self.assertTrue(replay["runs"][-1]["comparison"]["passed"], replay["job"])
        self.manager.run(self.pid, "pca", package=True)
        packaged = self.wait()
        self.assertEqual(packaged["job"]["status"], "completed", packaged["job"])
        self.assertTrue(packaged["runs"][-1]["package"]["comparison"]["passed"])
        self.assertTrue(packaged["runs"][-1]["package"]["generality"]["passed"])
        self.assertIn("scikit-learn", [p["name"] for p in packaged["runs"][-1]["installed"]["packages"]])

    def test_worker_uses_project_python_and_does_not_inherit_app_tokens(self):
        code = '''import os, sys
from pathlib import Path
def analyze(data, parameters):
    return {'metrics': {'app_token_absent': 'NB_TOKEN' not in os.environ,
                        'secret_absent': 'GUSNOTEBOOK_TEST_SECRET' not in os.environ,
                        'project_python': str(Path(sys.prefix)),
                        'selected_data': len(data['measurements'])}}
'''
        from unittest.mock import patch
        with patch.dict(os.environ, {"NB_TOKEN": "must-not-enter-worker", "GUSNOTEBOOK_TEST_SECRET": "secret"}):
            self.manager.save_analysis(self.pid, "runtime", {}, ["measurements"], code)
            self.manager.run(self.pid, "runtime")
            project = self.wait()
            self.assertEqual(project["job"]["status"], "completed", project["job"])
            result = json.loads(self.manager.artifact(self.pid, project["runs"][-1]["id"], "result.json"))["metrics"]
            self.assertTrue(result["app_token_absent"])
            self.assertTrue(result["secret_absent"])
            self.assertEqual(result["project_python"], project["environment"]["path"])
            self.assertEqual(result["selected_data"], 6)

    def test_cancel_terminates_only_the_worker_and_keeps_project(self):
        self.manager.save_analysis(self.pid, "cancelled", {}, ["measurements"],
                                   "import time\ndef analyze(data, parameters):\n    time.sleep(120)\n")
        self.manager.run(self.pid, "cancelled")
        time.sleep(.5)
        self.manager.cancel(self.pid)
        project = self.wait()
        self.assertEqual(project["job"]["status"], "cancelled", project["job"])
        self.assertTrue((self.root / "uv_test/reports/deck.html").exists())


if __name__ == "__main__":
    unittest.main()
