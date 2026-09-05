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

from flask import (Blueprint, Flask, Response, current_app, render_template,
                   request, jsonify, send_file)
from werkzeug.local import LocalProxy
from flask_sock import Sock

from . import bus
from . import error_help
from . import environments
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

routes = Blueprint("notebook", __name__)
sock = Sock()


def runtime():
    return current_app.extensions["gusnotebook"]


def _resource(name):
    return LocalProxy(lambda: getattr(runtime(), name))


notebooks = _resource("notebooks")
texts = _resource("texts")
previews = _resource("previews")
kernels = _resource("kernels")
terms = _resource("terms")
store = _resource("store")
NOTEBOOK_PATH = _resource("notebook_path")
WORK_DIR = LocalProxy(paths.work_dir)


# Who is asking. The browser sends a per-page id on every mutating request; it's
# stamped onto the events that request publishes, so the page can tell its own
# echo from a change somebody else made and skip a reload it already did.
#
# Done here rather than in each route because a route that forgot would go back
# to re-rendering the whole notebook on every keystroke pause — the failure is
# invisible in the code and only shows up as a stall while typing. `gusnb` and
# external edits send nothing and get no origin, which is right: those are
# changes the page really does need to load.
@routes.before_request
def _record_origin():
    bus.set_origin(request.headers.get("X-Client-Id"))


@routes.teardown_request
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
    env = current_app.config.get("NOTEBOOK") or os.environ.get("NOTEBOOK")
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
            store.drop_tab(p, cur.id)  # unreadable now; don't keep claiming it
    # The notebook the app was launched with is always reachable, even if it
    # belongs to no session — otherwise NOTEBOOK= would open into nothing.
    if not store.owns_tab(str(NOTEBOOK_PATH)) and not cur.tabs:
        store.add_tab(str(NOTEBOOK_PATH), cur.id)




def request_session():
    """The workspace named by this client, falling back to the last-used one.

    Browser windows send X-Session-Id on every request. That makes "current" a
    property of the window rather than a process-wide switch: two windows can be
    parked in different workspaces while CLI callers without a header retain the
    persisted last-used default.
    """
    sid = request.headers.get("X-Session-Id")
    named = store.get(sid) if sid else None
    return named or store.current()


def request_session_id():
    session = request_session()
    return session.id if session else None

# One execution at a time per notebook — different notebooks run concurrently.
_exec_locks = _resource("exec_locks")
_exec_locks_guard = _resource("exec_locks_guard")

# Where the user's caret is — the notebook *and* the cell, posted by the browser
# as the selection moves. This is what makes "the cell I'm on" answerable to
# `gusnb here`, so Claude can work on it without being told an id.
#
# One record per workspace, not one per notebook: a caret is somewhere within a
# workspace, singular. Keeping the workspace key prevents an agent in one window
# from receiving the cell selected in another.
#
# In memory, not in sessions.json: it changes on every click, and a disk write
# per click to record something meaningless after a restart is a bad trade. A
# cursor that resets to nothing when the app restarts is correct — nobody is
# parked anywhere until they click.
_focuses = _resource("focuses")
_focus_guard = _resource("focus_guard")
_markup_focuses = _resource("markup_focuses")


def set_focus(key, cell_id, session_id=None):
    session_id = session_id or "default"
    with _focus_guard:
        focus = _focuses.setdefault(
            session_id, {"notebook": None, "cell_id": None})
        if cell_id:
            focus.update(notebook=str(key), cell_id=cell_id)
            _markup_focuses.pop(session_id, None)
        elif focus["notebook"] == str(key):
            focus.update(notebook=None, cell_id=None)


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


def set_markup_focus(path, selection, source=None, expected_version=None,
                     session_id=None):
    """Remember the exact serialized range selected in a visual document."""
    session_id = session_id or "default"
    path = str(path)
    with _focus_guard:
        markup_focus = _markup_focuses.get(session_id)
        if not selection:
            if markup_focus and markup_focus["path"] == path:
                _markup_focuses.pop(session_id, None)
            return None

        actual_version = textfile.disk_version(path)
        if expected_version is not None and actual_version != expected_version:
            _markup_focuses.pop(session_id, None)
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

        runtime().markup_focus_serial += 1
        markup_focus = {
            "id": runtime().markup_focus_serial,
            "path": path,
            "document": document,
            "start": start,
            "end": end,
            "html": document[start:end],
            "text": str(selection.get("text") or ""),
            "disk_version": actual_version,
        }
        _markup_focuses[session_id] = markup_focus
        _focuses.setdefault(session_id, {}).update(
            notebook=None, cell_id=None)
        return dict(markup_focus)


def get_markup_focus(session_id=None):
    session_id = session_id or "default"
    with _focus_guard:
        stored = _markup_focuses.get(session_id)
        focus = dict(stored) if stored else None
    if not focus or not Path(focus["path"]).is_file():
        return None, None
    if textfile.disk_version(focus["path"]) != focus["disk_version"]:
        with _focus_guard:
            stored = _markup_focuses.get(session_id)
            if stored and stored["id"] == focus["id"]:
                _markup_focuses.pop(session_id, None)
        return None, focus["path"]
    return focus, None


# The last thing the user typed at an agent terminal, so a cell it rewrites
# can say what was asked for — the terminal's answer to the AI cell's prompt
# strip. Recorded by the same `UserPromptSubmit` hook that injects the focused
# cell, which already has the payload in hand.
#
# One in-memory record per workspace, like the focus above. Two agents can be
# active at once, so prompt attribution must not cross their workspace boundary.
_prompts = _resource("prompts")

# How long a prompt stays attributable. A cell rewritten by `gusnb set` from a
# plain shell hours after the last Claude prompt must not be labelled with it:
# attributing by "the prompt that was live when the write landed" is a heuristic,
# and a stale one is a wrong caption on the user's own notebook. Generous enough
# to cover a long agent loop, short enough that the next session starts clean.
PROMPT_TTL = 30 * 60


def set_prompt_text(text, session_id=None):
    session_id = session_id or "default"
    text = (text or "").strip()
    with _focus_guard:
        _prompts[session_id] = {"text": text or None, "at": time.time()}


