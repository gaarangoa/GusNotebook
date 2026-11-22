"""Report-first project broker: approvals, immutable runs, review and audit.

The registry and run snapshots preserve provenance separately from editable
project files. uv workers run with host permissions, without app token variables.
"""

import base64
from copy import deepcopy
import csv
from datetime import datetime, timezone
import io
import json
import math
from pathlib import Path
import re
import threading
import uuid

from .project_files import ProjectError, digest, encoded, identifier, read_file, write_file
from .project_runner import UvRunner, WORKER
from . import project_templates as templates


def now():
    return datetime.now(timezone.utc).isoformat()


def validate_result(result):
    if not isinstance(result, dict) or set(result) - {"title", "summary", "metrics", "chart", "table"}:
        raise ProjectError("Analysis must return title, summary, metrics, chart and/or table")
    for key in ("title", "summary"):
        if not isinstance(result.get(key, ""), str) or len(result.get(key, "")) > 20000:
            raise ProjectError("Invalid result text")
    metrics = result.get("metrics", {})
    if not isinstance(metrics, dict) or len(metrics) > 100 or any(
            not isinstance(v, (str, int, float, type(None))) for v in metrics.values()):
        raise ProjectError("Metrics must be a small mapping of names to scalar values")
    if "chart" in result:
        chart = result["chart"]
        if not isinstance(chart, dict) or not isinstance(chart.get("points"), list) or len(chart["points"]) > 50000:
            raise ProjectError("A chart requires at most 50,000 points")
        for point in chart["points"]:
            if not isinstance(point, dict) or any(type(point.get(k)) not in (int, float) for k in ("x", "y")):
                raise ProjectError("Chart points require numeric x and y")
    if "table" in result:
        table = result["table"]
        if not isinstance(table, dict) or not isinstance(table.get("columns"), list) or not isinstance(table.get("rows"), list):
            raise ProjectError("Table requires columns and rows")
        if len(table["columns"]) > 100 or len(table["rows"]) > 50000 or any(
                not isinstance(row, list) or len(row) != len(table["columns"]) for row in table["rows"]):
            raise ProjectError("Invalid or oversized table")
    if len(encoded(result)) > 8 * 1024 * 1024:
        raise ProjectError("Result exceeds 8 MB")
    return result


def compare_results(original, candidate, rtol=1e-7, atol=1e-10):
    changes, total = [], [0]

    def compare(a, b, path):
        equal = False
        if type(a) in (int, float) and type(b) in (int, float):
            equal = math.isclose(a, b, rel_tol=rtol, abs_tol=atol)
        elif isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                if key not in a or key not in b:
                    record(path + "/" + key, a.get(key), b.get(key))
                else:
                    compare(a[key], b[key], path + "/" + key)
            return
        elif isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                record(path + "/length", len(a), len(b))
            for index, (left, right) in enumerate(zip(a, b)):
                compare(left, right, path + "/" + str(index))
            return
        else:
            equal = type(a) is type(b) and a == b
        if not equal:
            record(path, a, b)

    def record(path, a, b):
        total[0] += 1
        if len(changes) < 100:
            changes.append({"path": path, "before": str(a)[:300], "after": str(b)[:300]})

    compare(original, candidate, "")
    return {"passed": total[0] == 0, "difference_count": total[0], "changes": changes,
            "rtol": rtol, "atol": atol}


