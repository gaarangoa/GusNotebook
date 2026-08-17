"""GusNotebook — notebooks plus embedded Claude Code and Codex terminals.

Left pane: tabs — notebooks (each with its own kernel and interpreter), text
files, images. Right pane: Claude Code or Codex PTYs, opened on demand and rooted
wherever you ask, so code an agent writes lands directly in the notebook.

Run it with `gusnotebook` in the directory you want to work in. That directory
is the file browser's root and the default session's root; the app's own state
lives elsewhere (see `paths.py`), so nothing is written into your project.
"""

import difflib
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

from flask import (Flask, Response, render_template, request, jsonify,
                   send_file)
from flask_sock import Sock

from . import bus
from . import error_help
from . import files
from . import llm
from . import notebook as notebook_mod
from . import paths
from . import preview
from . import sessions as sessions_mod
from . import skills as skills_mod
from . import terminals
from . import textfile
from . import venvs
from .kernel import KernelPool
from .notebook import Registry

app = Flask(__name__,
            template_folder=str(paths.template_dir()),
            # The stylesheet and the page's eight JS files. Both folders are
            # named rather than left to Flask(__name__)'s guess — see paths.py.
            static_folder=str(paths.static_dir()),
            static_url_path="/static")

# Support reverse proxy (e.g. Domino) — trust X-Forwarded-* headers
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

sock = Sock(app)


# Who is asking. The browser sends a per-page id on every mutating request; it's
# stamped onto the events that request publishes, so the page can tell its own
# echo from a change somebody else made and skip a reload it already did.
#
# Done here rather than in each route because a route that forgot would go back
# to re-rendering the whole notebook on every keystroke pause — the failure is
# invisible in the code and only shows up as a stall while typing. `gusnb` and
# external edits send nothing and get no origin, which is right: those are
# changes the page really does need to load.
@app.before_request
def _record_origin():
    bus.set_origin(request.headers.get("X-Client-Id"))


@app.teardown_request
def _clear_origin(_exc=None):
    # Threads are reused between requests; a stale origin would mislabel the
    # next caller's events as an echo and lose a repaint.
    bus.set_origin(None)


def _launch_notebook():
    """The notebook the app opens on, and the directory it works in.

    `WORK_DIR` is where the user ran `gusnotebook` — the project they meant, the
    way `jupyter lab` means the directory you launched it from. It's the file
    browser's root, the default session's root, and where a new file goes.

    The launch notebook is `NOTEBOOK=` if given, else the first `.ipynb` already
    in that directory (opening onto the user's own work beats opening onto a
    blank one), else `notebook.ipynb` there. Nothing is written into the project
    until the tab is actually opened, and a directory with no notebooks gets a
    new one rather than an error — but it goes in the state directory when the
    working directory isn't writable, since refusing to start over a read-only
    checkout would be absurd.
    """
    work = paths.work_dir()
    env = os.environ.get("NOTEBOOK")
    if env:
        return files.normalize(Path(env)), work
    try:
        existing = sorted(p for p in work.glob("*.ipynb") if p.is_file())
    except OSError:
        existing = []
    if existing:
        return files.normalize(existing[0]), work
    if os.access(work, os.W_OK):
        return files.normalize(work / paths.DEFAULT_NOTEBOOK), work
    return files.normalize(paths.state(paths.DEFAULT_NOTEBOOK)), work


NOTEBOOK_PATH, WORK_DIR = _launch_notebook()

notebooks = Registry()
texts = textfile.TextRegistry()
previews = preview.PreviewPool()
kernels = KernelPool(default_python=sys.executable)
terms = terminals.SessionPool()
notebooks.get(NOTEBOOK_PATH)          # the tab that's open on first load
notebook_mod.watch(notebooks)

# Sessions group tabs so several projects don't share one page.
store = sessions_mod.SessionStore()
store.ensure_default("Main", str(NOTEBOOK_PATH.parent), [str(NOTEBOOK_PATH)])

# Skills are markdown on disk, read by Claude and by the notebook's picker. The
# starter set is written only when there are none, so it demonstrates the format
# on a fresh install without reappearing after the user clears it out.
skills_mod.install_starters()


def restore_session_state():
    """Bring the stored sessions back in line with reality, then reopen.

    Two different kinds of staleness. Tabs are pruned against **disk** — a
    session's whole point is to survive a restart, so a still-existing file is
    kept even though nothing has it open yet. Terminals are pruned against
    **nothing**: PTYs die with the process, so every remembered id is gone.

    Only the current session's documents are opened. The others stay recorded
    and load when you switch to them, so ten sessions don't cost ten times the
    memory at boot.
    """
    for s in store.all():
        s.tabs = [p for p in s.tabs if Path(p).exists()]
    store.prune([p for s in store.all() for p in s.tabs], [])

    cur = store.current()
    if not cur:
        return
    for p in list(cur.tabs):
        try:
            if textfile.kind_of(p) == "notebook":
                notebooks.get(p)
            elif textfile.kind_of(p) == "text":
                texts.get(Path(p))
        except (OSError, ValueError):
            store.drop_tab(p)          # unreadable now; don't keep claiming it
    # The notebook the app was launched with is always reachable, even if it
    # belongs to no session — otherwise NOTEBOOK= would open into nothing.
    if not store.owns_tab(str(NOTEBOOK_PATH)) and not cur.tabs:
        store.add_tab(str(NOTEBOOK_PATH), cur.id)


restore_session_state()

# One execution at a time per notebook — different notebooks run concurrently.
_exec_locks = {}
_exec_locks_guard = threading.Lock()

# Where the user's caret is — the notebook *and* the cell, posted by the browser
# as the selection moves. This is what makes "the cell I'm on" answerable to
# `gusnb here`, so Claude can work on it without being told an id.
#
# One record, not one per notebook: a caret is somewhere, singular. Keying it by
# notebook meant an unqualified `gusnb here` fell back to doc_key()'s guess — the
# session's first notebook — which is exactly the tab the user is *not* looking
# at when it's wrong. The hook has no notebook to pass, so this has to answer on
# its own.
#
# In memory, not in sessions.json: it changes on every click, and a disk write
# per click to record something meaningless after a restart is a bad trade. A
# cursor that resets to nothing when the app restarts is correct — nobody is
# parked anywhere until they click.
_focus = {"notebook": None, "cell_id": None}
_focus_guard = threading.Lock()
_markup_focus = None
_markup_focus_serial = 0


