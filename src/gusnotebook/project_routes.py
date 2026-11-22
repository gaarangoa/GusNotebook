"""Authenticated project UI endpoints. Shared by the project UI and terminal CLI."""

import json
from flask import Blueprint, Response, current_app, jsonify, request

from .project_files import ProjectError

project_routes = Blueprint("projects", __name__)


def manager():
    return current_app.extensions["gusnotebook"].projects


def body():
    if request.content_length and request.content_length > 5 * 1024 * 1024:
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
    return jsonify(manager().create(data.get("name"), data.get("location", ""))), 201


@project_routes.get("/api/projects/<project_id>")
def view(project_id):
    return jsonify(manager().view(project_id))


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
    data = body() if request.method == "POST" else {}
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
