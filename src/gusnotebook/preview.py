"""Short-lived localhost web servers for visual HTML/SVG tabs.

Each open visual document gets a real browser origin.  That matters beyond
relative images and stylesheets: root-relative URLs, JavaScript modules,
``fetch()``, and origin-scoped browser APIs behave like they do on a normal
website instead of inheriting GusNotebook's own URL from an ``srcdoc`` iframe.

The server runs in a daemon thread inside the GusNotebook process.  It exposes
only the document's directory, injects the editor bridge into responses (never
into the file), and is explicitly stopped when the tab closes.
"""

import atexit
import html
import json
import mimetypes
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from . import paths
from .textfile import disk_version


_HEAD = re.compile(r"<head(?:\s[^>]*)?>", re.IGNORECASE)
_HTML = re.compile(r"<html(?:\s[^>]*)?>", re.IGNORECASE)
_DOCTYPE = re.compile(r"<!doctype[^>]*>", re.IGNORECASE)


def _svg_envelope(source):
    start = source.lower().find("<svg")
    if start < 0:
        return "", source, ""
    close = source.lower().rfind("</svg>")
    end = len(source) if close < start else close + len("</svg>")
    return source[:start], source[start:end], source[end:]


def _runtime_tag(config):
    payload = html.escape(json.dumps(config, separators=(",", ":")), quote=True)
    return (f'<script data-gusnotebook-runtime="bridge" '
            f'data-config="{payload}" '
            f'src="/.gusnotebook/editor.js"></script>')


def _editable_document(source, language, nonce, parent_origin):
    """Return a browser response with the transient editing bridge injected."""
    config = {
        "channel": "gusnotebook-markup-editor",
        "nonce": nonce,
        "mode": "svg" if language == "svg" else "html",
        "parentOrigin": parent_origin,
        "svgPrefix": "",
        "svgSuffix": "",
    }
    if language == "svg":
        prefix, body, suffix = _svg_envelope(source)
        config.update(svgPrefix=prefix, svgSuffix=suffix)
        return ("<!doctype html><html><head>" + _runtime_tag(config) +
                "<style data-gusnotebook-runtime=\"style\">"
                "html,body{margin:0;min-height:100%;}"
                "svg{display:block;max-width:100%;}</style></head><body>" +
                body + "</body></html>")

    tag = _runtime_tag(config)
    match = _HEAD.search(source)
    if match:
        return source[:match.end()] + tag + source[match.end():]
    match = _HTML.search(source)
    if match:
        return (source[:match.end()] + "<head>" + tag + "</head>" +
                source[match.end():])
    match = _DOCTYPE.search(source)
    if match:
        return (source[:match.end()] + "<head>" + tag + "</head>" +
                source[match.end():])
    return "<head>" + tag + "</head>" + source


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class _Handler(BaseHTTPRequestHandler):
    server_version = "GusNotebookPreview/1"

    def do_GET(self):
        self.server.preview.serve(self, head_only=False)

    def do_HEAD(self):
        self.server.preview.serve(self, head_only=True)

    def log_message(self, _format, *_args):
        # Asset chatter belongs in browser devtools, not beside the app URL.
        return


