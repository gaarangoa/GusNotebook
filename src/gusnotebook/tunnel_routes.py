"""Local-only APIs for saved remote connections and their launch page."""

from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, render_template, request

from .tunnels import TunnelError

tunnel_routes = Blueprint("tunnel_manager", __name__)


def is_local_manager():
    return (not current_app.config.get("PREVIEW_SINGLE_PORT") and
            urlsplit(request.host_url).hostname in {"localhost", "127.0.0.1", "::1"})


@tunnel_routes.before_request
def require_local():
    if not is_local_manager():
        return jsonify(error="Open GusNotebook locally on this computer to manage tunnel connections."), 403


@tunnel_routes.errorhandler(TunnelError)
@tunnel_routes.errorhandler(OSError)
def tunnel_error(error):
    return jsonify(error=str(error)), 400


def manager():
    return current_app.extensions["gusnotebook"].tunnels


def body():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise TunnelError("Expected a JSON object")
    return value


@tunnel_routes.route("/api/tunnels", methods=["GET", "POST"])
def registry():
    if request.method == "POST":
        return jsonify(manager().add(body()))
    return jsonify(manager().snapshot())


@tunnel_routes.route("/api/tunnels/<identifier>", methods=["PATCH", "DELETE"])
def entry(identifier):
    if request.method == "PATCH":
        return jsonify(manager().rename(identifier, body().get("name")))
    manager().remove(identifier)
    return jsonify(status="ok")


@tunnel_routes.post("/api/tunnels/refresh")
def refresh():
    manager().refresh()
    return jsonify(manager().snapshot()), 202


@tunnel_routes.route("/api/tunnels/login", methods=["POST", "DELETE"])
def login():
    if request.method == "DELETE":
        manager().cancel_login()
    else:
        manager().refresh(body().get("provider", "github"))
    return jsonify(manager().snapshot()), 202


@tunnel_routes.post("/api/tunnels/<identifier>/connect")
def connect(identifier):
    manager().connect(identifier)
    return jsonify(manager().snapshot()), 202


@tunnel_routes.post("/api/tunnels/<identifier>/disconnect")
def disconnect(identifier):
    manager().disconnect(identifier)
    return jsonify(manager().snapshot())


@tunnel_routes.post("/api/tunnels/<identifier>/git/retry")
def retry_git_sharing(identifier):
    manager().retry_git_sharing(identifier)
    return jsonify(manager().snapshot()), 202


@tunnel_routes.get("/tunnels/connect")
def connect_page():
    return render_template("tunnel-connect.html")