def set_focus(key, cell_id):
    global _markup_focus
    with _focus_guard:
        if cell_id:
            _focus.update(notebook=str(key), cell_id=cell_id)
            _markup_focus = None
        elif _focus["notebook"] == str(key):
            _focus.update(notebook=None, cell_id=None)


def _source_selection(rendered, source, start, end):
    """Map DOM-serialized range boundaries back to source-preserving offsets."""
    if rendered == source:
        return start, end

    fragment = rendered[start:end]
    exact = source.find(fragment)
    if fragment and exact >= 0 and source.find(fragment, exact + 1) < 0:
        return exact, exact + len(fragment)

    matcher = difflib.SequenceMatcher(None, rendered, source, autojunk=False)

    def boundary(pos):
        for tag, left, right, source_left, _source_right in matcher.get_opcodes():
            if tag == "equal" and left <= pos <= right:
                return source_left + (pos - left)
        return None

    source_start, source_end = boundary(start), boundary(end)
    if source_start is None or source_end is None or source_start >= source_end:
        raise ValueError("could not map that visual range to source; select its text again")
    return source_start, source_end


def set_markup_focus(path, selection, source=None, expected_version=None):
    """Remember the exact serialized range selected in a visual document."""
    global _markup_focus, _markup_focus_serial
    path = str(path)
    with _focus_guard:
        if not selection:
            if _markup_focus and _markup_focus["path"] == path:
                _markup_focus = None
            return None

        actual_version = textfile.disk_version(path)
        if expected_version is not None and actual_version != expected_version:
            _markup_focus = None
            raise textfile.ExternalChangeError(
                f"{Path(path).name} changed on disk; reload it before selecting")

        rendered = selection["document"]
        start, end = int(selection["start"]), int(selection["end"])
        if not 0 <= start < end <= len(rendered):
            raise ValueError("invalid visual selection range")
        document = source if isinstance(source, str) else rendered
        if (len(rendered.encode("utf-8")) > textfile.MAX_BYTES or
                len(document.encode("utf-8")) > textfile.MAX_BYTES):
            raise ValueError("visual document is too large")
        start, end = _source_selection(rendered, document, start, end)

        _markup_focus_serial += 1
        _markup_focus = {
            "id": _markup_focus_serial,
            "path": path,
            "document": document,
            "start": start,
            "end": end,
            "html": document[start:end],
            "text": str(selection.get("text") or ""),
            "disk_version": actual_version,
        }
        _focus.update(notebook=None, cell_id=None)
        return dict(_markup_focus)


def get_markup_focus():
    global _markup_focus
    with _focus_guard:
        focus = dict(_markup_focus) if _markup_focus else None
    if not focus or not Path(focus["path"]).is_file():
        return None, None
    if textfile.disk_version(focus["path"]) != focus["disk_version"]:
        with _focus_guard:
            if _markup_focus and _markup_focus["id"] == focus["id"]:
                _markup_focus = None
        return None, focus["path"]
    return focus, None


# The last thing the user typed at an agent terminal, so a cell it rewrites
# can say what was asked for — the terminal's answer to the AI cell's prompt
# strip. Recorded by the same `UserPromptSubmit` hook that injects the focused
# cell, which already has the payload in hand.
#
# One record, in memory, like the focus above: there's one user typing one prompt
# at a time, and which prompt was live an hour ago is not worth a disk write.
_prompt = {"text": None, "at": 0.0}

# How long a prompt stays attributable. A cell rewritten by `gusnb set` from a
# plain shell hours after the last Claude prompt must not be labelled with it:
# attributing by "the prompt that was live when the write landed" is a heuristic,
# and a stale one is a wrong caption on the user's own notebook. Generous enough
# to cover a long agent loop, short enough that the next session starts clean.
PROMPT_TTL = 30 * 60


def set_prompt_text(text):
    text = (text or "").strip()
    with _focus_guard:
        _prompt.update(text=text or None, at=time.time())


def recent_prompt():
    """The live prompt, or None if there isn't one or it's gone stale."""
    with _focus_guard:
        text, at = _prompt["text"], _prompt["at"]
    if not text or time.time() - at > PROMPT_TTL:
        return None
    return text


def get_focus(key=None):
    """The focused (notebook, cell_id), or (None, None).

    `key` narrows it: a request that names a notebook wants that notebook's
    caret, and gets nothing if the caret is in a different one — better than
    silently answering about a tab the caller didn't ask about.

    The cell is checked against the document rather than returned blind: it may
    have been deleted since it was focused, and handing an agent a stale id
    would have it edit whatever that id no longer is.
    """
    with _focus_guard:
        path, cell_id = _focus["notebook"], _focus["cell_id"]
    if not path or not cell_id:
        return None, None
    if key is not None and str(key) != path:
        return None, None
    _, cell = get_nb(path).find(cell_id)
    return (path, cell_id) if cell is not None else (None, None)


def exec_lock(key):
    with _exec_locks_guard:
        lock = _exec_locks.get(str(key))
        if lock is None:
            lock = _exec_locks[str(key)] = threading.Lock()
        return lock


def doc_key():
    """The notebook a request targets.

    From ?notebook= or the JSON body's "notebook"; falls back to the first open
    notebook so single-notebook callers keep working.

    The fallback prefers the **current session's** first notebook. The registry
    holds every session's documents at once, so its first entry may belong to a
    session nobody is looking at — an unqualified `gusnb add` would land there.
    """
    raw = request.args.get("notebook")
    if not raw:
        raw = (request.get_json(silent=True) or {}).get("notebook")
    if not raw:
        paths = notebooks.paths()
        cur = store.current()
        mine = [p for p in (cur.tabs if cur else []) if p in set(paths)]
        raw = (mine or paths or [str(NOTEBOOK_PATH)])[0]
    return str(files.normalize(raw))


def get_nb(key):
    """The Notebook for a key, opening it if it isn't already."""
    return notebooks.get(key)


def kernel_python(key):
    """The interpreter this notebook runs on, explicit or inherited default."""
    k = kernels.peek(key)
    if k:
        return k.python
    return get_nb(key).get_python() or kernels.default_python


def kernel_for(key):
    doc = get_nb(key)
    k = kernels.get(key, cwd=Path(key).parent, python=doc.get_python())
    want = doc.get_python()
    if want and k.python != want:      # metadata changed since the kernel started
        k.restart(python=want)
    return k


@app.context_processor
def inject_base_url():
    return {"BASE_URL": os.environ.get("APP_BASE_URL", "")}


# --- Shell ---

@app.route("/")
def index():
    return render_template("index.html")


# --- Path completions for the cell editor ---