class ProjectManager:
    def __init__(self, directory, runner=None, publish=None):
        self.publish = publish or (lambda *args, **kwargs: None)
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runner = runner or UvRunner()
        self.guard = threading.RLock()
        self.jobs = {}
        self.registry = self.directory / "registry.json"
        self.registry_error = None
        try:
            self.projects = json.loads(read_file(self.registry, 128 * 1024 * 1024)) if self.registry.exists() else {}
            if not isinstance(self.projects, dict) or any(not isinstance(p, dict) or
                    not {"id", "path", "runs", "events", "analyses", "sources"} <= set(p) for p in self.projects.values()):
                raise ProjectError("Invalid project registry")
        except (OSError, ValueError) as exc:
            # A damaged project registry must not disable ordinary notebooks or
            # silently turn into a new registry that overwrites the old grants.
            self.projects = {}
            self.registry_error = "Project registry cannot be read; restore it from backup: " + str(exc)
        for project in self.projects.values():
            # A killed server cannot leave a run looking active or accepted.
            interrupted = False
            for run in project["runs"]:
                if run["status"] == "running":
                    run.update(status="interrupted", error="Server stopped before this run completed")
                    write_file(self._authority(project) / f"runs/{run['id']}/receipt.json", encoded(run))
                    interrupted = True
            if interrupted:
                self._event(project, "server_recovery", text="Unfinished runs were marked interrupted")

    def _root(self, project):
        return Path(project["path"])

    def _authority(self, project):
        return self.directory / project["id"]

    def _get(self, project_id):
        if self.registry_error:
            raise ProjectError(self.registry_error)
        if project_id not in self.projects:
            raise ProjectError("Unknown project")
        return self.projects[project_id]

    def _idle(self, project):
        job = self.jobs.get(project["id"])
        if job and job["status"] == "running":
            raise ProjectError("Wait for the current project operation or cancel it first")

    def _save(self, project):
        write_file(self.registry, encoded(self.projects))
        projection = {k: v for k, v in project.items() if k not in {"events", "runs"}}
        projection["sources"] = [{k: v for k, v in s.items() if k not in {"snapshot"}} for s in project["sources"]]
        try:
            if not self._root(project).is_dir():
                raise FileNotFoundError("Project folder is missing; authoritative history was retained")
            write_file(self._root(project) / "gusproject.json", encoded(projection))
            write_file(self._root(project) / "audit/events.jsonl",
                       b"".join(json.dumps(e, allow_nan=False).encode() + b"\n" for e in project["events"]))
        except OSError as exc:
            project["projection_warning"] = str(exc)

    def _event(self, project, kind, **payload):
        # Large HTML/source revisions are content-addressed; never duplicate a
        # bundled renderer or an entire result in every registry/event entry.
        for key in ("before", "after", "report_before", "report_after"):
            if isinstance(payload.get(key), str) and len(payload[key]) > 16000:
                raw = payload[key].encode()
                sha = digest(raw)
                write_file(self._authority(project) / "artifacts" / sha, raw)
                write_file(self._root(project) / "audit/artifacts" / sha, raw)
                payload[key] = {"artifact_sha256": sha, "bytes": len(raw)}
        event = {**payload, "event_id": "event_" + uuid.uuid4().hex, "time": now(), "kind": kind}
        project["events"].append(event)
        self._save(project)
        self.publish("project_changed", project_id=project["id"])
        return event

    def list(self):
        with self.guard:
            if self.registry_error:
                raise ProjectError(self.registry_error)
            return [{"id": p["id"], "name": p["name"], "path": p["path"]} for p in self.projects.values()]

    def view(self, project_id):
        with self.guard:
            project = deepcopy(self._get(project_id))
            project["sources"] = [{k: v for k, v in s.items() if k != "snapshot"} for s in project["sources"]]
            job = self.jobs.get(project_id)
            project["job"] = {k: v for k, v in job.items() if not k.startswith("_")} if job else None
            project["events"] = project["events"][-200:]
            report = read_file(Path(project["path"]) / "reports/deck.html").decode()
            project["report_revision"] = digest(report.encode())
            project["report"] = str(self._root(project) / "reports/deck.html")
            project["notebook"] = str(self._root(project) / "notebooks/analysis.ipynb")
            project["notebooks"] = {key: str(self._root(project) / f"notebooks/{key}.ipynb") for key in project["analyses"]}
            for key, analysis in project["analyses"].items():
                code = read_file(Path(project["path"]) / f"code/src/project_analysis/{key}.py", 100000).decode()
                analysis["code"] = code
                analysis["code_revision"] = digest(code.encode())
                accepted = next((r for r in project["runs"] if r["id"] == analysis.get("accepted_run")), None)
                analysis["stale"] = bool(accepted and (accepted["code_sha256"] != digest(code.encode()) or
                    accepted["parameters"] != analysis["parameters"] or
                    {s["id"] for s in accepted["sources"]} != set(analysis["source_ids"]) or
                    accepted["environment"] != project["environment"] or
                    any(not any(s["id"] == src["id"] and s["sha256"] == src["sha256"] for s in project["sources"])
                        for src in accepted["sources"])))
                analysis["report_modified"] = False
                displayed = analysis.get("displayed_run") or (accepted["id"] if accepted else None)
                if displayed:
                    result = json.loads(read_file(self._authority(project) / f"runs/{displayed}/result.json"))
                    expected = templates.result_html(result, key, displayed)
                    analysis["report_modified"] = expected not in report
            return project

    def create(self, name, location):
        if self.registry_error:
            raise ProjectError(self.registry_error)
        identifier(name)
        parent = Path(location).expanduser()
        if not parent.is_absolute() or not parent.is_dir():
            raise ProjectError("Choose an existing absolute folder on the workspace host")
        root = parent.resolve() / name
        if self.directory == root or root in self.directory.parents or self.directory in root.parents:
            raise ProjectError("Projects cannot contain or reside in application state")
        with self.guard:
            if root.exists():
                raise ProjectError("Project folder already exists; choose a new name")
            root.mkdir(mode=0o700)
            project = {"version": 1, "id": "project_" + uuid.uuid4().hex, "name": name, "path": str(root),
                       "created_at": now(), "sources": [], "analyses": {}, "runs": [], "events": [],
                       "environment": None, "tolerances": {"rtol": 1e-7, "atol": 1e-10}}
            self.projects[project["id"]] = project
            for folder in ("code/src/project_analysis", "data/inputs", "data/derived", "results/runs",
                           "results/comparisons", "environment", "notebooks", "reports/views", "reports/assets", "tests", "audit"):
                (root / folder).mkdir(parents=True, exist_ok=True)
            specification = templates.pyproject(name)
            write_file(root / "pyproject.toml", specification)
            project["specification"] = specification
            write_file(root / ".python-version", "3.12\n")
            write_file(root / ".gitignore", "environment/\ndata/\nresults/\naudit/\nnotebooks/\nreports/deck.html\n.gusnb-*\n__pycache__/\n*.egg-info/\n")
            write_file(root / "code/src/project_analysis/__init__.py", '"""Reusable analyses with explicit DataFrame inputs."""\n')
            write_file(root / "reports/deck.html", templates.deck(name))
            write_file(root / "notebooks/analysis.ipynb", templates.empty_notebook(name, project["id"]))
            write_file(root / "reports/views/result.html", self._render({}, "analysis", "unexecuted").replace("{}", "GUS_RESULT_JSON", 1))
            write_file(root / "reports/views/renderer.js", templates.renderer())
            write_file(root / "README.md", "# " + name + "\n\nOpen this project in GusNotebook → + → Analysis project.\n"
                       "Use the existing terminal agent to prepare uv, register your approved data, and update the report.\n"
                       "The report and Jupyter notebook open as normal workspace tabs. Review results and history from their project toolbar.\n"
                       "Runs use environment/.venv, managed by uv. Runtime details are in environment/runtime.json.\n"
                       "To use the same environment outside GusNotebook:\n\n"
                       "```sh\nUV_PROJECT_ENVIRONMENT=environment/.venv uv sync --locked\n```\n"
                       "Select environment/.venv/bin/python as the notebook kernel. Python and terminal agents have host filesystem permissions; uv isolates dependencies only.\n")
            self._event(project, "project_created", name=name)
            return self.view(project["id"])

    def sources(self, project_id, entries):
        if not isinstance(entries, list) or not 1 <= len(entries) <= 20:
            raise ProjectError("Provide 1 to 20 source entries with id and absolute path")
        with self.guard:
            project = self._get(project_id)
            self._idle(project)
            additions = []
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                    raise ProjectError("Each source needs an id and a file path")
                source_id = identifier(entry.get("id"))
                path = Path(entry.get("path", "")).expanduser()
                if not path.is_absolute() or path.suffix.lower() not in {".csv", ".parquet"}:
                    raise ProjectError("Approved inputs must be absolute CSV or Parquet file paths")
                raw = read_file(path, 256 * 1024 * 1024)
                sha = digest(raw)
                if path.suffix.lower() == ".csv":
                    header = next(csv.reader(io.StringIO(raw[:1024 * 1024].decode("utf-8-sig"))), [])
                    schema = [{"name": column, "type": "CSV (inferred during execution)"} for column in header]
                else:
                    import pyarrow.parquet as pq
                    schema = [{"name": f.name, "type": str(f.type)} for f in pq.read_schema(io.BytesIO(raw))]
                if not schema or len(schema) > 1000:
                    raise ProjectError("Input must have 1 to 1,000 columns")
                snapshot = self._authority(project) / "inputs" / (sha + path.suffix.lower())
                write_file(snapshot, raw)
                source = {"id": source_id, "sha256": sha, "size": len(raw), "suffix": path.suffix.lower(),
                          "schema": schema, "approved_at": now(), "snapshot": str(snapshot)}
                write_file(self._root(project) / f"data/inputs/{source_id}/{sha}{source['suffix']}", raw)
                additions.append(source)
            if len({s["id"] for s in additions}) != len(additions):
                raise ProjectError("Source IDs must be unique")
            replaced = {s["id"] for s in additions}
            project["sources"] = [s for s in project["sources"] if s["id"] not in replaced] + additions
            self._event(project, "sources_approved", sources=[{k: v for k, v in s.items() if k != "snapshot"} for s in additions])
            return self.view(project_id)

    def revoke(self, project_id, source_id):
        with self.guard:
            project = self._get(project_id)
            self._idle(project)
            project["sources"] = [s for s in project["sources"] if s["id"] != source_id]
            self._event(project, "source_revoked", source_id=source_id)
            return self.view(project_id)

    def _launch(self, project, kind, action):
        self._idle(project)
        job = {"id": "job_" + uuid.uuid4().hex, "kind": kind, "status": "running", "started_at": now(), "_cancel": threading.Event()}
        self.jobs[project["id"]] = job
        self._event(project, "operation_started", job_id=job["id"], operation=kind)

        def work():
            try:
                action(job)
                job["status"] = "cancelled" if job["_cancel"].is_set() else "completed"
            except Exception as exc:
                job.update(status="cancelled" if job["_cancel"].is_set() else "failed", error=str(exc)[-12000:])
            finally:
                with self.guard:
                    job["finished_at"] = now()
                    self._event(project, "operation_finished", operation=job["kind"],
                                **{k: v for k, v in job.items() if not k.startswith("_") and k != "kind"})
        job["_thread"] = threading.Thread(target=work, daemon=True)
        job["_thread"].start()
        return {k: v for k, v in job.items() if not k.startswith("_")}

    def prepare(self, project_id):
        with self.guard:
            project = self._get(project_id)
            def action(job):
                root = self._root(project)
                if read_file(root / "pyproject.toml").decode() != project["specification"]:
                    raise ProjectError("pyproject.toml changed outside the broker; restore it before preparing this environment")
                old = project["environment"]
                lock = read_file(self._authority(project) / "uv.lock") if old else None
                environment, lock = self.runner.prepare(root, project["specification"], lock, job["_cancel"])
                environment.update(lock_sha256=digest(lock), specification_sha256=digest(project["specification"].encode()), prepared_at=now())
                with self.guard:
                    write_file(self._authority(project) / "uv.lock", lock)
                    write_file(root / "uv.lock", lock)
                    write_file(root / "environment/runtime.json", encoded(environment))
                    project["environment"] = environment
                    # Store the interpreter in standard notebook metadata without
                    # restarting any existing kernel or replacing user cells.
                    for notebook in (root / "notebooks").glob("*.ipynb"):
                        document = json.loads(read_file(notebook))
                        document.setdefault("metadata", {}).setdefault("kernelspec", {}).update(
                            name="python3", display_name="Project · uv", language="python",
                            notebook_python=environment["python"])
                        write_file(notebook, encoded(document))
                    self._event(project, "environment_prepared", **environment)
            return self._launch(project, "prepare", action)

    def save_analysis(self, project_id, analysis_id, parameters, source_ids, code=None, _active_job=False, expected_revision=None):
        identifier(analysis_id)
        if analysis_id == "__init__":
            raise ProjectError("Reserved module name")
        if not isinstance(parameters, dict) or len(encoded(parameters)) > 20000:
            raise ProjectError("Parameters must be a JSON object under 20 KB")
        with self.guard:
            project = self._get(project_id)
            if not _active_job:
                self._idle(project)
            if not isinstance(source_ids, list) or not source_ids or not all(isinstance(s, str) for s in source_ids) or not set(source_ids) <= {s["id"] for s in project["sources"]}:
                raise ProjectError("Select approved sources for this analysis")
            code = templates.PCA_CODE if code is None else code
            if not isinstance(code, str) or len(code.encode()) > 100000:
                raise ProjectError("Python source must be under 100 KB")
            compile(code, "analysis.py", "exec")  # syntax check only; execution happens in the project uv interpreter
            old = project["analyses"].get(analysis_id, {})
            analysis = {"id": analysis_id, "parameters": parameters, "source_ids": source_ids,
                        "accepted_run": old.get("accepted_run"), "candidate_run": old.get("candidate_run"),
                        "displayed_run": old.get("displayed_run")}
            path = self._root(project) / f"code/src/project_analysis/{analysis_id}.py"
            previous = read_file(path).decode() if path.exists() else None
            if expected_revision is not None and expected_revision != (digest(previous.encode()) if previous is not None else "new"):
                raise ProjectError("Analysis source changed elsewhere; reload before saving")
            write_file(path, code)
            project["analyses"][analysis_id] = analysis
            selected = [s for s in project["sources"] if s["id"] in source_ids]
            write_file(self._root(project) / f"notebooks/{analysis_id}.ipynb",
                       templates.notebook(analysis_id, parameters, selected, code, python=(project.get("environment") or {}).get("python")))
            self._event(project, "analysis_edited", analysis_id=analysis_id, before=previous, after=code,
                        parameters=parameters, source_ids=source_ids)
            return self.view(project_id)

    def record_agent_event(self, project_id, kind, text, **context):
        if kind not in {"user_message", "assistant_message", "agent_opened"}:
            raise ProjectError("Unknown agent event")
        if not isinstance(text, str) or len(text) > 100000:
            raise ProjectError("Agent message must be text under 100 KB")
        with self.guard:
            project = self._get(project_id)
            if kind == "user_message" and context.get("request_id") and any(
                    e.get("request_id") == context["request_id"] and e["kind"] == kind for e in project["events"]):
                return {"status": "recorded"}
            self._event(project, kind, text=text, **context)
        return {"status": "recorded"}

    def _render(self, result, analysis_id, run_id):
        return ('<style>' + templates.STYLE + '</style>' + templates.result_html(result, analysis_id, run_id) +
                '<script>' + templates.d3_source() + '</script><script>' + templates.renderer() + '</script>')

    def _run_record(self, project, run_id):
        record = next((r for r in project["runs"] if r["id"] == run_id), None)
        if not record:
            raise ProjectError("Unknown run in this project")
        return record

    def run(self, project_id, analysis_id, replay=None, package=False):
        with self.guard:
            project = self._get(project_id)
            if analysis_id not in project["analyses"]:
                raise ProjectError("Save an analysis first")
            if not project["environment"]:
                raise ProjectError("Prepare the uv environment first")
            def action(job):
                self._execute(project, analysis_id, job, replay, package)
            return self._launch(project, "package" if package else "replay" if replay else "run", action)

    def _execute(self, project, analysis_id, job, replay, package):
        with self.guard:
            analysis = project["analyses"][analysis_id]
            if read_file(self._root(project) / "pyproject.toml").decode() != project["specification"]:
                raise ProjectError("Project dependency declaration changed outside the broker")
            if digest(read_file(self._root(project) / "uv.lock")) != project["environment"]["lock_sha256"]:
                raise ProjectError("Project lockfile changed; restore the prepared lockfile before running")
            baseline_id = replay or analysis.get("accepted_run")
            baseline = deepcopy(self._run_record(project, baseline_id)) if baseline_id else None
            if baseline and (baseline["analysis_id"] != analysis_id or baseline["status"] != "completed"):
                raise ProjectError("Select a completed run of this analysis")
            source_ids = analysis["source_ids"]
            selected = [deepcopy(s) for s in project["sources"] if s["id"] in source_ids]
            if len(selected) != len(source_ids):
                raise ProjectError("An input was revoked; approve it again or update the analysis")
            code = read_file(self._root(project) / f"code/src/project_analysis/{analysis_id}.py", 100000).decode()
            parameters = deepcopy(analysis["parameters"])
            environment = deepcopy(project["environment"])
            if replay:
                code = read_file(self._authority(project) / f"runs/{replay}/code/src/project_analysis/{analysis_id}.py").decode()
                parameters = baseline["parameters"]
                environment = baseline["environment"]
                by_id = {s["id"]: s for s in project["sources"]}
                selected = []
                for source in baseline["sources"]:
                    if source["id"] not in by_id or by_id[source["id"]]["sha256"] != source["sha256"]:
                        raise ProjectError("Replay requires the exact input revision to still be approved")
                    selected.append(deepcopy(by_id[source["id"]]))
            run_id = "run_" + uuid.uuid4().hex
            directory = self._authority(project) / "runs" / run_id
            write_file(directory / "code/src/project_analysis/__init__.py", "")
            write_file(directory / f"code/src/project_analysis/{analysis_id}.py", code)
            write_file(directory / "pyproject.toml", project["specification"])
            write_file(directory / "worker.py", WORKER)
            inputs = []
            for source in selected:
                if digest(read_file(source["snapshot"], 256 * 1024 * 1024)) != source["sha256"]:
                    raise ProjectError("Approved input snapshot changed")
                source["path"] = source["snapshot"]
                inputs.append({k: v for k, v in source.items() if k != "snapshot"})
            request = {"analysis_id": analysis_id, "parameters": parameters, "sources": inputs, "build": package}
            write_file(directory / "request.json", encoded(request))
            record = {"id": run_id, "analysis_id": analysis_id, "status": "running", "started_at": now(),
                      "code_sha256": digest(code.encode()), "parameters": parameters, "sources": inputs,
                      "environment": environment, "renderer_sha256": digest(templates.renderer().encode()),
                      "d3_sha256": digest(templates.d3_source().encode()),
                      "replay_of": replay, "comparison_to": baseline_id}
            project["runs"].append(record)
            write_file(directory / "receipt.json", encoded(record))
            self._event(project, "run_started", **record)
        try:
            output = self.runner.execute(environment, directory, selected, job["_cancel"])
            if job["_cancel"].is_set():
                raise ProjectError("Operation cancelled")
            result = validate_result(output["result"])
            with self.guard:
                record.update(status="completed", finished_at=now(), result_sha256=digest(encoded(result)),
                              installed=output["environment"], log=output.get("log", "")[-32768:])
                write_file(directory / "result.json", encoded(result))
                rendered = self._render(result, analysis_id, run_id)
                nb = templates.notebook(analysis_id, parameters, selected, code, result, run_id, rendered, python=environment.get("python"))
                write_file(directory / "notebook.ipynb", nb)
                write_file(directory / "view.html", rendered)
                record["notebook_sha256"] = digest(nb.encode())
                write_file(self._root(project) / f"results/runs/{run_id}/result.json", encoded(result))
                write_file(self._root(project) / f"results/runs/{run_id}/notebook.ipynb", nb)
                write_file(self._root(project) / f"data/derived/{run_id}/result.json", encoded(result))
                write_file(self._root(project) / f"notebooks/{analysis_id}.ipynb", nb)
                if baseline:
                    before = json.loads(read_file(self._authority(project) / f"runs/{baseline_id}/result.json"))
                    comparison = compare_results(before, result, **project["tolerances"])
                    comparison["lineage_changes"] = [key for key in ("code_sha256", "parameters", "sources", "environment", "renderer_sha256") if baseline[key] != record[key]]
                    record["comparison"] = comparison
                    write_file(self._root(project) / f"results/comparisons/{run_id}.json", encoded(comparison))
                analysis["candidate_run"] = run_id
                self._event(project, "run_completed", run_id=run_id, analysis_id=analysis_id,
                            result_sha256=record["result_sha256"], comparison=record.get("comparison"))
            if package:
                wheel = output.get("wheel")
                if not wheel or not re.fullmatch(r"[A-Za-z0-9_.-]+\.whl", wheel["name"]):
                    raise ProjectError("Build did not return a wheel")
                # Run the installed wheel in a fresh worker, with no source on sys.path.
                request.update(build=False, wheel=wheel, check_generality=True)
                write_file(directory / "request.json", encoded(request))
                verified = self.runner.execute(environment, directory, selected, job["_cancel"])
                comparison = compare_results(result, validate_result(verified["result"]), **project["tolerances"])
                if not comparison["passed"] or not verified.get("generality", {}).get("passed"):
                    raise ProjectError("Packaged analysis did not reproduce its source result")
                with self.guard:
                    wheel_bytes = base64.b64decode(wheel["data"], validate=True)
                    write_file(self._root(project) / f"results/runs/{run_id}/{wheel['name']}", wheel_bytes)
                    record["package"] = {"name": wheel["name"], "sha256": digest(wheel_bytes),
                                         "comparison": comparison, "generality": verified["generality"]}
                    self._event(project, "package_verified", run_id=run_id, **record["package"])
        except Exception as exc:
            with self.guard:
                record.update(status="cancelled" if job["_cancel"].is_set() else "failed", error=str(exc)[-12000:], finished_at=now())
                if analysis.get("candidate_run") == run_id:
                    analysis["candidate_run"] = None
                self._event(project, "run_failed", run_id=run_id, error=record["error"])
            raise
        finally:
            with self.guard:
                write_file(directory / "receipt.json", encoded(record))
                write_file(self._root(project) / f"results/runs/{run_id}/receipt.json", encoded(record))
                self._save(project)

    def accept(self, project_id, run_id, expected_revision, *, draft=False):
        with self.guard:
            project = self._get(project_id)
            self._idle(project)
            record = self._run_record(project, run_id)
            if record["status"] != "completed":
                raise ProjectError("Only a completed run can be accepted")
            path = self._root(project) / "reports/deck.html"
            old = read_file(path).decode()
            if digest(old.encode()) != expected_revision:
                raise ProjectError("The report changed. Reload and review before accepting")
            key = record["analysis_id"]
            result = json.loads(read_file(self._authority(project) / f"runs/{run_id}/result.json"))
            block = templates.result_html(result, key, run_id)
            start, end = f"<!-- gusnb:{key}:start -->", f"<!-- gusnb:{key}:end -->"
            if start in old or end in old:
                if old.count(start) != 1 or old.count(end) != 1 or old.index(start) >= old.index(end):
                    raise ProjectError("Report analysis markers were modified; restore them before accepting")
                new = old[:old.index(start)] + start + block + end + old[old.index(end) + len(end):]
            else:
                if old.count("<!-- gusnb:sections -->") != 1:
                    raise ProjectError("Restore the report's gusnb:sections marker before adding an analysis")
                new = old.replace("<!-- gusnb:sections -->", start + block + end + "\n<!-- gusnb:sections -->")
            write_file(path, new)
            project["analyses"][key]["displayed_run"] = run_id
            if not draft:
                project["analyses"][key]["accepted_run"] = run_id
            self._event(project, "run_published" if draft else "run_accepted", run_id=run_id, analysis_id=key,
                        report_before=old, report_after=new, report_sha256=digest(new.encode()))
            return self.view(project_id)

    def report(self, project_id, text=None, expected_revision=None):
        with self.guard:
            project = self._get(project_id)
            path = self._root(project) / "reports/deck.html"
            old = read_file(path).decode()
            if text is not None:
                if not isinstance(text, str) or len(text.encode()) > 4 * 1024 * 1024:
                    raise ProjectError("Report must be HTML under 4 MB")
                if expected_revision != digest(old.encode()):
                    raise ProjectError("Report changed elsewhere; reload before saving")
                write_file(path, text)
                self._event(project, "report_edited", before=old, after=text)
                old = text
            return {"html": old, "revision": digest(old.encode())}

    def artifact(self, project_id, run_id, name):
        with self.guard:
            project = self._get(project_id)
            record = self._run_record(project, run_id)
            if name == "analysis.py":
                return read_file(self._authority(project) / "runs" / run_id / "code/src/project_analysis" / (record["analysis_id"] + ".py"))
            if name not in {"result.json", "receipt.json", "notebook.ipynb", "view.html"}:
                raise ProjectError("Unknown artifact")
            return read_file(self._authority(project) / "runs" / run_id / name)

    def cancel(self, project_id):
        with self.guard:
            self._get(project_id)
            job = self.jobs.get(project_id)
            if job and job["status"] == "running":
                job["_cancel"].set()

    def close(self):
        for job in list(self.jobs.values()):
            job["_cancel"].set()
        self.runner.close()
        for job in list(self.jobs.values()):
            job["_thread"].join(timeout=3)
