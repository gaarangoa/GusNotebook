"""Authenticated project UI endpoints. Shared by the project UI and terminal CLI."""

import json
from flask import Blueprint, Response, current_app, jsonify, request

from pathlib import Path
from .project_files import ProjectError
from .git_process import GitError, redact
from . import bus

project_routes = Blueprint("projects", __name__)


def manager():
    return current_app.extensions["gusnotebook"].projects


def linked_project():
    """Only an explicit terminal binding supplies automatic project context."""
    from .app import request_session
    runtime = current_app.extensions["gusnotebook"]
    terminal = runtime.terms.get(request.headers.get("X-Terminal-Id"))
    if not terminal or (terminal.workspace and terminal.workspace != request_session().id):
        return None
    if not terminal.project:
        return None
    return next((p for p in manager().list() if p['id'] == terminal.project), None)


def body(limit=5 * 1024 * 1024):
    if limit is not None and request.content_length and request.content_length > limit:
        raise ProjectError("Request exceeds 5 MB")
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ProjectError("Expected a JSON object")
    return value


@project_routes.errorhandler(ProjectError)
@project_routes.errorhandler(OSError)
@project_routes.errorhandler(SyntaxError)
@project_routes.errorhandler(ValueError)
def error(exc):
    return jsonify(error=str(exc)), 400


@project_routes.get("/api/projects")
def listing():
    return jsonify(projects=manager().list())


@project_routes.get("/api/projects/runner")
def runner():
    return jsonify(manager().runner.status())


@project_routes.post("/api/projects")
def create():
    data = body()
    project = manager().create(data.get("name"), data.get("location", ""))
    activate(project)
    return jsonify(project), 201


def activate(project):
    from .app import request_session, cell_audit_context
    from .project_agent import instructions_for
    runtime = current_app.extensions["gusnotebook"]
    project.update(manager().use_git(project["id"]))
    try:
        directory, root = runtime.git.root(project["path"])
        if root is None:
            runtime.git.git(directory, "init")
    except (GitError, OSError) as exc:
        project["git_warning"] = redact(exc)
    session = request_session()
    terminal = runtime.terms.get(request.headers.get("X-Terminal-Id"))
    if terminal and terminal.workspace and terminal.workspace != session.id:
        terminal = None
    if terminal:
        if terminal.project != project["id"]:
            context = cell_audit_context(agent=True)
            prompt = context.pop("prompt", None)
            if prompt:
                manager().record_agent_event(project["id"], "user_message", prompt, **context)
        terminal.project = project["id"]
    if Path(project["notebook"]).is_file():
        runtime.store.add_tab(project["notebook"], session.id)
    runtime.store.add_tab(project["report"], session.id)
    # A terminal opting in through create/open needs the rules immediately,
    # including during the same agent turn that created the project.
    project['instructions'] = instructions_for(project)
    bus.publish("project_opened", project_id=project["id"], session=session.id,
                terminal=terminal.id if terminal else None)


@project_routes.post("/api/projects/<project_id>/open")
def open_project(project_id):
    project = manager().use_git(project_id)
    activate(project)
    return jsonify(project)


@project_routes.get("/api/projects/context")
def context():
    from .project_agent import instructions_for
    linked = linked_project()
    if not linked:
        return jsonify(error="No linked project. Use gusnb project create NAME /parent/folder or gusnb project open PROJECT_ID."), 404
    project = manager().use_git(linked['id'])
    for key in ("events", "runs", "analyses", "tolerances"):
        project.pop(key, None)
    project["instructions"] = instructions_for(project)
    return jsonify(project)


@project_routes.post("/api/projects/<project_id>/publish")
def publish(project_id):
    data = body()
    return jsonify(manager().accept(project_id, data.get("run_id"), data.get("revision"), draft=True))


@project_routes.get("/api/projects/<project_id>")
def view(project_id):
    return jsonify(manager().use_git(project_id))


@project_routes.post("/api/projects/<project_id>/sources")
def sources(project_id):
    return jsonify(manager().sources(project_id, body().get("sources")))


@project_routes.delete("/api/projects/<project_id>/sources/<source_id>")
def revoke(project_id, source_id):
    return jsonify(manager().revoke(project_id, source_id))


@project_routes.post("/api/projects/<project_id>/prepare")
def prepare(project_id):
    return jsonify(manager().prepare(project_id)), 202


@project_routes.post("/api/projects/<project_id>/analyses/<analysis_id>")
def analysis(project_id, analysis_id):
    data = body()
    return jsonify(manager().save_analysis(project_id, analysis_id, data.get("parameters", {}), data.get("source_ids", []),
                                          data.get("code"), expected_revision=data.get("revision")))


@project_routes.post("/api/projects/<project_id>/analyses/<analysis_id>/run")
def run(project_id, analysis_id):
    from .app import execution_blocked, from_browser
    if execution_blocked() and not from_browser():
        return jsonify(error="Execution is disabled for agents in this session"), 403
    data = body()
    return jsonify(manager().run(project_id, analysis_id, data.get("replay"), data.get("package") is True)), 202


@project_routes.post("/api/projects/<project_id>/accept")
def accept(project_id):
    data = body()
    return jsonify(manager().accept(project_id, data.get("run_id"), data.get("revision")))


@project_routes.post("/api/projects/<project_id>/cancel")
def cancel(project_id):
    manager().cancel(project_id)
    return jsonify(status="cancelling")


@project_routes.route("/api/projects/<project_id>/report", methods=["GET", "POST"])
def report(project_id):
    # Reports carry self-contained figures, as in the normal HTML editor.
    data = body(limit=None) if request.method == "POST" else {}
    return jsonify(manager().report(project_id, data.get("html"), data.get("revision")))


@project_routes.get("/api/projects/<project_id>/runs/<run_id>/<name>")
def artifact(project_id, run_id, name):
    data = manager().artifact(project_id, run_id, name)
    if name == "analysis.py":
        response = Response(data, mimetype="text/plain")
        response.headers["Content-Disposition"] = 'attachment; filename="analysis.py"'
        return response
    if name == "view.html":
        # HTML is returned as text to be installed in an opaque sandbox, never
        # executed as an authenticated top-level app page.
        return jsonify(html=data.decode())
    response = Response(data, mimetype="application/json")
    if name == "notebook.ipynb":
        response.headers["Content-Disposition"] = 'attachment; filename="analysis.ipynb"'
    return response


@project_routes.get("/api/projects/<project_id>/history")
def history(project_id):
    store = manager()
    with store.guard:
        project = store._get(project_id)
        response = Response("\n".join(json.dumps(event) for event in project["events"]) + "\n", mimetype="application/x-ndjson")
        response.headers["Content-Disposition"] = 'attachment; filename="events.jsonl"'
        return response


@project_routes.post("/api/projects/<project_id>/notes")
def note(project_id):
    data = body()
    return jsonify(manager().record_agent_event(project_id, "assistant_message", data.get("text"),
                   terminal=request.headers.get("X-Terminal-Id", "cli")))