@app.route("/api/completions")
def api_completions():
    """List filesystem entries matching a partial path typed in a string literal.

    ?path=   the partial path the user has typed so far (may be empty)
    ?base=   the notebook's absolute path; completions are resolved relative to
             its directory so `../../data/` works as it would in the notebook

    Returns {completions: [{label, type}]} where type is "dir" or "file".
    """
    raw  = request.args.get("path", "")
    base = request.args.get("base", "")
    try:
        base_dir = Path(base).parent if base else WORK_DIR
        # Split into the directory prefix and the partial name being typed.
        # "../../data/fi" -> search_dir="../../data/", prefix="fi"
        if "/" in raw:
            dir_part, prefix = raw.rsplit("/", 1)
            search_dir = (base_dir / dir_part).resolve()
        else:
            dir_part, prefix = "", raw
            search_dir = base_dir.resolve()
        if not search_dir.is_dir():
            return jsonify({"completions": []})
        entries = []
        for p in sorted(search_dir.iterdir()):
            if p.name.startswith(".") and not prefix.startswith("."):
                continue
            if not p.name.startswith(prefix):
                continue
            label = (dir_part + "/" if dir_part else "") + p.name
            if p.is_dir():
                label += "/"
            entries.append({"label": label, "type": "dir" if p.is_dir() else "file"})
        return jsonify({"completions": entries[:60]})
    except Exception:
        return jsonify({"completions": []})


# --- File browser ---

@app.route("/api/files")
def api_files():
    """List a directory. Defaults to the current session's root."""
    cur = store.current()
    path = (request.args.get("path") or (cur.root if cur else None)
            or str(Path(doc_key()).parent))
    show_hidden = request.args.get("hidden") == "1"
    try:
        data = files.listdir(path, show_hidden=show_hidden)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    data["open"] = notebooks.paths()
    data["cwd"] = str(WORK_DIR)
    return jsonify(data)


@app.route("/api/files/new", methods=["POST"])
def api_new_file():
    """Create a file or folder in the browsed directory.

    A .ipynb name produces a real nbformat notebook (via the Notebook store, so
    it's the same document the pane opens), and the response says which kind of
    tab to open.
    """
    body = request.get_json(silent=True) or {}
    kind = body.get("kind", "file")
    try:
        target = files.new_path(body.get("directory") or str(WORK_DIR),
                                body.get("name"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        if kind == "dir":
            target.mkdir()
            return jsonify({"path": str(target), "kind": "dir"})
        if target.suffix == files.NOTEBOOK_SUFFIX:
            notebooks.get(str(files.normalize(target)))     # writes a blank .ipynb
            return jsonify({"path": str(files.normalize(target)),
                            "kind": "notebook"})
        target.touch()
    except OSError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"path": str(target),
                    "kind": textfile.kind_of(target)})


@app.route("/api/dirlist")
def api_dirlist():
    """List subdirectories and venv-like entries for the directory picker.

    Returns dirs plus any bin/python inside them so the picker can tell a
    venv from a plain folder without extra round-trips.
    ?path= the directory to list; defaults to the user's home directory.
    """
    raw = request.args.get("path", "").strip() or str(WORK_DIR)
    try:
        p = Path(raw).resolve()
        if not p.is_dir():
            return jsonify({"error": "not a directory"}), 400
        try:
            children = sorted(p.iterdir(), key=lambda x: x.name.lower())
        except PermissionError:
            # Try the parent so the user can navigate sideways rather than being stuck.
            return jsonify({"error": f"Permission denied: {p}"}), 403
        entries = []
        for child in children:
            if child.name.startswith(".") and child.name not in (".venv",):
                continue
            if not child.is_dir():
                continue
            is_venv = (child / "pyvenv.cfg").exists()
            python = str(child / "bin" / "python") if is_venv and (child / "bin" / "python").exists() else None
            entries.append({"name": child.name, "path": str(child),
                            "is_venv": is_venv, "python": python})
        parent = str(p.parent) if p.parent != p else None
        return jsonify({"path": str(p), "parent": parent, "entries": entries})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/files/rename", methods=["POST"])
def api_rename_file():
    body = request.get_json(silent=True) or {}
    src = body.get("path", "").strip()
    name = body.get("name", "").strip()
    if not src or not name:
        return jsonify({"error": "path and name are required"}), 400
    if "/" in name or "\\" in name or name in (".", ".."):
        return jsonify({"error": "name must be a single path component"}), 400
    src_path = Path(src)
    dst_path = src_path.parent / name
    if dst_path.exists():
        return jsonify({"error": f"{name} already exists"}), 400
    try:
        src_path.rename(dst_path)
    except OSError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"path": str(dst_path)})


