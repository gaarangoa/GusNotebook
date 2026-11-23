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
        return {"runner": "uv", "path": str(directory / "data/environment/.venv"), "python": str(directory / "data/environment/.venv/bin/python")}, b"fixture-lock"

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
        self.assertTrue((self.root / "research/results/reports/deck.html").exists())
        root = self.root / "research"
        self.assertEqual({p.name for p in root.iterdir() if p.is_dir()}, {"code", "data", "results"})
        for path in ("code/notebooks/analysis.ipynb", "code/pyproject.toml", "code/.python-version"):
            self.assertTrue((root / path).is_file(), path)
        for path in ("code/tests", "data/environment", "results/outputs"):
            self.assertTrue((root / path).is_dir(), path)
        for name, parent in (("research", self.root), ("state", self.root), ("nested", self.root / "state"), ("../escape", self.root)):
            with self.assertRaises(ProjectError):
                self.manager.create(name, str(parent))
        ignored = (self.root / "research/.gitignore").read_text()
        self.assertIn("/*", ignored)
        self.assertIn("!/code/", ignored)
        self.assertIn("/code/tests/", ignored)

    def test_large_html_report_round_trip_and_conflict_check(self):
        app = create_app({"STATE_DIR": str(self.root / "large-state"), "WORK_DIR": str(self.root),
                          "START_WATCHERS": False, "AUTH_TOKEN": "test"})
        try:
            client = app.test_client()
            client.environ_base["HTTP_AUTHORIZATION"] = "Bearer test"
            made = client.post('/api/projects', json={"name": "large_report", "location": str(self.root)}).json
            endpoint = '/api/projects/' + made['id']
            old = client.get(endpoint + '/report').json
            html = '<!doctype html><p>' + 'x' * (9 * 1024 * 1024) + '</p>'
            saved = client.post(endpoint + '/report', json={"html": html, "revision": old['revision']})
            self.assertEqual(saved.status_code, 200, saved.json if saved.status_code != 200 else None)
            self.assertEqual(client.get(endpoint + '/report').json['html'], html)
            self.assertEqual(client.get(endpoint).json['report_revision'], saved.json['revision'])
            stale = client.post(endpoint + '/report', json={"html": 'old edit', "revision": old['revision']})
            self.assertEqual(stale.status_code, 400)
            self.assertFalse((Path(made['path']) / 'data/.gusnotebook/audit/artifacts').exists())
        finally:
            close_app(app)

    def test_snapshots_are_exact_and_manifest_cannot_widen_grants(self):
        source = self.manager.projects[self.pid]["sources"][0]
        snapshot = read_file(source["snapshot"])
        self.source.write_text("changed")
        self.assertEqual(read_file(source["snapshot"]), snapshot)
        manifest = self.root / "research/data/.gusnotebook/project.json"
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

    def test_draft_publication_preserves_human_baseline_and_report_edits(self):
        self.prepare()
        baseline = self.run_analysis()
        accepted = baseline["runs"][-1]["id"]
        self.manager.accept(self.pid, accepted, baseline["report_revision"])
        candidate = self.run_analysis({"value": 2})
        run = candidate["runs"][-1]["id"]
        published = self.manager.accept(self.pid, run, candidate["report_revision"], draft=True)
        analysis = published["analyses"]["analysis"]
        self.assertEqual(analysis["displayed_run"], run)
        self.assertEqual(analysis["accepted_run"], accepted)
        self.assertFalse(analysis["report_modified"])
        self.assertEqual(published["events"][-1]["kind"], "run_published")
        self.assertIn(run, self.manager.report(self.pid)["html"])
        report = self.manager.report(self.pid)
        self.manager.report(self.pid, report["html"].replace("</body>", "<p>Human edit</p></body>"), report["revision"])
        with self.assertRaises(ProjectError):
            self.manager.accept(self.pid, accepted, report["revision"], draft=True)
        self.assertIn("Human edit", self.manager.report(self.pid)["html"])

    def test_linked_notebook_contains_producing_source_outputs_and_uv_kernel(self):
        path = Path(self.project["notebook"])
        initial = json.loads(path.read_text())
        self.assertEqual(initial["metadata"]["gusproject"]["project_id"], self.pid)
        self.prepare()
        python = self.manager.view(self.pid)["environment"]["python"]
        self.assertEqual(json.loads(path.read_text())["metadata"]["kernelspec"]["notebook_python"], python)
        project = self.run_analysis()
        notebook = json.loads(path.read_text())
        self.assertIn(PCA_CODE, "".join(notebook["cells"][1]["source"]))
        self.assertEqual(notebook["metadata"]["kernelspec"]["notebook_python"], python)
        self.assertEqual(notebook["cells"][1]["outputs"][0]["data"]["application/json"]["metrics"]["value"], 1)
        run = project["runs"][-1]["id"]
        path.write_text("human notebook edit")
        original = json.loads(self.manager.artifact(self.pid, run, "notebook.ipynb"))
        self.assertEqual(original["metadata"]["gusproject"]["run_id"], run)

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
        self.manager.report(self.pid, report["html"].replace("</body>", "<p>Human edit</p></body>"), report["revision"])
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

    def test_terminal_messages_are_durable_and_source_conflicts_preserved(self):
        self.manager.record_agent_event(self.pid, "user_message", "Analyze approved data", terminal="t1", actor="Codex")
        self.manager.record_agent_event(self.pid, "assistant_message", "Checked exclusions", terminal="t1")
        self.manager.save_analysis(self.pid, "analysis", {}, ["data"])
        old = self.manager.view(self.pid)["analyses"]["analysis"]["code_revision"]
        path = self.root / "research/code/src/project_analysis/analysis.py"
        path.write_text("# human edit\n" + PCA_CODE)
        with self.assertRaises(ProjectError):
            self.manager.save_analysis(self.pid, "analysis", {}, ["data"], PCA_CODE, expected_revision=old)
        self.assertTrue(path.read_text().startswith("# human edit"))
        reloaded = ProjectManager(self.root / "state", FixtureRunner())
        try:
            events = reloaded.view(self.pid)["events"]
            self.assertIn("Analyze approved data", [e.get("text") for e in events])
            self.assertIn("Checked exclusions", [e.get("text") for e in events])
        finally:
            reloaded.close()

    def test_corrupt_registry_fails_closed_without_overwriting_it(self):
        self.manager.registry.write_text("broken data")
        other = ProjectManager(self.root / "state", FixtureRunner())
        with self.assertRaises(ProjectError):
            other.list()
        with self.assertRaises(ProjectError):
            other.create("unavailable", str(self.root))
        self.assertEqual(self.manager.registry.read_text(), "broken data")
        other.close()

    def test_missing_uv_does_not_fall_back_to_app_python(self):
        from gusnotebook.project_runner import UvRunner
        runner = UvRunner()
        runner.uv = None
        self.assertFalse(runner.status()["available"])
        with self.assertRaises(ProjectError):
            runner.prepare(self.root / "build", "irrelevant", None)
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
    def test_ordinary_terminal_creates_and_switches_projects_in_normal_tabs(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            app = create_app({"STATE_DIR": str(root / "state"), "WORK_DIR": str(root),
                              "START_WATCHERS": False, "AUTH_TOKEN": "test"})
            try:
                runtime = app.extensions["gusnotebook"]
                session = runtime.store.current()
                terminal = SimpleNamespace(id="ordinary", workspace=session.id, project=None, kind="codex")
                client = app.test_client()
                client.environ_base.update(HTTP_AUTHORIZATION="Bearer test", HTTP_X_TERMINAL_ID=terminal.id,
                                           HTTP_X_SESSION_ID=session.id)
                with patch.object(runtime.terms, "get", return_value=terminal):
                    self.assertEqual(client.get("/api/projects/context").status_code, 404)
                    client.post('/api/prompt', json={"prompt": "Create a report with a linked notebook"})
                    first = client.post("/api/projects", json={"name": "first", "location": str(root)}).json
                    self.assertEqual(terminal.project, first["id"])
                    self.assertIn(first["notebook"], session.tabs)
                    self.assertEqual(session.active, first["report"])
                    second = client.post("/api/projects", json={"name": "second", "location": str(root)}).json
                    self.assertEqual(client.get("/api/projects/context").json["id"], second["id"])
                    client.post(f"/api/projects/{first['id']}/open")
                    self.assertEqual(client.get("/api/projects/context").json["id"], first["id"])
                    requests = [e for e in runtime.projects.view(first["id"])["events"] if e["kind"] == "user_message"]
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0]["text"], "Create a report with a linked notebook")
                    terminal.workspace = "another-session"
                    client.post(f"/api/projects/{second['id']}/open")
                    self.assertEqual(terminal.project, first["id"])
            finally:
                close_app(app)

    def test_cli_uses_live_terminal_binding_after_switching_projects(self):
        from types import SimpleNamespace
        from gusnotebook.cli import cmd_project
        with patch.dict(os.environ, {"NB_PROJECT": "old", "NB_TERMINAL": "terminal"}), \
             patch("gusnotebook.cli.call", side_effect=[{"id": "current"}, {"id": "current"}]) as call, \
             patch("builtins.print"):
            cmd_project(SimpleNamespace(project_id=None, action="status"))
        self.assertEqual(call.call_args_list[0].args, ("/api/projects/context",))
        self.assertEqual(call.call_args_list[1].args, ("/api/projects/current",))

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


    def test_project_terminals_receive_context_environment_and_prompt_audit(self):
        from types import SimpleNamespace
        from gusnotebook import terminals
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            app = create_app({"STATE_DIR": str(root / "state"), "WORK_DIR": str(root),
                              "START_WATCHERS": False, "AUTH_TOKEN": "test"})
            try:
                runtime = app.extensions["gusnotebook"]
                runtime.projects.runner = FixtureRunner()
                client = app.test_client()
                client.environ_base["HTTP_AUTHORIZATION"] = "Bearer test"
                made = client.post("/api/projects", json={"name": "trial", "location": str(root)}).json
                pid = made["id"]
                runtime.projects.prepare(pid)
                runtime.projects.jobs[pid]["_thread"].join(5)
                environment = runtime.projects.view(pid)["environment"]
                for kind in ("claude", "codex"):
                    session = SimpleNamespace(id="test-agent", kind=kind, project=pid,
                                              to_json=lambda: {"id": "test-agent", "project": pid})
                    with patch.object(runtime.terms, "create", return_value=session) as create, patch.object(terminals, "command_for", return_value=[kind]) as command:
                        response = client.post("/api/terminals", json={"kind": kind, "project": pid, "cwd": str(root)})
                    self.assertEqual(response.status_code, 200, response.json)
                    self.assertEqual(create.call_args.args[0], made["path"])
                    self.assertEqual(create.call_args.kwargs["python"], environment["python"])
                    self.assertIn("project context", command.call_args.args[1])
                    self.assertIn("uv", command.call_args.args[1])
                    with patch.object(runtime.terms, "get", return_value=session):
                        response = client.post("/api/prompt", json={"prompt": "Analyze this cohort"}, headers={"X-Terminal-Id": "test-agent"})
                    self.assertEqual(response.status_code, 200, response.json)
                event = runtime.projects.view(pid)["events"][-1]
                self.assertEqual(event["kind"], "user_message")
                self.assertEqual(event["actor"], "Codex")
                response = client.post(f"/api/projects/{pid}/notes", json={"text": "Explained exclusions"}, headers={"X-Terminal-Id": "test-agent"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(runtime.projects.view(pid)["events"][-1]["terminal"], "test-agent")
                self.assertEqual(client.post("/api/terminals", json={"kind": "shell", "project": "missing"}).status_code, 400)
            finally:
                close_app(app)


if __name__ == "__main__":
    unittest.main()
