"""Authentication for the local control API and embedded terminal sockets."""

import hmac
from urllib.parse import urlsplit

from flask import jsonify, request, make_response


def install(app):
    token = app.config["AUTH_TOKEN"]
    cookie = "gusnb_" + app.config["INSTANCE_ID"]
    app.config["AUTH_COOKIE"] = cookie

    def authenticated():
        supplied = request.headers.get("Authorization", "")
        if supplied.startswith("Bearer "):
            supplied = supplied[7:]
        else:
            supplied = request.cookies.get(cookie, "")
        return bool(supplied) and hmac.compare_digest(supplied.encode(), token.encode())

    @app.before_request
    def protect():
        host = urlsplit(request.host_url).hostname
        if host not in app.config["ALLOWED_HOSTS"]:
            return jsonify(error="Unrecognized server host"), 400
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            return jsonify(error="Requests must come from this app's origin"), 403
        if request.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
            return jsonify(error="Requests must come from this app's origin"), 403
        if request.endpoint == "static":
            return None
        if request.path == "/" or request.path == "/auth":
            return None
        if not authenticated():
            return jsonify(error="Open the launch link to unlock GusNotebook",
                           code="unauthorized"), 401

    @app.post("/auth")
    def authenticate():
        if not authenticated():
            return jsonify(error="Invalid launch token"), 401
        response = make_response(jsonify(status="ok"))
        response.set_cookie(cookie, token, httponly=True, samesite="Strict",
                            secure=request.is_secure,
                            path=request.script_root or "/")
        return response

    @app.after_request
    def headers(response):
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    return authenticated