@app.route("/api/files/delete", methods=["POST"])
def api_delete_file():
    import shutil
    body = request.get_json(silent=True) or {}
    path = body.get("path", "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    p = Path(path)
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    try:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    except OSError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


# --- Sessions: named groups of tabs ---

def restrictions_for(session=None):
    """What Claude may not do: the app-wide set unioned with this session's.

    One helper so every launch and every check asks the same question. A union,
    so a session can only tighten what Settings already forbids — see
    terminals.merge_restrictions.
    """
    base = llm.load_settings().get("claude_restrictions") or {}
    return terminals.merge_restrictions(base, session.restrictions if session else {})


def execution_blocked():
    """True when the current session forbids running cells.

    Read from the current session rather than a notebook's own, because a
    restriction is a property of the workspace you're working in, and `gusnb`
    doesn't name a session.
    """
    return bool(restrictions_for(store.current()).get("no_execute"))


def from_browser():
    """Whether this request came from the notebook page rather than a terminal.

    The page sends X-Client-Id on every request (it's how event echo is
    suppressed); `gusnb` deliberately sends nothing. So the header already
    distinguishes "the user pressed Run" from "an agent asked us to run", with
    no new plumbing.

    A guardrail, not a security boundary: a terminal could set the header with
    curl. That's the same property the deny rules have — both stop Claude's own
    tooling from doing something, and neither claims to contain a determined
    process running as the user.
    """
    return bool(request.headers.get("X-Client-Id"))


NO_EXECUTE_NOTE = ("execution is disabled for terminals in this session — "
                   "the cell is in the notebook, press ▶ to run it")


def session_json(s):
    """A session plus the live counts the list shows.

    Kernels and terminals are reported per session because "2 kernels live" is
    the whole reason switching doesn't tear anything down — you need to see what
    you left running elsewhere.
    """
    return {**s.to_json(),
            "kernels": sum(1 for p in s.tabs
                           if kernels.status(p) not in ("stopped", "dead")),
            "terminals_live": sum(1 for t in s.terminals if terms.get(t)),
            "current": s.id == (store.current().id if store.current() else None)}


@app.route("/api/sessions")
def api_sessions():
    return jsonify({"sessions": [session_json(s) for s in store.all()],
                    "current": store.current().id if store.current() else None})


@app.route("/api/sessions", methods=["POST"])
def api_new_session():
    """Create a session and switch to it. Starts empty — no tabs, no kernels."""
    body = request.get_json(silent=True) or {}
    root = body.get("root") or str(WORK_DIR)
    try:
        root = str(files.normalize(root))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    s = store.create(body.get("name"), root, switch=bool(body.get("switch", True)))
    bus.publish("sessions_changed", session=s.id)
    return jsonify(session_json(s))


@app.route("/api/sessions/<sid>", methods=["POST"])
def api_update_session(sid):
    """Switch to a session, rename it, move its root, or set its guardrails."""
    body = request.get_json(silent=True) or {}
    try:
        if "name" in body:
            store.rename(sid, body["name"])
        if "root" in body:
            store.set_root(sid, str(files.normalize(body["root"])))
        if "instructions" in body:
            store.set_instructions(sid, body["instructions"])
        if "restrictions" in body:
            store.set_restrictions(sid, body["restrictions"])
        if body.get("switch"):
            store.switch(sid)
            # Opening happens here, not in the browser: switching must leave the
            # other sessions' documents and kernels alone, so the server decides
            # what's open rather than closing everything the page isn't showing.
            for p in list(store.get(sid).tabs):
                try:
                    if textfile.kind_of(p) == "notebook":
                        notebooks.get(p)
                    elif textfile.kind_of(p) == "text":
                        texts.get(Path(p))
                except (OSError, ValueError):
                    store.drop_tab(p)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    bus.publish("sessions_changed", session=sid)
    return jsonify(session_json(store.get(sid)))


@app.route("/api/sessions/<sid>", methods=["DELETE"])
def api_delete_session(sid):
    """Delete a session, releasing what it owned.

    This is the one place things are torn down: a session you can no longer see
    must not leave kernels and PTYs running where nothing can reach them.
    """
    try:
        s = store.delete(sid)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    for p in s.tabs:
        # Never close the launch notebook, and never close a tab another session
        # also holds — membership overlaps when you open the same file in two.
        if p == str(NOTEBOOK_PATH) or store.owns_tab(p):
            continue
        notebooks.close(p) or texts.close(p)
        previews.close(p)
        kernels.drop(p)
    for t in s.terminals:
        terms.close(t)
    bus.publish("sessions_changed", session=store.current().id)
    return jsonify({"status": "ok", "deleted": s.id,
                    "current": store.current().id})


# --- Tabs: open / close any file ---

@app.route("/api/tabs")
def api_tabs():
    """Everything currently open, so a page reload restores the tab bar.

    `tabs` is the current session's, in its order — other sessions' documents
    stay open server-side but aren't this page's tabs. `all_tabs` is everything,
    for gusnb and anything that works across sessions.
    """
    cur = store.current()
    mine = list(cur.tabs) if cur else []
    kinds = {p: "notebook" for p in notebooks.paths()}
    kinds.update({p: "text" for p in texts.paths()})
    return jsonify({
        "tabs": [{"path": p, "kind": kinds.get(p, "text")} for p in mine
                 if p in kinds],
        "all_tabs": [{"path": p, "kind": k} for p, k in kinds.items()],
        "primary": str(NOTEBOOK_PATH),
        "session": cur.id if cur else None,
        "session_name": cur.name if cur else None,
        "session_root": cur.root if cur else str(WORK_DIR),
    })


@app.route("/api/open", methods=["POST"])
def api_open():
    """Open a file as a tab. Notebooks get cells + a kernel; text gets an editor."""
    raw = (request.get_json(silent=True) or {}).get("path", "")
    if not raw:
        return jsonify({"error": "path is required"}), 400
    path = files.normalize(raw)
    if not path.is_absolute():
        return jsonify({"error": "path must be absolute"}), 400

    kind = textfile.kind_of(path)
    if kind == "notebook":
        doc = notebooks.get(path)
        store.add_tab(str(path))
        data = doc.to_json()
        data["kind"] = "notebook"
        data["kernel_status"] = kernels.status(str(path))
        data["kernel_python"] = kernel_python(str(path))
        return jsonify(data)

    if kind == "image":
        return jsonify({"path": str(path), "kind": "image",
                        "url": f"/api/raw?path={path}"})

    if not path.exists():
        return jsonify({"error": f"no such file: {path}"}), 404
    try:
        data = texts.get(path).to_json()
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    # Only after it loaded: a file refused for being binary or oversized never
    # became a tab, so recording it would resurrect it on the next reload.
    if path.suffix.lower() in textfile.MARKUP_SUFFIXES:
        try:
            server = previews.open(path)
        except OSError as e:
            return jsonify({"error": f"could not start preview server: {e}"}), 400
        data["preview_origin"] = server.origin
        data["preview_version"] = server.version()
    store.add_tab(str(path))
    return jsonify(data)


@app.route("/api/close", methods=["POST"])
def api_close():
    """Close a tab: drop the document and its kernel."""
    raw = (request.get_json(silent=True) or {}).get("path", "")
    key = str(files.normalize(raw))
    store.drop_tab(key)
    closed = notebooks.close(key) or texts.close(key)
    preview_closed = previews.close(key)
    kernels.drop(key)
    return jsonify({"status": "ok", "closed": closed,
                    "preview_closed": preview_closed,
                    "open": notebooks.paths()})


@app.route("/api/text", methods=["POST"])
def api_save_text():
    """Save a text tab."""
    body = request.get_json(silent=True) or {}
    path = files.normalize(body.get("path", ""))
    if "text" not in body:
        return jsonify({"error": "text is required"}), 400
    try:
        doc = texts.get(path)
        saved = (doc.save(body["text"], body["disk_version"])
                 if "disk_version" in body else doc.save(body["text"]))
        server = previews.peek(path)
        if server:
            server.sync_saved(body["text"])
        return jsonify(saved)
    except textfile.ExternalChangeError as e:
        bus.publish("text_external_changed", path=str(path),
                    disk_version=textfile.disk_version(path))
        return jsonify({"error": str(e), "code": "external_change"}), 409
    except OSError as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/text-version")
def api_text_version():
    """Cheap poll target so visual files follow agent writes on disk."""
    path = files.normalize(request.args.get("path", ""))
    if textfile.kind_of(path) != "text" or not path.is_file():
        return jsonify({"error": "no such text file"}), 404
    server = previews.peek(path)
    return jsonify({"path": str(path),
                    "disk_version": textfile.disk_version(path),
                    "preview_origin": server.origin if server else None,
                    "preview_version": server.version() if server else None})


@app.route("/api/preview", methods=["POST"])
def api_preview():
    """Serve the browser's current markup buffer from its localhost origin."""
    body = request.get_json(silent=True) or {}
    path = files.normalize(body.get("path", ""))
    source = body.get("source")
    nonce = body.get("nonce")
    parent_origin = body.get("parent_origin")
    if path.suffix.lower() not in textfile.MARKUP_SUFFIXES or not path.is_file():
        return jsonify({"error": "preview requires an HTML or SVG file"}), 400
    if not isinstance(source, str) or not isinstance(nonce, str):
        return jsonify({"error": "preview source and nonce are required"}), 400
    if len(source.encode("utf-8")) > textfile.MAX_BYTES:
        return jsonify({"error": "visual document is too large"}), 400
    if (not isinstance(parent_origin, str) or
            not parent_origin.startswith(("http://", "https://"))):
        return jsonify({"error": "valid parent origin is required"}), 400
    try:
        server = previews.open(path)
        return jsonify(server.render(source, nonce, parent_origin))
    except (OSError, UnicodeError) as e:
        return jsonify({"error": f"could not render preview: {e}"}), 400


@app.route("/api/previews")
def api_previews():
    """Live preview origins, primarily for lifecycle/status UI and tests."""
    return jsonify({"previews": previews.info()})


@app.route("/api/raw")
def api_raw():
    """Serve a file as-is — used to show images in a tab."""
    path = files.normalize(request.args.get("path", ""))
    if not path.is_file():
        return jsonify({"error": "no such file"}), 404
    return send_file(str(path))


# --- Notebook document API (all routes take ?notebook=/abs/path) ---

@app.route("/api/notebook")
def api_notebook():
    key = doc_key()
    data = get_nb(key).to_json()
    data["kind"] = "notebook"
    data["kernel_status"] = kernels.status(key)
    data["kernel_python"] = kernel_python(key)
    data["open"] = notebooks.paths()
    return jsonify(data)


@app.route("/api/cells", methods=["POST"])
def api_add_cell():
    body = request.get_json(silent=True) or {}
    cell = get_nb(doc_key()).add_cell(
        cell_type=body.get("cell_type", "code"),
        source=body.get("source", ""),
        index=body.get("index"),
        after=body.get("after"),
    )
    return jsonify(cell)


@app.route("/api/cells/<cell_id>", methods=["PATCH"])
def api_update_cell(cell_id):
    body = request.get_json(silent=True) or {}
    # `undoable` is asked for, not assumed: the browser PATCHes as the user
    # types, and recording every pause would bury the entry that matters.
    undoable = bool(body.get("undoable"))
    doc = get_nb(doc_key())
    cell = doc.update_cell(
        cell_id, source=body.get("source"), cell_type=body.get("cell_type"),
        undoable=undoable)
    if cell is None:
        return jsonify({"error": "no such cell"}), 404
    # An undoable write is by definition one the user didn't type — `gusnb set`
    # or `here`, i.e. an agent or a snippet. That's the moment to caption the cell
    # with what was asked for, and it's the only moment we can: the CLI has the
    # cell id but no idea what prompt sent it.
    if undoable and body.get("source") is not None:
        text = recent_prompt()
        if text:
            cell = doc.set_claude_prompt(cell_id, text) or cell
    return jsonify(cell)


@app.route("/api/cells/<cell_id>/undo", methods=["POST"])
def api_undo_cell(cell_id):
    """Put back the source an agent or a snippet replaced. One step, this cell."""
    before = get_nb(doc_key()).cell_json(cell_id)
    if before is None:
        return jsonify({"error": "no such cell"}), 404
    cell = get_nb(doc_key()).undo_cell(cell_id)
    if before["undo_depth"] == 0:
        return jsonify({"error": "nothing to undo in this cell"}), 400
    return jsonify(cell)


@app.route("/api/cells/<cell_id>", methods=["DELETE"])
def api_delete_cell(cell_id):
    if not get_nb(doc_key()).delete_cell(cell_id):
        return jsonify({"error": "no such cell"}), 404
    return jsonify({"status": "ok"})


@app.route("/api/cells/<cell_id>/move", methods=["POST"])
def api_move_cell(cell_id):
    body = request.get_json(silent=True) or {}
    if not get_nb(doc_key()).move_cell(cell_id, int(body.get("index", 0))):
        return jsonify({"error": "no such cell"}), 404
    return jsonify({"status": "ok"})


# --- The cell the user is on ---

@app.route("/api/focus", methods=["POST"])
def api_set_focus():
    """The browser reporting where the caret is. Fire-and-forget."""
    body = request.get_json(silent=True) or {}
    set_focus(doc_key(), body.get("cell_id"))
    return jsonify({"status": "ok"})


@app.route("/api/markup-focus", methods=["POST"])
def api_set_markup_focus():
    """The visual editor reporting an exact HTML/SVG range for the agent."""
    body = request.get_json(silent=True) or {}
    path = files.normalize(body.get("path", ""))
    if path.suffix.lower() not in textfile.MARKUP_SUFFIXES or not path.is_file():
        return jsonify({"error": "visual selection is not in an open HTML/SVG file"}), 400
    try:
        focus = set_markup_focus(path, body.get("selection"), body.get("source"),
                                 body.get("disk_version"))
    except textfile.ExternalChangeError as e:
        bus.publish("text_external_changed", path=str(path),
                    disk_version=textfile.disk_version(path))
        return jsonify({"error": str(e), "code": "external_change"}), 409
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": "ok", "selection_id": focus and focus["id"]})


@app.route("/api/markup-selection", methods=["PATCH"])
def api_replace_markup_selection():
    """Replace only the visual range the user selected, then notify the page."""
    global _markup_focus
    body = request.get_json(silent=True) or {}
    replacement = body.get("replacement")
    if not isinstance(replacement, str):
        return jsonify({"error": "replacement is required"}), 400
    try:
        wanted = int(body.get("selection_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "selection_id is required"}), 400

    with _focus_guard:
        focus = _markup_focus
        if not focus or focus["id"] != wanted:
            return jsonify({"error": "the visual selection changed; select the region again"}), 409
        path = Path(focus["path"])
        start, end = focus["start"], focus["end"]
        document = focus["document"]
        updated = document[:start] + replacement + document[end:]
        if len(updated.encode("utf-8")) > textfile.MAX_BYTES:
            return jsonify({"error": "replacement makes the visual document too large"}), 400
        try:
            saved = texts.get(path).save(updated, focus["disk_version"])
        except textfile.ExternalChangeError as e:
            _markup_focus = None
            bus.publish("text_external_changed", path=str(path),
                        disk_version=textfile.disk_version(path))
            return jsonify({"error": str(e), "code": "external_change"}), 409
        except OSError as e:
            return jsonify({"error": str(e)}), 400
        server = previews.peek(path)
        if server:
            server.sync_saved(updated)
        _markup_focus = {
            **focus,
            "document": updated,
            "end": start + len(replacement),
            "html": replacement,
            "text": "",
            "disk_version": saved["disk_version"],
        }
        selection_id = focus["id"]

    bus.publish("markup_changed", path=str(path), text=updated,
                disk_version=saved["disk_version"],
                selection_start=start, selection_end=start + len(replacement),
                selection_id=selection_id)
    return jsonify({"status": "ok", "path": str(path),
                    "selection_id": selection_id,
                    "selection_start": start,
                    "selection_end": start + len(replacement)})


@app.route("/api/prompt", methods=["POST"])
def api_set_prompt():
    """An agent terminal reporting what the user just asked for.

    Posted by the same `UserPromptSubmit` hook that injects the focused cell — it
    already reads the payload, and the `prompt` field is in it. Fire-and-forget
    and never fatal: a prompt has to go through whether or not this lands.
    """
    body = request.get_json(silent=True) or {}
    set_prompt_text(body.get("prompt"))
    return jsonify({"status": "ok"})


@app.route("/api/here")
def api_here():
    """The notebook cell or visual markup range the user is parked on.

    The point of entry for "work on this": a visual range returns source-mapped
    boundaries and document context; a notebook cell returns source and output.
    Both return a pinned id so a replacement can reject a target that moved.
    """
    # The focused notebook wins when the caller didn't name one: "here" means
    # the tab on screen, and doc_key()'s fallback is the session's first
    # notebook, which is a different thing whenever the user has switched tabs.
    named = request.args.get("notebook")
    visual, stale_path = (None, None) if named else get_markup_focus()
    if stale_path:
        bus.publish("text_external_changed", path=stale_path,
                    disk_version=textfile.disk_version(stale_path))
        return jsonify({
            "cell": None,
            "note": (f"{Path(stale_path).name} changed on disk after the visual "
                     "selection; reload it in GusNotebook and select the region again"),
            "code": "external_change",
            "path": stale_path,
        })
    if visual:
        before = visual["document"][max(0, visual["start"] - 2000):visual["start"]]
        after = visual["document"][visual["end"]:visual["end"] + 2000]
        return jsonify({
            "kind": "markup",
            "path": visual["path"],
            "selection_id": visual["id"],
            "selected_html": visual["html"],
            "selected_text": visual["text"],
            "context_before": before,
            "context_after": after,
            "document": visual["document"],
            "note": ("edit this file directly; change only the selected range "
                     "and preserve the rest of the document"),
        })

    key, cell_id = get_focus(named and doc_key())
    if not cell_id:
        return jsonify({"cell": None, "notebook": named and doc_key(),
                        "note": "no cell or visual region is selected in the browser"})
    nb = get_nb(key)
    cell = nb.cell_json(cell_id)
    return jsonify({
        "cell": cell,
        "cell_id": cell_id,
        "notebook": key,
        "kernel_status": kernels.status(key),
        "error": error_help._error_text(cell.get("outputs")) or None,
    })


# --- Execution ---

# How much stream text a *live* output event carries. The browser only renders
# the last few hundred lines of a console box anyway (MAX_STREAM_LINES in
# index.html), so sending a 40 MB training log ten times a second buys nothing —
# it just costs the JSON encode here, the transfer, and a full re-parse there,
# repeatedly, for text that will be thrown away on arrival.
#
# The cap applies only to the streaming events. `cell_done` sends the whole
# thing, and the notebook on disk keeps it, so nothing is actually lost: the
# elision is a rendering shortcut during the run, resolved when it ends.
LIVE_STREAM_TAIL = 64_000


def _trim_for_live(outputs):
    """Outputs with long stream text reduced to its tail, for live events."""
    trimmed = []
    for out in outputs:
        text = out.get("text")
        if out.get("output_type") == "stream" and isinstance(text, str) \
                and len(text) > LIVE_STREAM_TAIL:
            out = dict(out)
            dropped = len(text) - LIVE_STREAM_TAIL
            out["text"] = (f"… {dropped:,} earlier characters hidden; "
                           "the full output appears when the cell finishes …\n"
                           + text[-LIVE_STREAM_TAIL:])
        trimmed.append(out)
    return trimmed


def _run_cell(key, cell_id):
    """Execute one code cell, streaming outputs to listeners as they arrive."""
    nb = get_nb(key)
    _, cell = nb.find(cell_id)
    if cell is None:
        return {"error": "no such cell"}, 404
    if cell.get("cell_type") != "code":
        return {"error": "not a code cell"}, 400

    source = cell.get("source", "")
    if not source.strip():
        nb.set_outputs(cell_id, [], None)
        bus.publish("cell_done", cell_id=cell_id, execution_count=None,
                    outputs=[], notebook=key)
        return {"status": "ok", "execution_count": None, "outputs": []}, 200

    with exec_lock(key):
        bus.publish("cell_running", cell_id=cell_id, notebook=key)

        def on_output(outputs):
            bus.publish("cell_output", cell_id=cell_id,
                        outputs=_trim_for_live(outputs), notebook=key)

        try:
            count, outputs = kernel_for(key).execute(source, on_output=on_output)
        except Exception as e:
            outputs = [{
                "output_type": "error",
                "ename": type(e).__name__,
                "evalue": str(e),
                "traceback": [],
            }]
            count = None

        nb.set_outputs(cell_id, outputs, count)
        bus.publish("cell_done", cell_id=cell_id, execution_count=count,
                    outputs=[dict(o) for o in outputs], notebook=key)
        return {"status": "ok", "execution_count": count, "outputs": outputs}, 200


@app.route("/api/cells/<cell_id>/run", methods=["POST"])
def api_run_cell(cell_id):
    # Restricted sessions let Claude write cells but not run them. Enforced here
    # rather than by a deny rule because the kernel is the app's, not Claude's:
    # a rule can stop `gusnb run`, but nothing outside this process can stop the
    # code a cell contains from opening the file a Read rule denied.
    #
    # The source is still written, so the cell the user asked for is in the
    # notebook waiting for ▶ rather than lost with the refusal.
    if execution_blocked() and not from_browser():
        body = request.get_json(silent=True) or {}
        if body.get("source") is not None:
            get_nb(doc_key()).update_cell(cell_id, source=body["source"])
        return jsonify({"error": NO_EXECUTE_NOTE}), 403

    body = request.get_json(silent=True) or {}
    key = doc_key()
    if body.get("source") is not None:
        get_nb(key).update_cell(cell_id, source=body["source"])
    result, status = _run_cell(key, cell_id)
    return jsonify(result), status


@app.route("/api/run-all", methods=["POST"])
def api_run_all():
    if execution_blocked() and not from_browser():
        return jsonify({"error": NO_EXECUTE_NOTE}), 403
    key = doc_key()
    results = []
    for cell in list(get_nb(key).nb.cells):
        if cell.get("cell_type") == "code" and cell.get("source", "").strip():
            result, _ = _run_cell(key, cell["id"])
            results.append({"cell_id": cell["id"], **result})
    return jsonify({"status": "ok", "ran": len(results), "results": results})


@app.route("/api/clear-outputs", methods=["POST"])
def api_clear_outputs():
    body = request.get_json(silent=True) or {}
    get_nb(doc_key()).clear_outputs(body.get("cell_id"))
    return jsonify({"status": "ok"})


# --- Error help (single LLM call) ---

@app.route("/api/cells/<cell_id>/help", methods=["POST"])
def api_cell_help(cell_id):
    """Explain the error in this cell and propose a fix. One model call."""
    nb = get_nb(doc_key())
    idx, cell = nb.find(cell_id)
    if cell is None:
        return jsonify({"error": "no such cell"}), 404

    # Preceding code cells give the model context on where names came from.
    preceding = [
        c.get("source", "")
        for c in nb.nb.cells[:idx]
        if c.get("cell_type") == "code" and c.get("source", "").strip()
    ][-4:]

    try:
        result = error_help.explain(
            cell.get("source", ""),
            cell.get("outputs", []),
            context="\n\n".join(preceding) or None,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502
    return jsonify(result)


# --- Environments (venv per notebook) ---

@app.route("/api/venvs")
def api_venvs():
    """Interpreters we could use, nearest to the notebook first."""
    key = doc_key()
    current = get_nb(key).get_python() or kernels.default_python
    # The env in use may live nowhere we search (an explicit "Browse…" path),
    # so seed the list with it — otherwise the menu can't show what's current.
    found = venvs.discover(near=key, current=current)
    return jsonify({
        "venvs": found,
        "current": current,
        "notebook": key,
    })


@app.route("/api/venv", methods=["POST"])
def api_set_venv():
    """Bind a notebook to an interpreter and restart its kernel on it."""
    body = request.get_json(silent=True) or {}
    key = doc_key()
    try:
        info = venvs.validate(body.get("python", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not info["ipykernel"]:
        return jsonify({
            "error": f"ipykernel is not installed in {info['prefix']} — "
                     f"run: {info['python']} -m pip install ipykernel",
        }), 400

    get_nb(key).set_python(info["python"], label=info["label"],
                           version=info["version"])
    with exec_lock(key):
        k = kernels.get(key, cwd=Path(key).parent, python=info["python"])
        k.restart(python=info["python"])
    return jsonify({"status": "ok", "notebook": key, **info})


# --- Kernel control (per notebook) ---

@app.route("/api/kernel", methods=["GET"])
def api_kernel_status():
    key = doc_key()
    k = kernels.peek(key)
    return jsonify({
        "notebook": key,
        "status": k.status if k else "stopped",
        "alive": bool(k and k.is_alive()),
        "python": k.python if k else (get_nb(key).get_python()
                                      or kernels.default_python),
        "execution_count": k.execution_count if k else 0,
        "all": kernels.info(),
    })


@app.route("/api/kernel/<action>", methods=["POST"])
def api_kernel_action(action):
    key = doc_key()
    try:
        if action == "start":
            kernel_for(key).start()
        elif action == "restart":
            kernel_for(key).restart()
        elif action == "interrupt":
            k = kernels.peek(key)
            if k:
                k.interrupt()
        elif action == "shutdown":
            kernels.drop(key)
        else:
            return jsonify({"error": f"unknown action: {action}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"status": kernels.status(key), "notebook": key})


# --- Events (SSE): notebook changes, cell outputs, kernel status, view reload ---

@app.route("/events")
def events():
    def stream_events():
        q = bus.subscribe()
        # Replay current kernel states so a fresh tab isn't blank until an event.
        for key, info in kernels.info().items():
            yield bus.format_sse({"type": "kernel_status", "notebook": key,
                                  "status": info["status"],
                                  "python": info["python"]})
        try:
            while True:
                try:
                    yield bus.format_sse(q.get(timeout=15))
                except Exception:
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(q)

    return Response(stream_events(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


# --- Agent terminals (several, each rooted where you asked) ---

@app.route("/api/terminals")
def api_terminals():
    """Live sessions. `?session=mine` narrows to the current session's."""
    all_terms = terms.list()
    if request.args.get("session") == "mine":
        cur = store.current()
        mine = set(cur.terminals) if cur else set()
        all_terms = [t for t in all_terms if t["id"] in mine]
    return jsonify({"terminals": all_terms})


@app.route("/api/terminals", methods=["POST"])
def api_new_terminal():
    """Open a session in the file browser's folder.

    `kind` picks what runs: "shell", "codex", or Claude Code (the default).
    """
    body = request.get_json(silent=True) or {}
    # This session's own instructions and restrictions, on top of the app-wide
    # ones. Both agents receive the instructions at launch. Claude also receives
    # its native deny rules, which are fixed when the process starts.
    cur = store.current()
    cwd = body.get("cwd") or (cur.root if cur else None) or str(Path(doc_key()).parent)
    try:
        s = terms.create(str(files.normalize(cwd)),
                         command=terminals.command_for(
                             body.get("kind"),
                             cur.instructions if cur else None,
                             restrictions_for(cur)),
                         label=body.get("label"),
                         python=kernel_python(doc_key()))
    except (ValueError, OSError) as e:
        return jsonify({"error": str(e)}), 400
    store.add_terminal(s.id)
    return jsonify(s.to_json())


@app.route("/api/terminals/<sid>", methods=["DELETE"])
def api_close_terminal(sid):
    store.drop_terminal(sid)
    return jsonify({"status": "ok", "closed": terms.close(sid)})


@sock.route("/ws/<sid>")
def websocket(ws, sid):
    """Attach this socket to an existing session.

    The PTY is drained by the session's own thread, so a reload reattaches to a
    still-running Claude instead of starting a new one.
    """
    session = terms.get(sid)
    if session is None:
        ws.send(f"\r\n\x1b[31m[no such terminal: {sid}]\x1b[0m\r\n")
        return

    q = queue.Queue()
    session.attach(q)
    try:
        ws.send(session.scrollback())          # catch the client up
    except Exception:
        session.detach(q)
        return

    stop = threading.Event()

    def pty_to_ws():
        while not stop.is_set():
            try:
                data = q.get(timeout=0.5)
            except queue.Empty:
                continue
            if data is None:                   # session ended
                break
            try:
                ws.send(data)
            except Exception:
                break
        stop.set()

    reader = threading.Thread(target=pty_to_ws, daemon=True)
    reader.start()

    try:
        while not stop.is_set():
            data = ws.receive()
            if data is None:
                break
            if isinstance(data, str) and data.startswith("{"):
                try:
                    msg = json.loads(data)
                    if msg.get("type") == "resize":
                        session.resize(msg["rows"], msg["cols"])
                        continue
                except (ValueError, KeyError):
                    pass
            session.write(data)
    except Exception:
        pass
    finally:
        # Detach only — the session keeps running for the next attach.
        stop.set()
        session.detach(q)
        reader.join(timeout=1)


# --- Settings (inline LLM) ---

@app.route("/api/settings")
def api_settings():
    return jsonify(llm.settings_view())


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    body = request.get_json(silent=True) or {}
    llm.save_settings(body)
    return jsonify(llm.settings_view())


# --- Skills: snippets and practices, for Claude and for the notebook ---

@app.route("/api/skills")
def api_skills():
    """Every skill, with its code extracted for the picker."""
    return jsonify({"skills": skills_mod.all_skills(),
                    "dir": str(skills_mod.SKILLS_DIR)})


@app.route("/api/skills", methods=["POST"])
def api_save_skill():
    """Create a skill, or update the one named by `id`.

    Takes effect for Claude on the *next* session: a plugin's skills are read at
    startup, so a terminal already running won't see a skill added just now.
    """
    body = request.get_json(silent=True) or {}
    try:
        s = skills_mod.save(body.get("name"), body.get("description"),
                            body.get("body"), sid=body.get("id"))
    except (ValueError, OSError) as e:
        return jsonify({"error": str(e)}), 400
    bus.publish("skills_changed", skill=s["id"])
    return jsonify(s)


@app.route("/api/skills/<sid>", methods=["DELETE"])
def api_delete_skill(sid):
    gone = skills_mod.delete(sid)
    bus.publish("skills_changed", skill=sid)
    return jsonify({"status": "ok", "deleted": gone})


# --- Inline LLM: prompt in, notebook Python out ---

def _ai_context(nb, upto_index, limit=6):
    """Earlier code cells (with a little of their output) for the prompt."""
    out = []
    for c in nb.nb.cells[:upto_index]:
        if c.get("cell_type") != "code" or not c.get("source", "").strip():
            continue
        text = ""
        for o in c.get("outputs", []) or []:
            if o.get("output_type") == "stream":
                text += o.get("text", "")
            elif o.get("output_type") in ("execute_result", "display_data"):
                text += (o.get("data") or {}).get("text/plain", "")
            elif o.get("output_type") == "error":
                text += f"{o.get('ename')}: {o.get('evalue')}"
        out.append({"source": c.get("source", ""), "output": text})
    return out[-limit:]


@app.route("/api/cells/<cell_id>/ai", methods=["POST"])
def api_cell_ai(cell_id):
    """Turn this cell's prompt into code, then make it a code cell.

    The generated code replaces the cell's source, so what you get back is an
    ordinary code cell — editable and re-runnable, with no AI machinery left in
    the .ipynb. The prompt is kept in cell metadata so it's still visible.
    """
    key = doc_key()
    nb = get_nb(key)
    idx, cell = nb.find(cell_id)
    if cell is None:
        return jsonify({"error": "no such cell"}), 404

    body = request.get_json(silent=True) or {}
    prompt = body.get("prompt")
    if prompt is None:
        prompt = cell.get("source", "")

    names = None
    k = kernels.peek(key)
    if k and k.is_alive():
        try:
            _, outs = k.execute(
                "print(', '.join(n for n in sorted(globals())"
                " if not n.startswith('_'))[:1500])")
            names = "".join(o.get("text", "") for o in outs
                            if o.get("output_type") == "stream").strip()
        except Exception:
            names = None

    try:
        result = llm.generate(prompt, context=_ai_context(nb, idx),
                              variables=names or None)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 502

    # Code first, then the prompt: switching cell_type rebuilds the cell, so
    # metadata written before the switch wouldn't survive it.
    nb.update_cell(cell_id, source=result["code"], cell_type="code")
    nb.set_prompt(cell_id, prompt)
    return jsonify({**result, "cell": nb.cell_json(cell_id)})


def main(argv=None):
    """The `gusnotebook` command.

    Deliberately thin: everything above already ran at import, so this only
    decides where to listen and tells `terminals` the URL its `gusnb` should
    use — a terminal opened on port 9000 must not send its cells to whatever is
    on 8888.
    """
    import argparse

    p = argparse.ArgumentParser(
        prog="gusnotebook",
        description="Notebooks with embedded Claude Code and Codex terminals. "
                    "Serves the directory you run it in.")
    p.add_argument("-p", "--port", type=int,
                   default=int(os.environ.get("PORT", 8888)))
    p.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                   help="interface to bind (default: localhost only)")
    p.add_argument("--debug", action="store_true",
                   default=os.environ.get("FLASK_DEBUG") == "1")
    p.add_argument("--no-browser", action="store_true",
                   help="don't open a browser window")
    args = p.parse_args(argv)

    terminals.set_url(f"http://127.0.0.1:{args.port}")
    print(f"GusNotebook — http://127.0.0.1:{args.port}\n"
          f"  working in {WORK_DIR}\n"
          f"  state in   {paths.state_dir()}")
    if not args.no_browser:
        # After a beat, so the server is accepting connections by the time the
        # browser asks. A failure here is not a reason not to serve.
        import threading
        import webbrowser
        threading.Timer(
            1.0, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")
        ).start()

    # Reloader off: it would fork a second copy of every kernel and PTY.
    try:
        app.run(host=args.host, port=args.port, debug=args.debug,
                threaded=True, use_reloader=False)
    finally:
        previews.close_all()


if __name__ == "__main__":
    main()
