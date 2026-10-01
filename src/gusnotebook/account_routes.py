"""Account status and a single entry point for sign-in."""

from flask import Blueprint, current_app, jsonify, request

from .git_process import GitError, redact
from .tunnel_routes import is_local_manager

account_routes = Blueprint("accounts", __name__)


@account_routes.errorhandler(GitError)
def error(exc):
    return jsonify(error=redact(exc)), 400


@account_routes.route("/api/accounts", methods=["GET", "POST", "DELETE"])
def accounts():
    manager = current_app.extensions["gusnotebook"].accounts
    local = is_local_manager()
    if request.method == "POST":
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise GitError("Expected a JSON object")
        manager.start(local, login=body.get("login") is True, provider=body.get("provider", "github"),
                      setup=body.get("setup") is True)
    elif request.method == "DELETE":
        manager.cancel(local=local)
    return jsonify(manager.snapshot(local)), 202 if request.method == "POST" else 200