def recent_prompt(session_id=None):
    """The live prompt, or None if there isn't one or it's gone stale."""
    session_id = session_id or "default"
    with _focus_guard:
        prompt = _prompts.get(session_id) or {"text": None, "at": 0.0}
        text, at = prompt["text"], prompt["at"]
    if not text or time.time() - at > PROMPT_TTL:
        return None
    return text


def get_focus(key=None, session_id=None):
    """The focused (notebook, cell_id), or (None, None).

    `key` narrows it: a request that names a notebook wants that notebook's
    caret, and gets nothing if the caret is in a different one — better than
    silently answering about a tab the caller didn't ask about.

    The cell is checked against the document rather than returned blind: it may
    have been deleted since it was focused, and handing an agent a stale id
    would have it edit whatever that id no longer is.
    """
    session_id = session_id or "default"
    with _focus_guard:
        focus = _focuses.get(session_id) or {}
        path, cell_id = focus.get("notebook"), focus.get("cell_id")
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
        cur = request_session()
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


@routes.context_processor
def inject_base_url():
    return {"BASE_URL": request.script_root or current_app.config["APP_BASE_URL"]}


# --- Shell ---

@routes.route("/")
def index():
    template = "index.html" if current_app.extensions["authenticated"]() else "unlock.html"
    return render_template(template)


# --- Path completions for the cell editor ---

@routes.route("/api/completions")
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

@routes.route("/api/files")
def api_files():
    """List a directory. Defaults to the current session's root."""
    cur = request_session()
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


@routes.route("/api/files/new", methods=["POST"])
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


@routes.route("/api/files/upload", methods=["POST"])
def api_upload_files():
    """Upload one or more browser-selected files into the visible directory.

    Existing files are never overwritten implicitly. This mirrors the safety
    of New file and makes a duplicate name a useful, visible error instead of
    silently destroying the copy already on disk.
    """
    directory = request.form.get("directory") or str(WORK_DIR)
    incoming = [item for item in request.files.getlist("files")
                if item and item.filename]
    if not incoming:
        return jsonify({"error": "choose at least one file to upload"}), 400

    planned = []
    seen = set()
    try:
        for item in incoming:
            target = files.new_path(directory, item.filename)
            if target in seen:
                raise ValueError(f"{target.name} was selected more than once")
            seen.add(target)
            planned.append((item, target))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    written = []
    try:
        for item, target in planned:
            # Exclusive creation closes the small validation/save race: an
            # agent creating the same path at this instant still cannot be
            # overwritten by the upload.
            with target.open("xb") as destination:
                written.append(target)
                item.save(destination)
    except OSError as e:
        # Roll back only files created by this request. Nothing pre-existing is
        # touched because every target was validated above.
        for target in written:
            try:
                target.unlink()
            except OSError:
                pass
        return jsonify({"error": str(e)}), 400

    return jsonify({"uploaded": [{
        "name": target.name,
        "path": str(target),
        "kind": files.kind_of(target),
        "size": target.stat().st_size,
    } for target in written]})


@routes.route("/api/files/download")
def api_download_file():
    """Download one file from the Files sidebar as an attachment."""
    raw = request.args.get("path", "").strip()
    if not raw:
        return jsonify({"error": "path is required"}), 400
    path = files.normalize(raw)
    if not path.is_file():
        return jsonify({"error": "no such file"}), 404
    try:
        return send_file(str(path), as_attachment=True,
                         download_name=path.name, conditional=True)
    except OSError as e:
        return jsonify({"error": str(e)}), 400


@routes.route("/api/dirlist")
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
            try:
                if not child.is_dir():
                    continue
                python = venvs.python_bin(child)
                is_venv = bool(python and (
                    (child / "pyvenv.cfg").is_file() or
                    (child / "conda-meta").is_dir()))
            except OSError:
                # Listing a parent should not fail because one child cannot be
                # inspected. This happens on Linux with protected entries such
                # as .Trash-*/bin/python3; simply omit that entry.
                continue
            # Hidden directories are normally picker noise, but a structurally
            # valid environment remains useful whatever it happens to be named.
            if child.name.startswith(".") and not is_venv and request.args.get("include_hidden") != "1":
                continue
            entries.append({"name": child.name, "path": str(child),
                            "is_venv": is_venv,
                            "python": str(python) if is_venv else None})
        parent = str(p.parent) if p.parent != p else None
        return jsonify({"path": str(p), "parent": parent, "entries": entries})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


def relocate_file(source, destination):
    """Move open-document identities together, preserving idle kernels."""
    old, new = str(source), str(destination)
    known = set(notebooks.paths() + texts.paths())
    known.update(p for session in store.all() for p in session.tabs)
    known.add(old)
    mapping = {p: new + p[len(old):] for p in known
               if p == old or p.startswith(old + os.sep)}
    acquired = []
    with _exec_locks_guard:
        try:
            for path in sorted(mapping):
                lock = exec_lock(path)
                if not lock.acquire(blocking=False):
                    raise textfile.ExternalChangeError(
                        "A notebook is running; stop it or wait before renaming or moving it")
                acquired.append(lock)
                if _running_cell_ids(path):
                    raise textfile.ExternalChangeError(
                        "A notebook has queued execution; wait before renaming or moving it")
            source.rename(destination)
            for before, after in mapping.items():
                store.rename_tab(before, after)
                notebooks.rename(before, after)
                texts.rename(before, after)
                kernels.rename(before, after)
                previews.close(before)
                _exec_locks[after] = _exec_locks.pop(before)
                with _focus_guard:
                    for focus in _focuses.values():
                        if focus.get("notebook") == before:
                            focus["notebook"] = after
                    for focus in _markup_focuses.values():
                        if focus.get("path") == before:
                            focus["path"] = after
            if str(NOTEBOOK_PATH) in mapping:
                runtime().notebook_path = Path(mapping[str(NOTEBOOK_PATH)])
            for session in store.all():
                if session.root == old or session.root.startswith(old + os.sep):
                    store.set_root(session.id, new + session.root[len(old):])
        finally:
            for lock in reversed(acquired):
                lock.release()
    bus.publish("files_renamed", paths=mapping)