class PreviewServer:
    """One localhost origin rooted at one visual document's directory."""

    def __init__(self, path, bind_host="127.0.0.1"):
        self.path = Path(path).resolve()
        self.root = self.path.parent
        self._lock = threading.RLock()
        self._source = self.path.read_text(encoding="utf-8")
        self._language = self.path.suffix.lstrip(".").lower()
        self._nonce = ""
        self._parent_origin = "*"
        self._render_serial = 0
        self._asset_generation = 1
        self._observed = {self.path: disk_version(self.path)}
        self._runtime = (paths.static_dir() / "markup-runtime.js").read_bytes()

        self._httpd = _Server((bind_host, 0), _Handler)
        self._httpd.preview = self
        self.port = self._httpd.server_address[1]
        # Informational fallback only — never a real bind_host like "0.0.0.0".
        # Callers with a request in hand should use origin_for() instead, so
        # the origin matches whatever host the browser used to reach us.
        self.origin = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name=f"gusnb-preview-{self.port}", daemon=True)
        self._thread.start()

    def origin_for(self, host):
        """The origin as reached from `host` — the browser's own Host header."""
        return f"http://{host}:{self.port}"

    def render(self, source, nonce, parent_origin, host=None):
        """Set the transient browser buffer and return a cache-busted URL."""
        with self._lock:
            self._source = source
            self._nonce = nonce
            self._parent_origin = parent_origin or "*"
            self._language = self.path.suffix.lstrip(".").lower()
            self._render_serial += 1
            self._observed[self.path] = disk_version(self.path)
            relative = quote(self.path.name, safe="")
            origin = self.origin_for(host) if host else self.origin
            return {
                "url": f"{origin}/{relative}?gusnb={self._render_serial}",
                "origin": origin,
                "preview_version": self.version(),
            }

    def sync_saved(self, source):
        """Keep a browser save from looking like a later external asset edit."""
        with self._lock:
            self._source = source
            self._observed[self.path] = disk_version(self.path)

    def version(self):
        """A generation that changes when any already-served asset changes."""
        with self._lock:
            changed = False
            for path, seen in list(self._observed.items()):
                current = disk_version(path)
                if current != seen:
                    self._observed[path] = current
                    changed = True
            if changed:
                self._asset_generation += 1
            return str(self._asset_generation)

    def info(self, host=None):
        origin = self.origin_for(host) if host else self.origin
        return {"path": str(self.path), "origin": origin,
                "port": self.port, "preview_version": self.version()}

    def _resolve(self, raw_path):
        decoded = unquote(raw_path)
        if "\0" in decoded:
            return None
        relative = decoded.lstrip("/")
        try:
            target = (self.root / relative).resolve()
            target.relative_to(self.root)
        except (OSError, ValueError):
            return None
        if target.is_dir():
            target = (target / "index.html").resolve()
            try:
                target.relative_to(self.root)
            except ValueError:
                return None
        return target if target.is_file() else None

    @staticmethod
    def _write(handler, status, content, content_type, head_only=False):
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(content)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        if not head_only:
            handler.wfile.write(content)

    def serve(self, handler, head_only=False):
        route = urlsplit(handler.path).path
        if route == "/.gusnotebook/editor.js":
            self._write(handler, 200, self._runtime,
                        "text/javascript; charset=utf-8", head_only)
            return

        target = self._resolve(route)
        if target is None:
            self._write(handler, 404, b"Not found\n",
                        "text/plain; charset=utf-8", head_only)
            return
        try:
            if target == self.path:
                with self._lock:
                    rendered = _editable_document(
                        self._source, self._language, self._nonce,
                        self._parent_origin).encode("utf-8")
                self._write(handler, 200, rendered,
                            "text/html; charset=utf-8", head_only)
                return
            content = target.read_bytes()
        except OSError:
            self._write(handler, 404, b"Not found\n",
                        "text/plain; charset=utf-8", head_only)
            return

        with self._lock:
            self._observed[target] = disk_version(target)
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if mime.startswith("text/") or mime in {
                "application/javascript", "application/json", "image/svg+xml"}:
            mime += "; charset=utf-8"
        self._write(handler, 200, content, mime, head_only)

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)


class PreviewPool:
    """The preview servers owned by currently open visual tabs."""

    def __init__(self):
        self._servers = {}
        self._lock = threading.RLock()
        self._bind_host = "127.0.0.1"
        atexit.register(self.close_all)

    def set_bind_host(self, host):
        """Match the interface the main app was told to listen on."""
        self._bind_host = host

    def open(self, path):
        key = str(Path(path).resolve())
        with self._lock:
            server = self._servers.get(key)
            if server is None:
                server = PreviewServer(key, bind_host=self._bind_host)
                self._servers[key] = server
            return server

    def peek(self, path):
        with self._lock:
            return self._servers.get(str(Path(path).resolve()))

    def close(self, path):
        with self._lock:
            server = self._servers.pop(str(Path(path).resolve()), None)
        if server:
            server.close()
        return bool(server)

    def close_all(self):
        with self._lock:
            servers = list(self._servers.values())
            self._servers.clear()
        for server in servers:
            server.close()

    def info(self, host=None):
        with self._lock:
            servers = list(self._servers.values())
        return [server.info(host) for server in servers]
