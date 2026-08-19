"""Sessions: named groups of tabs, so several projects don't share one page.

A session is a **view**, not a container. It records a name, a root directory for
the file panel, and which tabs and terminals belong to it. Switching sessions
changes what the browser shows and where the file panel points — it does not
close documents or shut kernels down, so a training run in another session keeps
going and you come back to a live kernel.

That "nothing is torn down" choice is why membership is tracked here rather than
inferred: the server holds every open document at once, so something has to say
which subset is yours right now.

Deleting a session is the one destructive operation — its tabs and kernels are
released, because a session you can no longer see must not leave kernels running
where nothing can reach them.

Not to be confused with `terminals.Session`, which is one PTY. A session here
can own several of those.

State lives in `sessions.json` beside `settings.json`, in the state directory
(see `paths.py`): absolute paths and a per-machine layout, so it belongs to the
user rather than to the installed package.
"""

import json
import os
import pathlib
import threading

from . import paths

STORE_PATH = paths.state("sessions.json")


def _slug(name):
    """An id from a name: readable in the JSON, safe as a dict key."""
    keep = [c.lower() if c.isalnum() else "-" for c in name.strip()]
    out = "".join(keep).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "session"


class Session:
    def __init__(self, sid, name, root, tabs=None, terminals=None, active=None,
                 instructions="", restrictions=None):
        self.id = sid
        self.name = name
        self.root = str(root)
        # Order is the tab order the browser shows, so it's a list, not a set.
        self.tabs = list(tabs or [])
        self.terminals = list(terminals or [])
        self.active = active
        # Standing instructions for agents in this session only, added to the
        # app-wide ones from Settings. A session is a project, and a project's
        # guardrails don't belong to every other project on the machine.
        self.instructions = instructions or ""
        # What Claude may not do here, on top of the app-wide set. Unioned with
        # it rather than replacing it: a session tightens, never loosens.
        self.restrictions = dict(restrictions or {})

    def to_json(self):
        return {"id": self.id, "name": self.name, "root": self.root,
                "tabs": list(self.tabs), "terminals": list(self.terminals),
                "active": self.active, "instructions": self.instructions,
                "restrictions": dict(self.restrictions)}

    @classmethod
    def from_json(cls, d):
        return cls(d.get("id") or _slug(d.get("name", "")),
                   d.get("name") or "Session",
                   d.get("root") or str(paths.work_dir()),
                   d.get("tabs"), d.get("terminals"), d.get("active"),
                   d.get("instructions") or "",
                   d.get("restrictions") or {})