@routes.route("/api/files/rename", methods=["POST"])
def api_rename_file():
    body = request.get_json(silent=True) or {}
    src = body.get("path", "").strip()
    name = body.get("name", "").strip()
    if not src or not name:
        return jsonify({"error": "path and name are required"}), 400
    if "/" in name or "\\" in name or name in (".", ".."):
        return jsonify({"error": "name must be a single path component"}), 400
    src_path = files.normalize(src)
    dst_path = src_path.parent / name
    if dst_path.exists():
        return jsonify({"error": f"{name} already exists"}), 400
    try:
        relocate_file(src_path, dst_path)
    except textfile.ExternalChangeError:
        raise
    except OSError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"path": str(dst_path)})


@routes.route("/api/files/copy", methods=["POST"])
def api_copy_file():
    """Copy one file or folder to a directory under a caller-chosen name."""
    import shutil
    body = request.get_json(silent=True) or {}
    src = body.get("path", "").strip()
    directory = body.get("directory", "").strip()
    name = body.get("name", "").strip()
    if not src or not directory or not name:
        return jsonify({"error": "path, directory and name are required"}), 400
    try:
        src_path = files.normalize(src)
        parent = Path(directory).expanduser().resolve()
        target = files.new_path(parent, name)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not src_path.exists():
        return jsonify({"error": "source does not exist"}), 404
    if not parent.is_dir():
        return jsonify({"error": "target directory does not exist"}), 400
    try:
        if src_path.is_dir():
            target.resolve().relative_to(src_path.resolve())
            return jsonify({"error": "cannot copy a folder into itself"}), 400
    except ValueError:
        pass
    try:
        if src_path.is_dir():
            shutil.copytree(src_path, target)
        else:
            shutil.copy2(src_path, target)
    except OSError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"path": str(target), "kind": files.kind_of(target)})


@routes.route("/api/files/move", methods=["POST"])
def api_move_file():
    """Move one file or folder into a directory without overwriting."""
    body = request.get_json(silent=True) or {}
    src = body.get("path", "").strip()
    directory = body.get("directory", "").strip()
    if not src or not directory:
        return jsonify({"error": "path and directory are required"}), 400
    try:
        src_path = files.normalize(src)
        parent = Path(directory).expanduser().resolve()
    except OSError as e:
        return jsonify({"error": str(e)}), 400
    if not src_path.exists():
        return jsonify({"error": "source does not exist"}), 404
    if not parent.is_dir():
        return jsonify({"error": "target directory does not exist"}), 400
    target = parent / src_path.name
    if target.exists():
        return jsonify({"error": f"{target.name} already exists there"}), 400
    try:
        if src_path.is_dir():
            target.resolve().relative_to(src_path.resolve())
            return jsonify({"error": "cannot move a folder into itself"}), 400
    except ValueError:
        pass
    try:
        relocate_file(src_path, target)
    except textfile.ExternalChangeError:
        raise
    except OSError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"path": str(target), "kind": files.kind_of(target)})


@routes.route("/api/files/delete", methods=["POST"])
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
    return bool(restrictions_for(request_session()).get("no_execute"))


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


def session_json(s, current_id=None):
    """A session plus the live counts the list shows.

    Kernels and terminals are reported per session because "2 kernels live" is
    the whole reason switching doesn't tear anything down — you need to see what
    you left running elsewhere.
    """
    return {**s.to_json(),
            "kernels": sum(1 for p in s.tabs
                           if kernels.status(p) not in ("stopped", "dead")),
            "terminals_live": sum(1 for t in s.terminals if terms.get(t)),
            "current": s.id == current_id}


@routes.route("/api/sessions")
def api_sessions():
    current_id = request_session_id()
    return jsonify({"sessions": [session_json(s, current_id) for s in store.all()],
                    "current": current_id})


@routes.route("/api/sessions", methods=["POST"])
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
    return jsonify(session_json(s, s.id))


@routes.route("/api/sessions/<sid>", methods=["POST"])
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
        if "tabs" in body:
            store.set_tabs(sid, body["tabs"])
        if "active" in body:
            store.set_active(sid, body["active"])
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
                    store.drop_tab(p, sid)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    bus.publish("sessions_changed", session=sid)
    current_id = sid if body.get("switch") else request_session_id()
    return jsonify(session_json(store.get(sid), current_id))


@routes.route("/api/sessions/<sid>", methods=["DELETE"])
def api_delete_session(sid):
    """Delete a session, releasing what it owned.

    This is the one place things are torn down: a session you can no longer see
    must not leave kernels and PTYs running where nothing can reach them.
    """
    requested = request_session_id()
    try:
        s = store.delete(sid)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    with _focus_guard:
        _focuses.pop(sid, None)
        _markup_focuses.pop(sid, None)
        _prompts.pop(sid, None)
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
    fallback = store.current().id
    current = fallback if requested == sid else requested
    bus.publish("sessions_changed", session=current)
    return jsonify({"status": "ok", "deleted": s.id,
                    "current": current})


# --- Tabs: open / close any file ---

@routes.route("/api/tabs")
def api_tabs():
    """Everything currently open, so a page reload restores the tab bar.

    `tabs` is the current session's, in its order — other sessions' documents
    stay open server-side but aren't this page's tabs. `all_tabs` is everything,
    for gusnb and anything that works across sessions.
    """
    cur = request_session()
    mine = list(cur.tabs) if cur else []
    all_paths = []
    for session in store.all():
        for path in session.tabs:
            if path not in all_paths and Path(path).is_file():
                all_paths.append(path)
    kinds = {p: textfile.kind_of(p) for p in all_paths}
    return jsonify({
        "tabs": [{"path": p, "kind": kinds.get(p, "text")} for p in mine
                 if p in kinds and kinds[p] != "unknown"],
        "all_tabs": [{"path": p, "kind": kinds[p]} for p in all_paths
                     if kinds[p] != "unknown"],
        "primary": str(NOTEBOOK_PATH),
        "session": cur.id if cur else None,
        "session_name": cur.name if cur else None,
        "session_root": cur.root if cur else str(WORK_DIR),
        "active": cur.active if cur else None,
    })


