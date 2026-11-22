"""Opt-in real isolation, replay and package tests: GUSNOTEBOOK_TEST_DOCKER=1."""

import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from gusnotebook.projects import ProjectManager


@unittest.skipUnless(os.environ.get("GUSNOTEBOOK_TEST_DOCKER") == "1", "requires local Docker")
class ContainerProjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        cls.manager = ProjectManager(cls.root / "state")
        cls.pid = cls.manager.create("container_test", str(cls.root))["id"]
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

    def test_worker_has_no_host_credentials_network_or_broker_writes(self):
        secret = self.root / "never-approved.csv"
        secret.write_text("private\n12345\n")
        code = '''import os, socket
from pathlib import Path
def analyze(data, parameters):
    blocked = {}
    for name, path in parameters['paths'].items():
        try: Path(path).read_bytes(); blocked[name] = False
        except OSError: blocked[name] = True
    try:
        socket.create_connection(('1.1.1.1', 443), timeout=1); blocked['network'] = False
    except OSError: blocked['network'] = True
    try:
        Path('/analysis/request.json').write_text('tampered'); blocked['readonly'] = False
    except OSError: blocked['readonly'] = True
    blocked['credential'] = 'GUSNOTEBOOK_TEST_SECRET' not in os.environ
    blocked['selected_data'] = len(data['measurements']) == 6
    return {'title':'Isolation checks','metrics':blocked}
'''
        os.environ["GUSNOTEBOOK_TEST_SECRET"] = "must-not-enter-worker"
        try:
            self.manager.save_analysis(self.pid, "isolation", {"paths": {
                "host": str(secret), "registry": str(self.root / "state/registry.json"),
                "socket": "/var/run/docker.sock", "sibling": "/inputs/never-approved.csv"}}, ["measurements"], code)
            self.manager.run(self.pid, "isolation")
            project = self.wait()
            self.assertEqual(project["job"]["status"], "completed", project["job"])
            result = json.loads(self.manager.artifact(self.pid, project["runs"][-1]["id"], "result.json"))
            self.assertTrue(all(result["metrics"].values()), result)
        finally:
            os.environ.pop("GUSNOTEBOOK_TEST_SECRET", None)

    def test_cancel_terminates_only_the_worker_and_keeps_project(self):
        self.manager.save_analysis(self.pid, "cancelled", {}, ["measurements"],
                                   "import time\ndef analyze(data, parameters):\n    time.sleep(120)\n")
        self.manager.run(self.pid, "cancelled")
        time.sleep(.5)
        self.manager.cancel(self.pid)
        project = self.wait()
        self.assertEqual(project["job"]["status"], "cancelled", project["job"])
        self.assertTrue((self.root / "container_test/reports/deck.html").exists())


if __name__ == "__main__":
    unittest.main()