class SessionStore:
    """Every session, which one is current, and what belongs to each."""

    def __init__(self, path=STORE_PATH):
        self.path = pathlib.Path(path)
        self._lock = threading.RLock()
        self._sessions = {}
        self._current = None
        self._load()

    # --- persistence ---

    def _load(self):
        data = {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        for d in data.get("sessions") or []:
            s = Session.from_json(d)
            self._sessions[s.id] = s
        self._current = data.get("current")
        if self._current not in self._sessions:
            self._current = next(iter(self._sessions), None)

    def _save(self):
        """Write atomically — a half-written file would lose every session."""
        payload = {"current": self._current,
                   "sessions": [s.to_json() for s in self._sessions.values()]}
        tmp = str(self.path) + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, str(self.path))
        except OSError:
            pass

    # --- the current session ---

    def ensure_default(self, name, root, tabs=()):
        """Guarantee at least one session exists, for a first-ever launch."""
        with self._lock:
            if self._sessions:
                return self.current()
            s = self.create(name, root, switch=True)
            for p in tabs:
                s.tabs.append(str(p))
            self._save()
            return s

    def current(self):
        with self._lock:
            return self._sessions.get(self._current)

    def switch(self, sid):
        with self._lock:
            if sid not in self._sessions:
                raise ValueError(f"no such session: {sid}")
            self._current = sid
            self._save()
            return self._sessions[sid]

    def all(self):
        with self._lock:
            return list(self._sessions.values())

    def get(self, sid):
        with self._lock:
            return self._sessions.get(sid)

    # --- lifecycle ---

    def create(self, name, root, switch=True):
        # self._lock is an RLock, so this is safe to call from ensure_default,
        # which is already holding it.
        with self._lock:
            name = (name or "").strip() or "Session"
            sid = base = _slug(name)
            # Two sessions may share a name; ids must not. Suffix rather than
            # refuse — the name is the user's label, not a key.
            n = 2
            while sid in self._sessions:
                sid = f"{base}-{n}"
                n += 1
            s = Session(sid, name, root)
            self._sessions[sid] = s
            if switch or self._current is None:
                self._current = sid
            self._save()
            return s

    def rename(self, sid, name):
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                raise ValueError(f"no such session: {sid}")
            name = (name or "").strip()
            if not name:
                raise ValueError("a session needs a name")
            s.name = name
            self._save()
            return s

    def set_instructions(self, sid, text):
        """Standing instructions for agents in this session.

        Read when a terminal starts, so this affects the next agent you open,
        not the ones already running — its instructions are fixed at launch.
        """
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                raise ValueError(f"no such session: {sid}")
            s.instructions = (text or "").strip()
            self._save()
            return s

    def set_restrictions(self, sid, restrictions):
        """What Claude may not do in this session, on top of the app-wide set.

        Read when a terminal starts, like the instructions, so this binds the
        next Claude you open rather than the ones already running — permission
        rules are fixed at launch along with the rest of the settings file.
        """
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                raise ValueError(f"no such session: {sid}")
            s.restrictions = dict(restrictions or {})
            self._save()
            return s

    def set_root(self, sid, root):
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                raise ValueError(f"no such session: {sid}")
            s.root = str(root)
            self._save()
            return s

    def set_active(self, sid, path):
        """Remember the tab visible in one workspace without changing membership."""
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                raise ValueError(f"no such session: {sid}")
            path = str(path) if path else None
            if path is not None and path not in s.tabs:
                raise ValueError("the active file is not open in this session")
            s.active = path
            self._save()
            return s

    def delete(self, sid):
        """Remove a session and report what it owned, for the caller to release.

        Refuses the last one: with no session there is no current root and no
        place for a new tab to go.
        """
        with self._lock:
            if sid not in self._sessions:
                raise ValueError(f"no such session: {sid}")
            if len(self._sessions) == 1:
                raise ValueError("the last session can't be closed")
            s = self._sessions.pop(sid)
            if self._current == sid:
                self._current = next(iter(self._sessions))
            self._save()
            return s

    # --- membership ---

    def add_tab(self, path, sid=None):
        """Record a tab as belonging to a session (the current one by default)."""
        with self._lock:
            s = self._sessions.get(sid or self._current)
            if s is None:
                return None
            path = str(path)
            if path not in s.tabs:
                s.tabs.append(path)
            s.active = path
            self._save()
            return s

    def drop_tab(self, path, sid=None):
        """Remove a tab from one session (the persisted current one by default).

        A path may intentionally be open in several workspaces. Closing it in one
        must not silently remove it from all the others.
        """
        with self._lock:
            path = str(path)
            s = self._sessions.get(sid or self._current)
            if s is None:
                return False
            hit = path in s.tabs
            if hit:
                s.tabs.remove(path)
            if s.active == path:
                s.active = s.tabs[-1] if s.tabs else None
            if hit:
                self._save()
            return hit

    def add_terminal(self, sid_of_term, sid=None):
        with self._lock:
            s = self._sessions.get(sid or self._current)
            if s is None:
                return None
            if sid_of_term not in s.terminals:
                s.terminals.append(sid_of_term)
            self._save()
            return s

    def drop_terminal(self, sid_of_term):
        with self._lock:
            hit = False
            for s in self._sessions.values():
                if sid_of_term in s.terminals:
                    s.terminals.remove(sid_of_term)
                    hit = True
            if hit:
                self._save()
            return hit

    def owns_tab(self, path):
        """The session a path belongs to, or None if it's loose."""
        with self._lock:
            path = str(path)
            for s in self._sessions.values():
                if path in s.tabs:
                    return s
            return None

    def prune(self, live_tabs, live_terminals):
        """Forget tabs and terminals that no longer exist.

        A restart drops every kernel and PTY, and files get deleted outside the
        app, so what was saved last time is a claim to check rather than trust.
        """
        with self._lock:
            live_tabs = {str(p) for p in live_tabs}
            live_terminals = set(live_terminals)
            changed = False
            for s in self._sessions.values():
                keep = [p for p in s.tabs if p in live_tabs]
                if keep != s.tabs:
                    s.tabs, changed = keep, True
                keep_t = [t for t in s.terminals if t in live_terminals]
                if keep_t != s.terminals:
                    s.terminals, changed = keep_t, True
                if s.active and s.active not in s.tabs:
                    s.active, changed = (s.tabs[-1] if s.tabs else None), True
            if changed:
                self._save()