@routes.route("/api/open", methods=["POST"])
def api_open():
    """Open a file as a tab. Notebooks get cells + a kernel; text gets an editor."""
    body = request.get_json(silent=True) or {}
    raw = body.get("path", "")
    if not raw:
        return jsonify({"error": "path is required"}), 400
    path = files.normalize(raw)
    if not path.is_absolute():
        return jsonify({"error": "path must be absolute"}), 400

    kind = textfile.kind_of(path)
    remember = not body.get("restore")
    session = request_session()
    if kind == "notebook":
        doc = notebooks.get(path)
        if remember:
            store.add_tab(str(path), session.id if session else None)
        data = doc.to_json()
        data["kind"] = "notebook"
        data["kernel_status"] = kernels.status(str(path))
        data["kernel_python"] = kernel_python(str(path))
        data["running_cells"] = _running_cell_ids(str(path))
        return jsonify(data)

    if kind == "image":
        if remember:
            store.add_tab(str(path), session.id if session else None)
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
        data["preview_origin"] = server.origin_for(request.host.split(":")[0])
        data["preview_version"] = server.version()
    if remember:
        store.add_tab(str(path), session.id if session else None)
    return jsonify(data)


@routes.route("/api/close", methods=["POST"])
def api_close():
    """Close a tab in this workspace; release shared resources at last owner."""
    raw = (request.get_json(silent=True) or {}).get("path", "")
    key = str(files.normalize(raw))
    session = request_session()
    store.drop_tab(key, session.id if session else None)
    closed = preview_closed = False
    if not store.owns_tab(key):
        closed = notebooks.close(key) or texts.close(key)
        preview_closed = previews.close(key)
        kernels.drop(key)
    return jsonify({"status": "ok", "closed": closed,
                    "preview_closed": preview_closed,
                    "open": notebooks.paths()})


@routes.route("/api/text", methods=["POST"])
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
    except (OSError, ValueError) as e:
        return jsonify({"error": str(e)}), 400


@routes.route("/api/text-version")
def api_text_version():
    """Cheap poll target so visual files follow agent writes on disk."""
    path = files.normalize(request.args.get("path", ""))
    if textfile.kind_of(path) != "text" or not path.is_file():
        return jsonify({"error": "no such text file"}), 404
    server = previews.peek(path)
    host = request.host.split(":")[0]
    return jsonify({"path": str(path),
                    "disk_version": textfile.disk_version(path),
                    "preview_origin": server.origin_for(host) if server else None,
                    "preview_version": server.version() if server else None})


@routes.route("/api/preview", methods=["POST"])
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
        host = request.host.split(":")[0]
        return jsonify(server.render(source, nonce, parent_origin, host))
    except (OSError, UnicodeError) as e:
        return jsonify({"error": f"could not render preview: {e}"}), 400


@routes.route("/api/previews")
def api_previews():
    """Live preview origins, primarily for lifecycle/status UI and tests."""
    return jsonify({"previews": previews.info(request.host.split(":")[0])})


@routes.route("/api/raw")
def api_raw():
    """Serve a file as-is — used to show images in a tab."""
    path = files.normalize(request.args.get("path", ""))
    if not path.is_file():
        return jsonify({"error": "no such file"}), 404
    return send_file(str(path))


# --- Notebook document API (all routes take ?notebook=/abs/path) ---

@routes.route("/api/notebook")
def api_notebook():
    key = doc_key()
    doc = get_nb(key)
    if request.args.get("force") == "1":
        doc.load()
    data = doc.to_json()
    data["kind"] = "notebook"
    data["kernel_status"] = kernels.status(key)
    data["kernel_python"] = kernel_python(key)
    data["running_cells"] = _running_cell_ids(key)
    data["open"] = notebooks.paths()
    return jsonify(data)


@routes.route("/api/cells", methods=["POST"])
def api_add_cell():
    body = request.get_json(silent=True) or {}
    cell = get_nb(doc_key()).add_cell(
        cell_type=body.get("cell_type", "code"),
        source=body.get("source", ""),
        index=body.get("index"),
        after=body.get("after"),
    )
    return jsonify(cell)


@routes.route("/api/cells/<cell_id>", methods=["PATCH"])
def api_update_cell(cell_id):
    body = request.get_json(silent=True) or {}
    # `undoable` is asked for, not assumed: the browser PATCHes as the user
    # types, and recording every pause would bury the entry that matters.
    undoable = bool(body.get("undoable"))
    doc = get_nb(doc_key())
    cell = doc.update_cell(
        cell_id, source=body.get("source"), cell_type=body.get("cell_type"),
        undoable=undoable,
        expected_source=body.get("expected_source", textfile.ANY_VERSION))
    if cell is None:
        return jsonify({"error": "no such cell"}), 404
    # An undoable write is by definition one the user didn't type — `gusnb set`
    # or `here`, i.e. an agent or a snippet. That's the moment to caption the cell
    # with what was asked for, and it's the only moment we can: the CLI has the
    # cell id but no idea what prompt sent it.
    if undoable and body.get("source") is not None:
        text = recent_prompt(request_session_id())
        if text:
            cell = doc.set_claude_prompt(cell_id, text) or cell
    return jsonify(cell)


@routes.route("/api/cells/<cell_id>/undo", methods=["POST"])
def api_undo_cell(cell_id):
    """Put back the source an agent or a snippet replaced. One step, this cell."""
    before = get_nb(doc_key()).cell_json(cell_id)
    if before is None:
        return jsonify({"error": "no such cell"}), 404
    cell = get_nb(doc_key()).undo_cell(cell_id)
    if before["undo_depth"] == 0:
        return jsonify({"error": "nothing to undo in this cell"}), 400
    return jsonify(cell)


@routes.route("/api/cells/<cell_id>", methods=["DELETE"])
def api_delete_cell(cell_id):
    if not get_nb(doc_key()).delete_cell(cell_id):
        return jsonify({"error": "no such cell"}), 404
    return jsonify({"status": "ok"})


