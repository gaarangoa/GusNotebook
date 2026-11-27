"""Linked project setup and uv environments, plus legacy run compatibility."""

import base64
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import threading
import uuid

from .project_files import ProjectError, digest, encoded, identifier, parent_fd, read_file, write_file
from .project_runner import UvRunner, WORKER
from . import project_templates as templates
from .project_layout import LAYOUT, GITIGNORE, paths as layout_paths
from .project_sources import inspect_source, public_source, revision as source_revision, verify_reference
from . import project_access, project_data


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

    def _path(self, project, key, *parts):
        return self._root(project) / layout_paths(project)[key] / Path(*parts)

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
        projection["sources"] = [public_source(s, self._root(project)) for s in project["sources"]]
        try:
            if not self._root(project).is_dir():
                raise FileNotFoundError("Project folder is missing; authoritative history was retained")
            write_file(self._path(project, "manifest"), encoded(projection))
            write_file(self._path(project, "audit", "events.jsonl"),
                       b"".join(json.dumps(e, allow_nan=False).encode() + b"\n" for e in project["events"]))
            project_data.write_docs(project)
            project.pop("projection_warning", None)
        except (OSError, ProjectError) as exc:
            project["projection_warning"] = str(exc)

    def _event(self, project, kind, **payload):
        # Large HTML/source revisions are content-addressed; never duplicate a
        # bundled renderer or an entire result in every registry/event entry.
        for key in ("before", "after", "report_before", "report_after"):
            if project.get("versioning") == "git":
                payload.pop(key, None)
                continue
            if isinstance(payload.get(key), str) and len(payload[key]) > 16000:
                raw = payload[key].encode()
                sha = digest(raw)
                write_file(self._authority(project) / "artifacts" / sha, raw)
                write_file(self._path(project, "audit", "artifacts", sha), raw)
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

    def notebook_path(self, project_id):
        """Resolve an agent's default notebook without loading a large report."""
        with self.guard:
            return str(self._path(self._get(project_id), "notebooks", "analysis.ipynb"))

    def _notebook_paths(self, project):
        """Discover named analysis notebooks without reading outputs or raw data."""
        directory = self._path(project, 'notebooks')
        notebooks = {}
        try:
            # Open the directory without following symlinked parent folders.
            # The final component is only a placeholder; no index file is made.
            with parent_fd(directory / '.notebook-index') as (folder, _):
                with os.scandir(folder) as entries:
                    for entry in entries:
                        if (not entry.name.startswith('.') and entry.name.endswith('.ipynb')
                                and entry.is_file(follow_symlinks=False)):
                            notebooks[Path(entry.name).stem] = str(directory / entry.name)
        except FileNotFoundError:
            pass
        return dict(sorted(notebooks.items()))

    def for_directory(self, directory):
        """The closest registered project enclosing a terminal's working folder."""
        if not directory:
            return None
        directory = Path(directory).expanduser().resolve()
        with self.guard:
            matches = [p for p in self.projects.values()
                       if Path(p["path"]).resolve() in (directory, *directory.parents)]
            if not matches:
                return None
            project = max(matches, key=lambda p: len(Path(p["path"]).parts))
            return {key: project[key] for key in ("id", "name", "path")}

    def view(self, project_id):
        with self.guard:
            project = deepcopy(self._get(project_id))
            project["sources"] = [public_source(s, self._root(project)) for s in project["sources"]]
            project['data_access'] = project_access.status(project)
            project['sources'] = [s for s in project['sources'] if project_access.allows(project['data_access'], s['path'])]
            job = self.jobs.get(project_id)
            project["job"] = {k: v for k, v in job.items() if not k.startswith("_")} if job else None
            project["events"] = project["events"][-200:]
            report = read_file(self._path(project, "reports", "deck.html"), limit=None).decode()
            project["report_revision"] = digest(report.encode())
            project["report"] = str(self._path(project, "reports", "deck.html"))
            project["notebook"] = str(self._path(project, "notebooks", "analysis.ipynb"))
            project["layout_paths"] = layout_paths(project)
            project["data_readme"] = str(self._path(project, "data_readme"))
            project["data_lineage"] = str(self._path(project, "lineage"))
            project["notebooks"] = {key: str(self._path(project, "notebooks", f"{key}.ipynb")) for key in project["analyses"]}
            project["notebooks"].update(self._notebook_paths(project))
            for key, analysis in project["analyses"].items():
                code = read_file(Path(project["path"]) / f"code/src/project_analysis/{key}.py", 100000).decode()
                analysis["code"] = code
                analysis["code_revision"] = digest(code.encode())
                accepted = next((r for r in project["runs"] if r["id"] == analysis.get("accepted_run")), None)
                analysis["stale"] = bool(accepted and (accepted["code_sha256"] != digest(code.encode()) or
                    accepted["parameters"] != analysis["parameters"] or
                    {s["id"] for s in accepted["sources"]} != set(analysis["source_ids"]) or
                    accepted["environment"] != project["environment"] or
                    any(not any(s["id"] == src["id"] and source_revision(s) == source_revision(src) for s in project["sources"])
                        for src in accepted["sources"])))
                analysis["report_modified"] = False
                displayed = analysis.get("displayed_run") or (accepted["id"] if accepted else None)
                if displayed:
                    result = json.loads(read_file(self._authority(project) / f"runs/{displayed}/result.json"))
                    expected = templates.result_html(result, key, displayed)
                    analysis["report_modified"] = expected not in report
            return project

    def use_git(self, project_id):
        """Migrate scaffold exclusions without deleting existing files or runs."""
        with self.guard:
            project = self._get(project_id)
            project_access.ensure(project)
            policy = project_access.status(project)
            if project.get('input_policy_observed') != policy:
                project['input_policy_observed'] = policy
                self._event(project, 'input_policy_observed', policy=policy)
            if project.get("versioning") == "git":
                if not self._path(project, 'data_readme').exists() or not self._path(project, 'lineage').exists():
                    self._save(project)
                return self.view(project_id)
            path = self._root(project) / ".gitignore"
            old = read_file(path).decode() if path.exists() else ""
            lines = [line for line in old.splitlines() if line not in {"notebooks/", "reports/deck.html"}]
            for line in (GITIGNORE if project.get("layout") == LAYOUT else "data/\nresults/\nenvironment/\naudit/\ngusproject.json\n").splitlines():
                if line and line not in lines:
                    lines.append(line)
            if "\n".join(lines) + "\n" != old:
                write_file(path, "\n".join(lines) + "\n")
            if project.get("versioning") != "git":
                project["versioning"] = "git"
                self._save(project)
            return self.view(project_id)

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
                       "versioning": "git", "layout": LAYOUT,
                       "created_at": now(), "sources": [], "analyses": {}, "runs": [], "events": [],
                       "environment": None, "tolerances": {"rtol": 1e-7, "atol": 1e-10}}
            self.projects[project["id"]] = project
            for folder in ("code/src/project_analysis", "code/notebooks", "code/tests",
                           "data/environment", "data/processed", "results/reports", "results/outputs"):
                (root / folder).mkdir(parents=True, exist_ok=True)
            specification = templates.pyproject(name, source_directory="src")
            write_file(root / "code/pyproject.toml", specification)
            project["specification"] = specification
            write_file(root / "code/.python-version", "3.12\n")
            write_file(root / ".gitignore", GITIGNORE)
            write_file(root / "code/src/project_analysis/__init__.py", '"""Reusable analyses with explicit DataFrame inputs."""\n')
            write_file(root / "results/reports/deck.html", templates.deck(name))
            write_file(root / "code/notebooks/analysis.ipynb", templates.empty_notebook(name, project["id"],
                       report="../../results/reports/deck.html"))
            write_file(root / "code/README.md", "# " + name + "\n\n"
                       "## Analysis index\n\n"
                       "- [analysis.ipynb](notebooks/analysis.ipynb): starting notebook for the first scientific question.\n\n"
                       "Continue the same question in its notebook; create a descriptive notebook in notebooks/ for a separate analysis.\n"
                       "Record each notebook's purpose and reusable module links here. Keep inputs, parameters, calls, explanations and results in the notebook.\n"
                       "Put reusable transformations, calculations and model components in src/project_analysis/ and import them from notebook cells.\n"
                       "Keep figure-specific D3 code visible in the notebook and embed executed outputs into ../results/reports/deck.html.\n"
                       "Use Git for code and notebook history. Tests, data, environments and reports are ignored.\n"
                       "From this code directory, prepare the same uv environment:\n\n"
                       "```sh\nUV_PROJECT_ENVIRONMENT=../data/environment/.venv uv sync --locked --no-install-project\n```\n")
            project_access.ensure(project)
            self._event(project, "project_created", name=name)
            return self.view(project["id"])

    def sources(self, project_id, entries, context=None):
        if not isinstance(entries, list) or not 1 <= len(entries) <= 20:
            raise ProjectError("Provide 1 to 20 source entries with an absolute path and optional label")
        with self.guard:
            project = self._get(project_id)
            self._idle(project)
            policy = project_access.load(project)
            additions = []
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                    raise ProjectError("Each source needs a file path")
                path = project_access.require(project, entry['path'], policy)
                source = inspect_source(path)
                existing = next((s for s in project["sources"] if s.get("path") == source["path"]), None)
                label = entry.get("id")
                if label is None:
                    label = existing["id"] if existing else re.sub(r"[^a-z0-9_]+", "_", path.stem.lower()).strip("_")
                    if not label or not label[0].isalpha():
                        label = "source_" + label
                    label = label[:64]
                    if any(s["id"] == label and s.get("path") != source["path"] for s in project["sources"]):
                        raise ProjectError("A source already uses this filename. Provide a distinct label, or the existing label to relink it.")
                source_id = identifier(label)
                if existing and existing["id"] != source_id:
                    raise ProjectError("This path is already registered as " + existing["id"])
                description = entry.get('description', (existing or {}).get('description'))
                if description is not None:
                    source['description'] = project_data.text(description, 'Source description')
                source.update(id=source_id, approved_at=now(), access_revision=policy['revision'])
                additions.append(source)
            if len({s["id"] for s in additions}) != len(additions):
                raise ProjectError("Source IDs must be unique")
            if len({s["path"] for s in additions}) != len(additions):
                raise ProjectError("Register each input path only once")
            replaced = {s["id"] for s in additions}
            project["sources"] = [s for s in project["sources"] if s["id"] not in replaced] + additions
            self._event(project, "sources_approved", sources=[{k: v for k, v in s.items() if k != "snapshot"} for s in additions],
                        context=context or {})
            return self.view(project_id)

    def inputs(self, project_id, path=None, offset=0, limit=100):
        with self.guard:
            return project_access.discover(self._get(project_id), path, offset, limit)

    def data(self, project_id):
        with self.guard:
            return project_data.catalog(self._get(project_id))

    def record_data(self, project_id, spec, context=None):
        with self.guard:
            project = self._get(project_id)
            self._idle(project)
            dataset = project_data.record(project, spec, now(), context or {})
            project['processed_data'] = [d for d in project.get('processed_data', []) if d['path'] != dataset['path']] + [dataset]
            self._event(project, 'processed_data_recorded', dataset=dataset)
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
                specification = read_file(self._path(project, "dependencies", "pyproject.toml")).decode()
                if project.get("layout") == LAYOUT:
                    project["specification"] = specification
                elif specification != project["specification"]:
                    raise ProjectError("pyproject.toml changed outside the broker; restore it before preparing this environment")
                old = project["environment"]
                lock = read_file(self._authority(project) / "uv.lock") if old else None
                if project.get("layout") == LAYOUT:
                    tracked_lock = self._path(project, "dependencies", "uv.lock")
                    lock = read_file(tracked_lock) if tracked_lock.exists() else None
                environment, lock = self.runner.prepare(root, project["specification"], lock, job["_cancel"])
                environment.update(lock_sha256=digest(lock), specification_sha256=digest(project["specification"].encode()), prepared_at=now())
                with self.guard:
                    write_file(self._authority(project) / "uv.lock", lock)
                    write_file(self._path(project, "dependencies", "uv.lock"), lock)
                    write_file(self._path(project, "environment", "runtime.json"), encoded(environment))
                    project["environment"] = environment
                    # Store the interpreter in standard notebook metadata without
                    # restarting any existing kernel or replacing user cells.
                    for notebook in self._path(project, "notebooks").glob("*.ipynb"):
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
            selected = [s for s in project["sources"] if s["id"] in source_ids]
            policy = project_access.load(project)
            for source in selected:
                project_access.require(project, public_source(source, self._root(project))['path'], policy)
            if any(s["suffix"] not in {".csv", ".parquet"} for s in selected):
                raise ProjectError("Structured analyses support CSV or Parquet. Read other formats directly in the project notebook.")
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
            # Compatibility for old structured analyses; ordinary projects do
            # not scaffold a separate renderer or per-run notebooks.
            view = self._path(project, "reports", "views/result.html")
            if not view.exists():
                write_file(view, self._render({}, analysis_id, "unexecuted").replace("{}", "GUS_RESULT_JSON", 1))
            project["analyses"][analysis_id] = analysis
            write_file(self._path(project, "notebooks", f"{analysis_id}.ipynb"),
                       templates.notebook(analysis_id, parameters, selected, code, python=(project.get("environment") or {}).get("python"), layout=layout_paths(project)))
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
            if read_file(self._path(project, "dependencies", "pyproject.toml")).decode() != project["specification"]:
                raise ProjectError("Project dependency declaration changed outside the broker")
            if digest(read_file(self._path(project, "dependencies", "uv.lock"))) != project["environment"]["lock_sha256"]:
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
                    if source["id"] not in by_id or source_revision(by_id[source["id"]]) != source_revision(source):
                        raise ProjectError("Replay requires the exact input revision to still be approved")
                    selected.append(deepcopy(by_id[source["id"]]))
            policy = project_access.load(project)
            for source in selected:
                project_access.require(project, public_source(source, self._root(project))['path'], policy)
            run_id = "run_" + uuid.uuid4().hex
            directory = self._authority(project) / "runs" / run_id
            write_file(directory / "code/src/project_analysis/__init__.py", "")
            write_file(directory / f"code/src/project_analysis/{analysis_id}.py", code)
            write_file(directory / "pyproject.toml", project["specification"].replace('where = ["src"]', 'where = ["code/src"]'))
            write_file(directory / "worker.py", WORKER)
            inputs = []
            for source in selected:
                if "file_revision" in source:
                    verify_reference(source)
                else:
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
            for source in selected:
                project_access.require(project, public_source(source, self._root(project))['path'])
                verify_reference(source)
            if job["_cancel"].is_set():
                raise ProjectError("Operation cancelled")
            result = validate_result(output["result"])
            with self.guard:
                record.update(status="completed", finished_at=now(), result_sha256=digest(encoded(result)),
                              installed=output["environment"], log=output.get("log", "")[-32768:])
                write_file(directory / "result.json", encoded(result))
                rendered = self._render(result, analysis_id, run_id)
                nb = templates.notebook(analysis_id, parameters, selected, code, result, run_id, rendered,
                                        python=environment.get("python"), layout=layout_paths(project))
                write_file(directory / "notebook.ipynb", nb)
                write_file(directory / "view.html", rendered)
                record["notebook_sha256"] = digest(nb.encode())
                write_file(self._root(project) / f"results/runs/{run_id}/result.json", encoded(result))
                write_file(self._root(project) / f"results/runs/{run_id}/notebook.ipynb", nb)
                write_file(self._path(project, "outputs", run_id, "result.json"), encoded(result))
                write_file(self._path(project, "notebooks", f"{analysis_id}.ipynb"), nb)
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
                for source in selected:
                    verify_reference(source)
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
            path = self._path(project, "reports", "deck.html")
            old = read_file(path, limit=None).decode()
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
            path = self._path(project, "reports", "deck.html")
            old = read_file(path, limit=None).decode()
            if text is not None:
                if not isinstance(text, str):
                    raise ProjectError("Report must be HTML text")
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
