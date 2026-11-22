"""Project authority, scientific comparison and lifecycle regressions."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gusnotebook.app import create_app, close_app
from gusnotebook.project_files import ProjectError, read_file
from gusnotebook.projects import ProjectManager, compare_results, validate_result
from gusnotebook.project_templates import PCA_CODE


class FixtureRunner:
    def prepare(self, directory, specification, lock, cancel):
        return {"image": "sha256:fixture"}, b"fixture-lock"

    def execute(self, image, snapshot, sources, cancel):
        request = json.loads(read_file(Path(snapshot) / "request.json"))
        if request["parameters"].get("fail"):
            raise ProjectError("deliberate fixture failure")
        return {"result": {"title": "Fixture", "metrics": {"value": request["parameters"].get("value", 1)}},
                "environment": {"python": "fixture", "packages": []}, "log": "fixture output"}

    def close(self):
        pass


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.manager = ProjectManager(self.root / "state", FixtureRunner())
        self.project = self.manager.create("research", str(self.root))
        self.pid = self.project["id"]
        self.source = self.root / "data.csv"
        self.source.write_text("a,b\n1,4\n2,2\n3,5\n")
        self.manager.sources(self.pid, [{"id": "data", "path": str(self.source)}])

    def tearDown(self):
        self.manager.close()
        self.temporary.cleanup()

    def finish(self):
        self.manager.jobs[self.pid]["_thread"].join(timeout=10)
        return self.manager.view(self.pid)

    def prepare(self):
        self.manager.prepare(self.pid)
        self.assertEqual(self.finish()["job"]["status"], "completed")

    def run_analysis(self, parameters=None):
        self.manager.save_analysis(self.pid, "analysis", parameters or {}, ["data"])
        self.manager.run(self.pid, "analysis")
        return self.finish()

    def test_scaffold_cannot_replace_existing_folder_or_state(self):
        self.assertTrue((self.root / "research/reports/deck.html").exists())
        for name, parent in (("research", self.root), ("state", self.root), ("nested", self.root / "state"), ("../escape", self.root)):
            with self.assertRaises(ProjectError):
                self.manager.create(name, str(parent))
        ignored = (self.root / "research/.gitignore").read_text()
        self.assertIn("data/", ignored)
        self.assertIn("audit/", ignored)

    def test_snapshots_are_exact_and_manifest_cannot_widen_grants(self):
        source = self.manager.projects[self.pid]["sources"][0]
        snapshot = read_file(source["snapshot"])
        self.source.write_text("changed")
        self.assertEqual(read_file(source["snapshot"]), snapshot)
        manifest = self.root / "research/gusproject.json"
        manifest.write_text('{"sources":[{"id":"secret","path":"/etc/passwd"}]}')
        with self.assertRaises(ProjectError):
            self.manager.save_analysis(self.pid, "analysis", {}, ["secret"])
        reloaded = ProjectManager(self.root / "state", FixtureRunner())
        self.assertEqual([s["id"] for s in reloaded.view(self.pid)["sources"]], ["data"])
        reloaded.close()

    def test_symlink_and_parent_symlink_sources_rejected(self):
        alias = self.root / "alias.csv"
        alias.symlink_to(self.source)
        directory = self.root / "alias-directory"
        directory.symlink_to(self.root, target_is_directory=True)
        for path in (alias, directory / "data.csv"):
            with self.assertRaises(OSError):
                self.manager.sources(self.pid, [{"id": "alias", "path": str(path)}])
        self.assertEqual(len(self.manager.view(self.pid)["sources"]), 1)

    def test_replaced_source_during_read_is_rejected(self):
        actual = os.fstat
        count = [0]
        def stat(fd):
            result = actual(fd)
            count[0] += 1
            if count[0] == 1:
                replacement = self.root / "replacement.csv"
                replacement.write_text("different\n1\n")
                replacement.replace(self.source)
            return result
        with patch("gusnotebook.project_files.os.fstat", side_effect=stat), self.assertRaises(ProjectError):
            read_file(self.source)

    def test_failed_candidate_preserves_baseline_and_report(self):
        self.prepare()
        p = self.run_analysis()
        accepted = p["runs"][-1]["id"]
        self.manager.accept(self.pid, accepted, p["report_revision"])
        before = self.manager.report(self.pid)
        failed = self.run_analysis({"fail": True})
        self.assertEqual(failed["runs"][-1]["status"], "failed")
        self.assertEqual(failed["analyses"]["analysis"]["accepted_run"], accepted)
        self.assertEqual(before, self.manager.report(self.pid))

    def test_compare_replay_and_concurrent_report_edit(self):
        self.prepare()
        p = self.run_analysis({"value": 1})
        accepted = p["runs"][-1]["id"]
        self.manager.accept(self.pid, accepted, p["report_revision"])
        p = self.run_analysis({"value": 2})
        candidate = p["runs"][-1]
        self.assertFalse(candidate["comparison"]["passed"])
        self.assertIn("parameters", candidate["comparison"]["lineage_changes"])
        report = self.manager.report(self.pid)
        self.manager.report(self.pid, report["html"].replace("Write your story", "Human edit"), report["revision"])
        with self.assertRaises(ProjectError):
            self.manager.accept(self.pid, candidate["id"], report["revision"])
        self.manager.run(self.pid, "analysis", replay=accepted)
        p = self.finish()
        self.assertTrue(p["runs"][-1]["comparison"]["passed"])
        self.assertEqual(p["runs"][-1]["parameters"], {"value": 1})
        self.assertIn("Human edit", self.manager.report(self.pid)["html"])

    def test_revocation_blocks_runs_and_replays(self):
        self.prepare()
        p = self.run_analysis()
        run = p["runs"][-1]["id"]
        self.manager.revoke(self.pid, "data")
        self.manager.run(self.pid, "analysis", replay=run)
        self.assertEqual(self.finish()["job"]["status"], "failed")

    def test_other_project_cannot_read_or_accept_run(self):
        self.prepare()
        p = self.run_analysis()
        second = self.manager.create("other", str(self.root))
        with self.assertRaises(ProjectError):
            self.manager.artifact(second["id"], p["runs"][-1]["id"], "result.json")
        with self.assertRaises(ProjectError):
            self.manager.accept(second["id"], p["runs"][-1]["id"], second["report_revision"])

    def test_manual_generated_content_change_is_identified(self):
        self.prepare()
        p = self.run_analysis()
        self.manager.accept(self.pid, p["runs"][-1]["id"], p["report_revision"])
        p = self.manager.view(self.pid)
        self.assertFalse(p["analyses"]["analysis"]["report_modified"])
        report = self.manager.report(self.pid)
        self.manager.report(self.pid, report["html"].replace('"value": 1', '"value": 99'), report["revision"])
        self.assertTrue(self.manager.view(self.pid)["analyses"]["analysis"]["report_modified"])

    def test_chat_discloses_schema_but_no_rows_and_cannot_expand_scope(self):
        seen = []
        def model(messages, context):
            seen.append(context)
            return {"message": "Proposed analysis", "action": "analysis", "source_ids": ["secret"], "parameters": {}, "code": PCA_CODE}
        self.manager.chat(self.pid, "Do PCA", "analysis", ["data"], model)
        p = self.finish()
        self.assertEqual(p["job"]["status"], "failed")
        self.assertNotIn(str(self.source), json.dumps(seen))
        self.assertEqual(seen[0]["sources"][0]["schema"][0]["name"], "a")
        self.assertNotIn("analyses", seen[0])
        self.assertIn("user_message", [e["kind"] for e in p["events"]])
        self.assertIn("assistant_message", [e["kind"] for e in p["events"]])

    def test_chat_saves_and_runs_proposal_without_granting_model_acceptance(self):
        self.prepare()
        self.manager.chat(self.pid, "Analyze", "analysis", ["data"], lambda *_: {
            "action": "analysis", "message": "I propose a PCA", "parameters": {}, "source_ids": ["data"], "code": PCA_CODE})
        p = self.finish()
        self.assertEqual(p["job"]["status"], "completed")
        self.assertIsNone(p["analyses"]["analysis"]["accepted_run"])
        self.assertTrue(p["analyses"]["analysis"]["candidate_run"])

    def test_agent_report_prose_is_escaped_and_audited(self):
        self.manager.chat(self.pid, "Draft a deck", "analysis", [], lambda *_: {
            "action": "report", "message": "Drafted two sections", "slides": [
                {"title": "Introduction", "paragraphs": ["<script>not executable</script>"]},
                {"title": "Methods", "paragraphs": ["Analysis pending review."]}]})
        p = self.finish()
        self.assertEqual(p["job"]["status"], "completed", p["job"])
        report = self.manager.report(self.pid)["html"]
        self.assertIn("&lt;script&gt;not executable&lt;/script&gt;", report)
        self.assertIn("<h2>Methods</h2>", report)
        self.assertIn("report_edited", [e["kind"] for e in p["events"]])

    def test_model_does_not_overwrite_concurrent_human_code_edit(self):
        self.manager.save_analysis(self.pid, "analysis", {}, ["data"])
        path = self.root / "research/code/src/project_analysis/analysis.py"
        def propose(*_):
            path.write_text("# human edit\n" + PCA_CODE)
            return {"action": "analysis", "message": "Update", "parameters": {}, "source_ids": ["data"], "code": PCA_CODE}
        self.manager.chat(self.pid, "Update", "analysis", ["data"], propose)
        self.assertEqual(self.finish()["job"]["status"], "failed")
        self.assertTrue(path.read_text().startswith("# human edit"))

    def test_corrupt_registry_fails_closed_without_overwriting_it(self):
        self.manager.registry.write_text("broken data")
        other = ProjectManager(self.root / "state", FixtureRunner())
        with self.assertRaises(ProjectError):
            other.list()
        with self.assertRaises(ProjectError):
            other.create("unavailable", str(self.root))
        self.assertEqual(self.manager.registry.read_text(), "broken data")
        other.close()

    def test_missing_docker_has_no_host_execution_fallback(self):
        from gusnotebook.project_runner import ContainerRunner
        runner = ContainerRunner()
        runner.docker = None
        self.assertFalse(runner.status()["available"])
        with self.assertRaises(ProjectError):
            runner.prepare(self.root / "build", "irrelevant", None)
        runner.close()

    def test_remote_docker_cannot_mount_unverified_remote_paths(self):
        from gusnotebook.project_runner import ContainerRunner
        runner = ContainerRunner()
        with patch.dict(os.environ, {"DOCKER_HOST": "tcp://other-host:2375", "DOCKER_CONTEXT": ""}), self.assertRaises(ProjectError):
            runner.execute("sha256:fixture", self.root, [])
        runner.close()

    def test_result_validation_and_fixed_tolerance(self):
        self.assertTrue(compare_results({"x": 1.0}, {"x": 1 + 1e-8})["passed"])
        self.assertFalse(compare_results({"x": [1, 2]}, {"x": [1]})["passed"])
        for result in ({"html": "<script>"}, {"metrics": {"bad": float("nan")}}, {"chart": {"points": [{"x": "not numeric", "y": 1}]}}):
            with self.assertRaises((ProjectError, ValueError)):
                validate_result(result)

    def test_pca_is_reusable_and_rejects_invalid_features(self):
        import pandas as pd
        namespace = {}
        exec(PCA_CODE, namespace)  # trusted shipped starter, not generated source
        data = {"cohort": pd.DataFrame({"first": [1,2,3,4,5], "second": [8,4,9,3,6]})}
        params = {"source": "cohort", "columns": ["first", "second"]}
        result = namespace["analyze"](data, params)
        self.assertEqual(result["metrics"]["rows_analyzed"], 5)
        with self.assertRaises(ValueError):
            namespace["analyze"](data, {**params, "columns": ["first"]})


class ProjectRouteTests(unittest.TestCase):
    def test_authentication_validation_and_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            app = create_app({"STATE_DIR": str(root / "state"), "WORK_DIR": str(root),
                              "START_WATCHERS": False, "AUTH_TOKEN": "test", "APP_BASE_URL": "/nested"})
            try:
                client = app.test_client()
                self.assertEqual(client.get("/nested/api/projects").status_code, 401)
                auth = {"Authorization": "Bearer test"}
                made = client.post("/nested/api/projects", json={"name": "trial", "location": str(root)}, headers=auth)
                self.assertEqual(made.status_code, 201, made.data)
                pid = made.json["id"]
                invalid = client.post(f"/nested/api/projects/{pid}/sources", json={"sources": [1]}, headers=auth)
                self.assertEqual(invalid.status_code, 400)
                self.assertEqual(client.get(f"/nested/api/projects/{pid}/history", headers=auth).status_code, 200)
            finally:
                close_app(app)


if __name__ == "__main__":
    unittest.main()