@routes.route("/api/cells/<cell_id>/move", methods=["POST"])
def api_move_cell(cell_id):
    body = request.get_json(silent=True) or {}
    if not get_nb(doc_key()).move_cell(cell_id, int(body.get("index", 0))):
        return jsonify({"error": "no such cell"}), 404
    return jsonify({"status": "ok"})


# --- The cell the user is on ---

@routes.route("/api/focus", methods=["POST"])
def api_set_focus():
    """The browser reporting where the caret is. Fire-and-forget."""
    body = request.get_json(silent=True) or {}
    set_focus(doc_key(), body.get("cell_id"), request_session_id())
    return jsonify({"status": "ok"})


@routes.route("/api/markup-focus", methods=["POST"])
def api_set_markup_focus():
    """The visual editor reporting an exact HTML/SVG range for the agent."""
    body = request.get_json(silent=True) or {}
    path = files.normalize(body.get("path", ""))
    if path.suffix.lower() not in textfile.MARKUP_SUFFIXES or not path.is_file():
        return jsonify({"error": "visual selection is not in an open HTML/SVG file"}), 400
    try:
        focus = set_markup_focus(path, body.get("selection"), body.get("source"),
                                 body.get("disk_version"), request_session_id())
    except textfile.ExternalChangeError as e:
        bus.publish("text_external_changed", path=str(path),
                    disk_version=textfile.disk_version(path))
        return jsonify({"error": str(e), "code": "external_change"}), 409
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": "ok", "selection_id": focus and focus["id"]})


