"""Git UI APIs; credentials only cross the private helper/session channels."""

from flask import Blueprint, current_app, jsonify, request

from .git_process import GitError, redact

git_routes = Blueprint("git", __name__)


def manager():
    return current_app.extensions["gusnotebook"].git


def body():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise GitError("Expected a JSON object")
    return value


@git_routes.errorhandler(GitError)
@git_routes.errorhandler(OSError)
def error(exc):
    return jsonify(error=redact(exc)), 400


@git_routes.get("/api/git")
def status():
    return jsonify(manager().status(request.args.get("path")))


@git_routes.get("/api/git/diff")
def diff():
    return jsonify(manager().diff(request.args.get("path"), request.args.get("file"), request.args.get("staged") == "1"))


@git_routes.post("/api/git/operation")
def operation():
    manager().operate(body())
    return jsonify(status="started"), 202


@git_routes.delete("/api/git/operation")
def cancel():
    manager().cancel()
    return jsonify(status="canceling")


@git_routes.get("/api/git/auth")
def auth_status():
    return jsonify(manager().auth.snapshot())


@git_routes.post("/api/git/auth")
def auth():
    manager().auth.refresh(login=body().get("login") is True)
    return jsonify(status="started"), 202


@git_routes.delete("/api/git/auth")
def auth_cancel():
    manager().auth.cancel()
    return jsonify(status="canceling")


@git_routes.post("/api/git/bridge/<action>")
def bridge(action):
    # Only private tunnel hosts accept a session. The random per-connection
    # capability never appears in browser responses, URLs, files, or logs.
    if not current_app.config.get("PREVIEW_SINGLE_PORT"):
        return jsonify(error="Git credential forwarding requires a private GusNotebook tunnel"), 403
    secret = request.headers.get("X-GusNotebook-Git-Session")
    if not secret:
        return jsonify(error="Missing Git sharing session"), 403
    hub = manager().auth.remote
    value = body()
    if action == "attach":
        hub.attach(secret)
    elif action == "poll":
        return jsonify(hub.poll(secret, value.get("replies", {}), value.get("account")))
    elif action == "detach":
        hub.close(secret)
    else:
        raise GitError("Unknown sharing action")
    return jsonify(status="ok")