@routes.route("/api/markup-selection", methods=["PATCH"])
def api_replace_markup_selection():
    """Replace only the visual range the user selected, then notify the page."""
    body = request.get_json(silent=True) or {}
    replacement = body.get("replacement")
    if not isinstance(replacement, str):
        return jsonify({"error": "replacement is required"}), 400
    try:
        wanted = int(body.get("selection_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "selection_id is required"}), 400

    workspace = request_session_id() or "default"
    with _focus_guard:
        focus = _markup_focuses.get(workspace)
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
            _markup_focuses.pop(workspace, None)
            bus.publish("text_external_changed", path=str(path),
                        disk_version=textfile.disk_version(path))
            return jsonify({"error": str(e), "code": "external_change"}), 409
        except OSError as e:
            return jsonify({"error": str(e)}), 400
        server = previews.peek(path)
        if server:
            server.sync_saved(updated)
        _markup_focuses[workspace] = {
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
                selection_id=selection_id, session=workspace)
    return jsonify({"status": "ok", "path": str(path),
                    "selection_id": selection_id,
                    "selection_start": start,
                    "selection_end": start + len(replacement)})


@routes.route("/api/prompt", methods=["POST"])
def api_set_prompt():
    """An agent terminal reporting what the user just asked for.

    Posted by the same `UserPromptSubmit` hook that injects the focused cell — it
    already reads the payload, and the `prompt` field is in it. Fire-and-forget
    and never fatal: a prompt has to go through whether or not this lands.
    """
    body = request.get_json(silent=True) or {}
    session = request_session()
    if session and body.get("prompt"):
        runtime().history.begin(session.id, request.headers.get("X-Terminal-Id", "cli"),
                                body["prompt"], session.tabs)
    set_prompt_text(body.get("prompt"), request_session_id())
    return jsonify({"status": "ok"})


@routes.route("/api/history")
def api_history():
    return jsonify(groups=runtime().history.list(request_session_id()))


@routes.route("/api/history", methods=["POST"])
def api_begin_history():
    session = request_session()
    body = request.get_json(silent=True) or {}
    group = runtime().history.begin(session.id, "manual", body.get("prompt"), session.tabs)
    return jsonify(id=group)


@routes.route("/api/history/<group_id>/finish", methods=["POST"])
def api_finish_history(group_id):
    try:
        return jsonify(runtime().history.finish(group_id, request_session_id()))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@routes.route("/api/history/<group_id>/undo", methods=["POST"])
def api_undo_history(group_id):
    # An executing cell can save outputs after a restore. Refuse until those
    # writes have finished, leaving both the kernel and the documents intact.
    with _exec_locks_guard:
        if any(_running_cell_ids(path) for path in request_session().tabs):
            return jsonify(error="Wait for running cells before restoring changes"), 409
        try:
            changed = runtime().history.undo(group_id, request_session_id(),
                        (request.get_json(silent=True) or {}).get("revision"))
        except textfile.ExternalChangeError:
            raise
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except OSError as exc:
            return jsonify(error=str(exc)), 500
        for path in changed:
            bus.publish("notebook_changed" if path.endswith(".ipynb") else "text_external_changed",
                        notebook=path, path=path, reason="history")
    return jsonify(restored=changed)


@routes.route("/api/here")
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
    workspace = request_session_id()
    visual, stale_path = ((None, None) if named
                          else get_markup_focus(workspace))
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

    key, cell_id = get_focus(named and doc_key(), workspace)
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

# A Run fetch and a Stop fetch use separate HTTP connections, so the browser's
# Stop can arrive first while Run is still being dispatched. Tokens let us
# remember that early cancellation and apply it to exactly the intended run.
_run_control_lock = _resource("run_control_lock")
_cancelled_runs = _resource("cancelled_runs")
_running_cells = _resource("running_cells")


def _remember_cancel(run_id):
    if not run_id:
        return
    now = time.monotonic()
    _cancelled_runs[run_id] = now
    # Tokens are normally removed when their run exits. This also bounds stale
    # tokens from a page which disappeared immediately after clicking Stop.
    for token, when in list(_cancelled_runs.items()):
        if now - when > 60:
            _cancelled_runs.pop(token, None)


def _running_cell_ids(key):
    with _run_control_lock:
        return dict(_running_cells.get(key, {}))


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


def _run_cell(key, cell_id, run_id=None):
    """Execute one code cell, streaming outputs to listeners as they arrive."""
    with _exec_locks_guard:
        if runtime().stop.is_set():
            raise RuntimeError("GusNotebook is shutting down")
        if not Path(key).is_file():
            return {"error": "notebook was moved or removed; reload before running"}, 404
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
                        outputs=[], notebook=key, run_id=run_id)
            return {"status": "ok", "execution_count": None, "outputs": []}, 200

        with _run_control_lock:
            _running_cells.setdefault(key, {})[cell_id] = run_id or ""

    try:
        with exec_lock(key):
            if runtime().stop.is_set():
                raise RuntimeError("GusNotebook is shutting down")
            k = kernel_for(key)
            with _run_control_lock:
                cancelled = bool(run_id and _cancelled_runs.pop(run_id, None))
                k.prepare_execution(run_id)

            bus.publish("cell_running", cell_id=cell_id, notebook=key,
                        run_id=run_id)

            def on_output(outputs):
                bus.publish("cell_output", cell_id=cell_id,
                            outputs=_trim_for_live(outputs), notebook=key,
                            kernel_status=k.status, python=k.python,
                            run_id=run_id)

            try:
                if cancelled:
                    k.cancel_prepared_execution(run_id)
                    count, outputs = None, [{
                        "output_type": "error",
                        "ename": "KeyboardInterrupt",
                        "evalue": "execution stopped before the kernel was ready",
                        "traceback": [],
                    }]
                    bus.publish("kernel_status", status=k.status, notebook=key,
                                python=k.python)
                else:
                    count, outputs = k.execute(source, on_output=on_output)
            except Exception as e:
                outputs = [{
                    "output_type": "error",
                    "ename": type(e).__name__,
                    "evalue": str(e),
                    "traceback": [],
                }]
                count = None
            finally:
                if run_id:
                    with _run_control_lock:
                        _cancelled_runs.pop(run_id, None)

        nb.set_outputs(cell_id, outputs, count)
        bus.publish("cell_done", cell_id=cell_id, execution_count=count,
                    outputs=[dict(o) for o in outputs], notebook=key,
                    kernel_status=k.status, python=k.python, run_id=run_id)
        return {"status": "ok", "execution_count": count, "outputs": outputs}, 200
    finally:
        with _run_control_lock:
            running = _running_cells.get(key)
            if running:
                running.pop(cell_id, None)
                if not running:
                    _running_cells.pop(key, None)


def _start_cell_run(key, cell_id, run_id, origin=None):
    """Run a browser-started cell without holding its HTTP connection open.

    The page already receives every intermediate output and the final result over
    SSE. Keeping the POST alive as well only consumes one of the browser's small
    pool of localhost connections; once an event stream and several terminal
    WebSockets are open, a new terminal or agent request can then sit queued until
    the cell ends. A daemon worker leaves that connection free immediately.

    Preserve the request origin on the worker so any events produced there still
    belong to the page which started the run. More importantly, always publish a
    terminal ``cell_done`` event if something fails outside Kernel.execute's own
    error handling: the browser's running marker must never be stranded.
    """
    application = current_app._get_current_object()

    def execute_work():
        bus.set_origin(origin)
        try:
            result, status = _run_cell(key, cell_id, run_id)
            if status != 200:
                raise ValueError(result["error"])
        except Exception as e:
            outputs = [{
                "output_type": "error",
                "ename": type(e).__name__,
                "evalue": str(e),
                "traceback": [],
            }]
            try:
                get_nb(key).set_outputs(cell_id, outputs, None)
            except Exception:
                pass
            k = kernels.peek(key)
            bus.publish("cell_done", cell_id=cell_id, execution_count=None,
                        outputs=outputs, notebook=key, run_id=run_id,
                        kernel_status=k.status if k else "dead",
                        python=k.python if k else kernel_python(key))
        finally:
            with _run_control_lock:
                running = _running_cells.get(key, {})
                if running.get(cell_id) == (run_id or ""):
                    running.pop(cell_id, None)
                if not running:
                    _running_cells.pop(key, None)
            bus.set_origin(None)

    def work():
        with application.app_context():
            try:
                execute_work()
            finally:
                with runtime().workers_lock:
                    runtime().workers.discard(threading.current_thread())

    worker = threading.Thread(target=work, name=f"cell-{cell_id}", daemon=True)
    with runtime().workers_lock:
        runtime().workers.add(worker)
    worker.start()


@routes.route("/api/cells/<cell_id>/run", methods=["POST"])
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
    with _exec_locks_guard:
        key = doc_key()
        if not Path(key).is_file():
            return jsonify(error="Notebook was moved or removed; reload before running"), 404
        _, target = get_nb(key).find(cell_id)
        if target is None:
            return jsonify(error="No such cell"), 404
        if target.get("cell_type") != "code":
            return jsonify(error="Not a code cell"), 400
        if body.get("source") is not None:
            get_nb(key).update_cell(cell_id, source=body["source"],
                                   expected_source=body.get("expected_source", textfile.ANY_VERSION))
        # Browser runs are event-driven: release this request as soon as the worker
        # starts, then let cell_running/output/done carry the lifecycle. CLI callers
        # retain the synchronous response because they have no SSE connection and
        # expect the execution result in this response.
        if from_browser():
            with _run_control_lock:
                _running_cells.setdefault(key, {})[cell_id] = body.get("run_id") or ""
            _start_cell_run(key, cell_id, body.get("run_id"), bus.origin())
            return jsonify({"status": "started", "run_id": body.get("run_id")}), 202
    result, status = _run_cell(key, cell_id, body.get("run_id"))
    return jsonify(result), status


@routes.route("/api/run-all", methods=["POST"])
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


@routes.route("/api/clear-outputs", methods=["POST"])
def api_clear_outputs():
    body = request.get_json(silent=True) or {}
    get_nb(doc_key()).clear_outputs(body.get("cell_id"))
    return jsonify({"status": "ok"})


# --- Error help (single LLM call) ---

@routes.route("/api/cells/<cell_id>/help", methods=["POST"])
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

@routes.route("/api/environments")
def api_environments():
    manager = runtime().environments
    session = request_session()
    found = venvs.discover(near=session.root, current=kernels.default_python,
                          registered=manager.registered(), probe=False)
    return jsonify(environments=found, uv_available=bool(manager.uv),
                   default_python=kernels.default_python, location=session.root,
                   jobs=manager.jobs_for(session.id))


@routes.route("/api/environments", methods=["POST"])
def api_create_environment():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify(error="Provide the environment name, location, and packages"), 400
    try:
        return jsonify(runtime().environments.create(body, request_session_id())), 202
    except (OSError, ValueError) as exc:
        return jsonify(error=str(exc)), 400


@routes.route("/api/environments/jobs/<job_id>", methods=["GET", "DELETE"])
def api_environment_job(job_id):
    manager = runtime().environments
    try:
        operation = manager.cancel if request.method == "DELETE" else manager.get
        return jsonify(operation(job_id, request_session_id()))
    except ValueError as exc:
        return jsonify(error=str(exc)), 404


@routes.route("/api/environments/packages")
def api_environment_packages():
    try:
        return jsonify(environments.inspect_packages(request.args.get("python", "")))
    except (OSError, ValueError) as exc:
        return jsonify(error=str(exc)), 400


@routes.route("/api/venvs")
def api_venvs():
    """Interpreters we could use, nearest to the notebook first."""
    key = doc_key()
    current = get_nb(key).get_python() or kernels.default_python
    # The env in use may live nowhere we search (an explicit "Browse…" path),
    # so seed the list with it — otherwise the menu can't show what's current.
    found = venvs.discover(near=key, current=current,
                          registered=runtime().environments.registered())
    return jsonify({
        "venvs": found,
        "current": current,
        "notebook": key,
    })


@routes.route("/api/venv", methods=["POST"])
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

    with _exec_locks_guard:
        lock = exec_lock(key)
        if _running_cell_ids(key) or not lock.acquire(blocking=False):
            return jsonify(error="Stop running cells before switching this notebook's environment"), 409
    try:
        get_nb(key).set_python(info["python"], label=info["label"], version=info["version"])
        k = kernels.get(key, cwd=Path(key).parent, python=info["python"])
        k.restart(python=info["python"])
    finally:
        lock.release()
    return jsonify({"status": "ok", "notebook": key, **info})


# --- Kernel control (per notebook) ---

@routes.route("/api/kernel", methods=["GET"])
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


@routes.route("/api/kernel/<action>", methods=["POST"])
def api_kernel_action(action):
    key = doc_key()
    try:
        if action == "start":
            kernel_for(key).start()
        elif action == "restart":
            kernel_for(key).restart()
        elif action == "interrupt":
            body = request.get_json(silent=True) or {}
            run_id = body.get("run_id")
            with _run_control_lock:
                _remember_cancel(run_id)
                k = kernels.peek(key)
                if k:
                    k.interrupt(run_id=run_id)
        elif action == "shutdown":
            kernels.drop(key)
        else:
            return jsonify({"error": f"unknown action: {action}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"status": kernels.status(key), "notebook": key})


# --- Events (SSE): notebook changes, cell outputs, kernel status, view reload ---

@routes.route("/events")
def events():
    event_bus = bus.current()
    kernel_states = kernels.info()

    def stream_events():
        q = event_bus.subscribe()
        try:
            # Replay current kernel states so a fresh tab isn't blank until an event.
            for key, info in kernel_states.items():
                yield bus.format_sse({"type": "kernel_status", "notebook": key,
                                      "status": info["status"],
                                      "python": info["python"]})
            while True:
                try:
                    yield bus.format_sse(q.get(timeout=15))
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            event_bus.unsubscribe(q)

    return Response(stream_events(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


# --- Agent terminals (several, each rooted where you asked) ---

@routes.route("/api/terminals")
def api_terminals():
    """Live sessions. `?session=mine` narrows to the current session's."""
    all_terms = terms.list()
    if request.args.get("session") == "mine":
        cur = request_session()
        mine = set(cur.terminals) if cur else set()
        all_terms = [t for t in all_terms if t["id"] in mine]
    return jsonify({"terminals": all_terms})


@routes.route("/api/terminals", methods=["POST"])
def api_new_terminal():
    """Open a session in the file browser's folder.

    `kind` picks what runs: "shell", "codex", or Claude Code (the default).
    """
    body = request.get_json(silent=True) or {}
    # This session's own instructions and restrictions, on top of the app-wide
    # ones. Both agents receive the instructions at launch. Claude also receives
    # its native deny rules, which are fixed when the process starts.
    cur = request_session()
    cwd = body.get("cwd") or (cur.root if cur else None) or str(Path(doc_key()).parent)
    try:
        s = terms.create(str(files.normalize(cwd)),
                         command=terminals.command_for(
                             body.get("kind"),
                             cur.instructions if cur else None,
                             restrictions_for(cur)),
                         label=body.get("label"),
                         python=kernel_python(doc_key()),
                         workspace=cur.id if cur else None)
    except (ValueError, OSError) as e:
        return jsonify({"error": str(e)}), 400
    store.add_terminal(s.id, cur.id if cur else None)
    return jsonify(s.to_json())


@routes.route("/api/terminals/<sid>", methods=["DELETE"])
def api_close_terminal(sid):
    store.drop_terminal(sid)
    return jsonify({"status": "ok", "closed": terms.close(sid)})


@sock.route("/ws/<sid>", bp=routes)
def websocket(ws, sid):
    """Attach this socket to an existing session.

    The PTY is drained by the session's own thread, so a reload reattaches to a
    still-running Claude instead of starting a new one.
    """
    session = terms.get(sid)
    if session is None:
        ws.send(f"\r\n\x1b[31m[no such terminal: {sid}]\x1b[0m\r\n")
        return
    client_workspace = request.args.get("session")
    if (client_workspace and session.workspace and
            client_workspace != session.workspace):
        ws.send("\r\n\x1b[31m[terminal belongs to another session]\x1b[0m\r\n")
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

@routes.route("/api/settings")
def api_settings():
    return jsonify(llm.settings_view())


@routes.route("/api/settings", methods=["POST"])
def api_save_settings():
    body = request.get_json(silent=True) or {}
    llm.save_settings(body)
    return jsonify(llm.settings_view())


# --- Skills: snippets and practices, for Claude and for the notebook ---

@routes.route("/api/skills")
def api_skills():
    """Every skill, with its code extracted for the picker."""
    return jsonify({"skills": skills_mod.all_skills(),
                    "dir": str(skills_mod.SKILLS_DIR)})


@routes.route("/api/skills", methods=["POST"])
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


@routes.route("/api/skills/<sid>", methods=["DELETE"])
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


@routes.route("/api/cells/<cell_id>/ai", methods=["POST"])
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


def create_app(config=None):
    """Build an isolated application. Importing this module starts nothing."""
    import secrets
    from werkzeug.middleware.proxy_fix import ProxyFix
    from . import auth
    from .runtime import Runtime

    application = Flask(__name__, template_folder=str(paths.template_dir()),
                        static_folder=str(paths.static_dir()), static_url_path="/static")
    application.config.update(
        AUTH_TOKEN=os.environ.get("GUSNOTEBOOK_TOKEN") or secrets.token_urlsafe(32),
        INSTANCE_ID=secrets.token_hex(8),
        ALLOWED_HOSTS=["localhost", "127.0.0.1", "::1"],
        TRUST_PROXY=False, START_WATCHERS=True, WORK_DIR=str(paths.work_dir()),
        APP_URL="http://127.0.0.1:8888", PREVIEW_HOST="127.0.0.1",
        APP_BASE_URL=os.environ.get("APP_BASE_URL", ""),
    )
    application.config.update(config or {})
    if application.config["TRUST_PROXY"]:
        application.wsgi_app = ProxyFix(application.wsgi_app, x_for=1, x_proto=1,
                                        x_host=1, x_prefix=1)

    # Strip a URL prefix when the reverse proxy forwards it unchanged.
    # APP_BASE_URL="/some/prefix" keeps Flask routes at "/" while SCRIPT_NAME
    # preserves the public prefix for generated URLs and authentication cookies.
    _base = application.config["APP_BASE_URL"].rstrip("/")
    application.config["APP_BASE_URL"] = _base
    if _base:
        _inner = application.wsgi_app

        def _strip_prefix(environ, start_response):
            path = environ.get("PATH_INFO", "")
            if path == _base or path.startswith(_base + "/"):
                environ["PATH_INFO"] = path[len(_base):] or "/"
                environ["SCRIPT_NAME"] = _base
            return _inner(environ, start_response)

        application.wsgi_app = _strip_prefix

    application.extensions["authenticated"] = auth.install(application)
    application.register_blueprint(routes)

    @application.errorhandler(notebook_mod.NotebookReadError)
    def notebook_error(exc):
        return jsonify(error=str(exc), code="notebook_read_error"), 409

    @application.errorhandler(textfile.ExternalChangeError)
    def conflict(exc):
        return jsonify(error=str(exc), code="external_change"), 409

    with application.app_context():
        state = Runtime()
        application.extensions["gusnotebook"] = state
        state.notebook_path, _work = _launch_notebook()
        state.previews.set_bind_host(application.config["PREVIEW_HOST"])
        try:
            state.notebooks.get(state.notebook_path)
        except notebook_mod.NotebookReadError as exc:
            application.logger.error("%s", exc)
        state.store.ensure_default("Main", str(paths.work_dir()), [str(state.notebook_path)])
        skills_mod.install_starters()
        restore_session_state()
        if application.config["START_WATCHERS"]:
            state.start()
    return application


def close_app(application):
    with application.app_context():
        application.extensions["gusnotebook"].close()


_default_app = None
_default_app_lock = threading.Lock()


def _get_default_app():
    global _default_app
    with _default_app_lock:
        if _default_app is None:
            _default_app = create_app()
        return _default_app


# WSGI compatibility; initialization occurs on first use, never on import.
app = LocalProxy(_get_default_app)


def main(argv=None):
    """Parse launch options before creating any documents or background work."""
    import argparse
    import signal
    import webbrowser
    from werkzeug.serving import make_server
    from .persistence import atomic_write

    parser = argparse.ArgumentParser(prog="gusnotebook", description=__doc__.split("\n")[0])
    parser.add_argument("-p", "--port", type=int, default=int(os.environ.get("PORT", 8888)))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--debug", action="store_true", default=os.environ.get("FLASK_DEBUG") == "1")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--trust-proxy", action="store_true",
                        help="trust one explicitly configured reverse proxy")
    parser.add_argument("--allowed-host", action="append", default=[],
                        help="additional hostname used to reach this server")
    args = parser.parse_args(argv)
    allowed = ["localhost", "127.0.0.1", "::1"] + args.allowed_host
    if args.host not in {"0.0.0.0", "::"}:
        allowed.append(args.host)
    link_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    if ":" in link_host:
        link_host = "[" + link_host + "]"
    # Reserve the port before opening documents or pruning persistent sessions.
    # A second launch on an occupied port must leave the live server's state alone.
    server = make_server(args.host, args.port, lambda _env, _start: (), threaded=True)
    base = f"http://{link_host}:{server.server_port}"
    try:
        application = create_app({"APP_URL": base, "PREVIEW_HOST": args.host,
                                  "ALLOWED_HOSTS": allowed, "TRUST_PROXY": args.trust_proxy,
                                  "DEBUG": args.debug})
    except BaseException:
        server.server_close()
        raise
    server.app = application
    url = base + "/#token=" + application.config["AUTH_TOKEN"]
    with application.app_context():
        connection = paths.state(f"server-{server.server_port}.json")
        atomic_write(connection, json.dumps({"url": base, "pid": os.getpid(),
                     "token": application.config["AUTH_TOKEN"]}), mode=0o600)
        print(f"GusNotebook — {url}\n  working in {paths.work_dir()}\n"
              f"  state in   {paths.state_dir()}", flush=True)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    def stop_server(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        close_app(application)
        server.server_close()
        try:
            if json.loads(connection.read_text()).get("pid") == os.getpid():
                connection.unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()
