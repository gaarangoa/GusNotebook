"""Agent and shell terminal sessions — several at once, each rooted somewhere.

A session is a PTY running Claude Code, Codex, or a shell with a working
directory of its own. Sessions live here rather than inside a WebSocket handler
so that:

  * closing the browser tab (or reloading the page) doesn't kill a running
    agent — the reader thread keeps draining the PTY into a scrollback buffer
    and the UI reattaches to it,
  * several browser views can watch the same session,
  * "open a terminal here" is just create(cwd=...).

Nothing starts a session implicitly: the notebook opens with no terminal, and
the user asks for one.

What an agent session is launched with is decided in one place, `command_for`.
Both agents receive the standing instructions and current-target hook. Claude
also receives the gateway credential, skills plugin, and the user's
Claude-specific **restrictions** — see RESTRICTION_RULES.
"""

import errno
import fcntl
import json
import os
import pathlib
import pty
import select
import shlex
import shutil
import signal
import struct
import sys
import tempfile
import termios
import threading
from collections import deque

from . import bus

# Scrollback kept per session so a reattaching client sees recent history.
# Agent TUIs redraw on resize, so this only has to cover a screen or
# two of context — not an entire conversation.
BUFFER_BYTES = 256 * 1024

CLAUDE_COMMAND = ["claude", "--dangerously-skip-permissions"]

# Codex keeps the user's own model, login, approval, and sandbox settings. The
# app only asks it to render inline (important inside an embedded xterm) and
# supplies an app-owned, reviewable current-target hook in codex_args().
CODEX_COMMAND = ["codex", "--no-alt-screen"]

# Backwards-compatible name for callers that construct a Session directly.
DEFAULT_COMMAND = CLAUDE_COMMAND

# A plain interactive shell, for when you want a terminal rather than Claude.
# Login shell (-l) so it reads the user's profile and looks like their own.
SHELL_COMMAND = [os.environ.get("SHELL") or "/bin/bash", "-l"]

# --- restrictions -----------------------------------------------------------
#
# What a Claude terminal is *not* allowed to do, as `permissions.deny` rules.
# Rules rather than instructions because Claude Code enforces rules itself,
# while the system prompt only shapes what Claude tries: "do not read csv
# files" in prose is a request that is usually honoured and sometimes not.
#
# These survive the `--dangerously-skip-permissions` in DEFAULT_COMMAND — deny
# and ask rules apply in every permission mode, which is what makes this
# feature possible without changing how a session launches. Allow rules are the
# opposite (inert under that flag), so this is a deny-list only.
#
# Kept here, as data, so the answer to "what does this actually block?" is one
# list to read rather than something assembled across a modal and a route.
RESTRICTION_RULES = {
    # Bash that prints a file's contents. Needed explicitly because `cat`,
    # `head`, `tail` and `grep` are in Claude Code's built-in read-only set,
    # which runs with no prompt in every mode and is not configurable — so
    # nothing else stops them.
    #
    # Deliberately left alone: ls, find, wc, stat, du, tree, file. Those answer
    # "what is here and how big is it" without showing what's inside, which is
    # the "identify structure" half the user asked to keep.
    "no_bash_read": [
        "Bash(cat *)", "Bash(head *)", "Bash(tail *)", "Bash(sed *)",
        "Bash(awk *)", "Bash(less *)", "Bash(more *)", "Bash(strings *)",
        "Bash(od *)", "Bash(xxd *)", "Bash(jq *)", "Bash(grep *)",
        "Bash(rg *)", "Bash(egrep *)", "Bash(fgrep *)", "Bash(zcat *)",
    ],
    # The data itself, via Claude's own file tools. A Read deny also covers Edit
    # on the same path, so these files can't be rewritten either.
    "no_read_data": [
        "Read(**/*.csv)", "Read(**/*.tsv)", "Read(**/*.parquet)",
        "Read(**/*.xlsx)", "Read(**/*.xls)", "Read(**/*.json)",
        "Read(**/*.pkl)", "Read(**/*.pickle)", "Read(**/*.db)",
        "Read(**/*.sqlite)", "Read(**/*.sqlite3)", "Read(**/*.feather)",
        "Read(**/*.h5)", "Read(**/*.hdf5)", "Read(**/*.dta)", "Read(**/*.sav)",
    ],
    # Running things. The Bash rules cover the interpreters; `gusnb run` and
    # `run-all` are named because they're this app's own way to execute a cell.
    #
    # `gusnb add` is not blocked and can't usefully be: it runs by default and a
    # deny rule can't carry an exception for `--no-run`. app.py refuses the run
    # API instead, which is the only place that can, since it owns the kernel.
    "no_execute": [
        "Bash(python *)", "Bash(python3 *)", "Bash(ipython *)",
        "Bash(uv *)", "Bash(pip *)", "Bash(pip3 *)", "Bash(pytest *)",
        "Bash(node *)", "Bash(npm *)", "Bash(npx *)", "Bash(make *)",
        "Bash(sh *)", "Bash(bash *)", "Bash(zsh *)", "Bash(eval *)",
        "Bash(gusnb run *)", "Bash(gusnb run-all*)",
    ],
    # Bare tool names, which per Claude Code's docs remove the tool from its
    # context entirely rather than blocking calls it attempts — so Claude never
    # offers to fetch anything in the first place.
    "no_network": [
        "WebFetch", "WebSearch",
        "Bash(curl *)", "Bash(wget *)", "Bash(nc *)", "Bash(ssh *)",
        "Bash(scp *)", "Bash(rsync *)",
    ],
}

# Shown to the user, and appended to the system prompt so Claude knows the wall
# is there rather than finding it by walking into it.
RESTRICTION_LABELS = {
    "no_bash_read": "Reading file contents through the shell — cat, head, grep "
                    "and friends. ls, find, wc and stat still work, so you can "
                    "still see how a tree is laid out.",
    "no_read_data": "Opening data files (.csv, .parquet, .xlsx, .json, …) with "
                    "the Read tool.",
    "no_execute": "Running code — python, uv, pytest, npm, make — and running "
                  "a notebook cell. Write the cell and leave it for the user "
                  "to run.",
    "no_network": "Reaching the network: WebFetch, WebSearch, curl, wget.",
}

def _venv_env(python=None):
    """VIRTUAL_ENV and PATH for a venv, derived from `python` (or sys.executable).

    `python` should be the notebook's selected interpreter — e.g. .venv/bin/python.
    Falls back to sys.executable so a terminal opened without a notebook context
    still activates the app's own venv.
    """
    bin_dir = pathlib.Path(python or sys.executable).parent  # no resolve() — symlink points outside venv
    venv_dir = bin_dir.parent
    if not (venv_dir / "pyvenv.cfg").exists():
        return {}, None
    path = os.environ.get("PATH", "")
    if str(bin_dir) not in path.split(os.pathsep):
        path = str(bin_dir) + os.pathsep + path
    activate = venv_dir / "bin" / "activate"
    return {"VIRTUAL_ENV": str(venv_dir), "PATH": path}, str(activate) if activate.exists() else None


# The URL a terminal's `gusnb` should talk to. Set by the entry point, because
# only it knows the port; the default matches cli.py's own so a directly-run
# app.py still works.
APP_URL = "http://127.0.0.1:8888"


def set_url(url):
    global APP_URL
    APP_URL = url


def nb_command():
    """How to invoke the `gusnb` CLI from a subprocess, as a shell-safe string.

    An absolute path, resolved here rather than left to PATH. Installed with
    `uv tool install` or into a venv that isn't activated, `gusnb` is on nobody's
    PATH but the app's own interpreter always knows where its siblings are — and
    a hook that silently can't find its own CLI is the hardest kind of failure to
    diagnose, because the symptom is Claude behaving as if the feature doesn't
    exist.

    Falls back to `python -m gusnotebook.cli`, which works from a source
    checkout with no install at all.
    """
    script = pathlib.Path(sys.executable).parent / "gusnb"
    if script.is_file() and os.access(script, os.X_OK):
        return shlex.quote(str(script))
    found = shutil.which("gusnb")
    if found:
        return shlex.quote(found)
    return f"{shlex.quote(sys.executable)} -m gusnotebook.cli"


def merge_restrictions(*sets):
    """Union of several restriction dicts — app-wide plus a session's own.

    A union, because a restriction only ever adds: a session must not be able to
    lift a rule the app set for every project. That also matches how Claude Code
    resolves rules, where a deny from any scope wins over an allow from another.
    """
    out = {}
    extra = []
    for s in sets:
        for key in RESTRICTION_RULES:
            if (s or {}).get(key):
                out[key] = True
        text = ((s or {}).get("deny_extra") or "").strip()
        if text:
            extra.append(text)
    if extra:
        out["deny_extra"] = "\n".join(extra)
    return out


def deny_rules(restrictions):
    """The `permissions.deny` list a restriction set asks for, de-duplicated.

    Order is preserved so the settings file reads in the same order as the
    checkboxes that produced it, which makes a diff of two sessions legible.
    """
    rules = []
    for key, group in RESTRICTION_RULES.items():
        if (restrictions or {}).get(key):
            rules += group
    for line in ((restrictions or {}).get("deny_extra") or "").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rules.append(line)
    return list(dict.fromkeys(rules))


def restriction_note(restrictions):
    """What's blocked, in prose, for the system prompt. None when nothing is.

    The rules do the enforcing; this only saves Claude from discovering them by
    hitting them, which costs a turn and reads to the user as the app being
    broken. Free-text rules are summarised rather than listed: they're Claude
    Code's own syntax, which is precise but says nothing about intent.
    """
    named = [RESTRICTION_LABELS[k] for k in RESTRICTION_RULES
             if (restrictions or {}).get(k) and k in RESTRICTION_LABELS]
    extra = deny_rules({"deny_extra": (restrictions or {}).get("deny_extra")})
    if not named and not extra:
        return None

    lines = ["The user has restricted this workspace. The following are blocked, "
             "enforced by Claude Code itself rather than left to your "
             "discretion, so attempting one simply fails:"]
    lines += [f"- {label}" for label in named]
    if extra:
        lines.append(f"- {len(extra)} further rule(s) the user added by hand.")
    lines.append("Say so plainly if one of these stops you from doing what was "
                 "asked, and suggest what the user could do instead — don't "
                 "look for a way around it.")
    return "\n".join(lines)


def command_for(kind, instructions=None, restrictions=None):
    """argv for a session kind: shell, Claude, or Codex.

    Both agents get the workspace's standing instructions and current-target
    context. Claude also gets the skills plugin and its native deny rules.
    Assembled here so every launch path behaves the same way.
    """
    if kind == "shell":
        return list(SHELL_COMMAND)
    if kind == "codex":
        if not shutil.which(CODEX_COMMAND[0]):
            raise ValueError("Codex CLI is not installed or is not on PATH")
        return list(CODEX_COMMAND) + codex_args(instructions)
    return list(CLAUDE_COMMAND) + claude_args(instructions, restrictions)


def standing_instructions(extra=None, restrictions=None):
    """The app-wide and per-session agent instructions as one prompt.

    The stored setting retains its original `claude_instructions` name for
    compatibility, but the text is provider-neutral and is passed to Codex as
    developer instructions too. Claude-only deny rules are described when
    `restrictions` is supplied; Codex does not receive that native rule syntax.
    """
    from . import llm                       # deferred: llm doesn't import terminals
    parts = []
    base = (llm.load_settings().get("claude_instructions") or "").strip()
    if base:
        parts.append(base)
    extra = (extra or "").strip()
    if extra:
        parts.append("Instructions for this session specifically:\n" + extra)

    note = restriction_note(restrictions)
    if not parts and not note:
        return None

    body = ""
    if parts:
        body = ("The user set these standing instructions for this workspace. "
                "Follow them as you would the project's own conventions.\n\n"
                + "\n\n".join(parts) + "\n")
    if note:
        body += ("\n" if body else "") + note + "\n"
    return body


def system_prompt_file(extra=None, restrictions=None):
    """Write the user's standing instructions for Claude, and return the path.

    Injected with `--append-system-prompt-file` rather than by writing a
    CLAUDE.md into the project: the session root is the user's own repository,
    and a file the app dropped there could be committed by accident, collide
    with a CLAUDE.md that already exists, or confuse a colleague who clones it.
    An app-owned temp file has none of those failure modes, and is thrown away
    with the process.

    Two layers, global first: Settings holds what's always true, and `extra` is
    the current session's own note — sessions exist to separate projects, so
    per-project guardrails belong there. Returns None when nothing is set, so
    the flag is omitted rather than pointing at an empty file.

    `restrictions` are enforced by the deny rules in the settings file, not by
    this text; it's here so Claude knows what it can't do before it tries, and
    it's appended last so the user's own words come first.
    """
    body = standing_instructions(extra, restrictions)
    if not body:
        return None
    fd, path = tempfile.mkstemp(prefix="nb-claude-", suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    return path


# The hook that puts the user's current cell or visually selected HTML/SVG range
# in front of an agent before it reads the prompt. `UserPromptSubmit` output is
# added to the conversation as context, so "rewrite this paragraph" arrives
# already knowing the exact protected range, with no /command to remember.
#
# A shell script rather than a Python one so it costs no interpreter startup on
# every prompt, and it stays silent on failure: if the app is down or nothing is
# focused, a prompt must still go through unchanged.
FOCUS_HOOK = """#!/bin/sh
# Written by GusNotebook. Injects the user's active notebook cell or visual
# HTML/SVG selection as context on every prompt, and tells the app what was asked
# so a cell an agent rewrites can show it. Claude's copy is deleted when its
# terminal closes; Codex uses the stable app-state copy described below.
NB_URL={url}
export NB_URL
# The hook payload, which carries the prompt. Handed to the app as-is rather than
# picked apart here: the field is `prompt`, and the server can read one key out of
# JSON far more reliably than sh can. Backgrounded, capped at two seconds and with
# everything discarded, because this is a courtesy — a prompt must go through at
# full speed whether or not the app is listening.
payload=$(cat)
if command -v curl >/dev/null 2>&1; then
  printf '%s' "$payload" | curl -sS -m 2 -X POST \
    -H 'Content-Type: application/json' --data-binary @- \
    "$NB_URL/api/prompt" >/dev/null 2>&1 &
fi
cell=$({nb} here 2>/dev/null) || exit 0
[ -n "$cell" ] || exit 0
printf '%s\\n%s\\n' \
  'The current user target follows: either a notebook cell or an exact visual \
selection in an HTML/SVG document. For a visual selection, use the document path \
and surrounding context below, then edit that file directly with normal file \
tools. Change only the marked region, preserve the rest of the file, and save it \
on disk; GusNotebook watches the file and reloads the visual editor. Do not use \
`{nb} here -` for a visual document. For a notebook cell, `{nb} here - --run` replaces \
and runs it, with per-cell undo. If an empty cell is asking for code, write it \
into the cell instead of only showing it in the terminal. `{nb} --help` lists \
the rest.' \
  "$cell"
"""


def focus_hook_file(stable=False):
    """Write the provider-neutral current-target hook and return its path.

    Claude settings are temporary, so its hook is too. Codex hook trust is tied
    to the command it is asked to run; a stable app-state path lets the user
    review it once instead of being prompted for every randomly named temp file.
    The stable file is still outside the project and is replaced on app updates.
    """
    body = FOCUS_HOOK.format(nb=nb_command(), url=shlex.quote(APP_URL))
    if stable:
        from . import paths
        script = paths.state("codex-focus-hook.sh")
        tmp = None
        try:
            if script.is_file() and script.read_text(encoding="utf-8") == body:
                return str(script)
            fd, tmp = tempfile.mkstemp(dir=str(script.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
            os.chmod(tmp, 0o700)
            os.replace(tmp, script)
            return str(script)
        except OSError:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return None

    try:
        fd, script = tempfile.mkstemp(prefix="nb-focus-", suffix=".sh")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(script, 0o700)
        return script
    except OSError:
        return None


def session_settings_file(restrictions=None):
    """Write the target-injection hook and the settings file that configures it.

    One file carries both things a Claude session needs from a settings JSON:
    the `UserPromptSubmit` hook, and the user's `permissions.deny` rules. They
    share a file because Claude Code takes one `--settings`, and because both
    are app-owned and both are deleted together when the session closes.

    Returns (settings_path, script_path), or (None, None) if it can't be
    written — a session that opens without the hook is strictly better than one
    that fails to open. Restrictions are the exception to that leniency in
    spirit only: they live in the same file, so a write that fails takes the
    hook with it and the session opens unrestricted. Callers that care should
    check; nothing here can fix a filesystem that won't take a temp file.

    The file is temporary, like the prompt file, and for the same reason: the
    user's own ~/.claude/settings.json is theirs, and an app that edits it
    leaves hooks and permission rules behind after the app is gone.
    """
    script = focus_hook_file()
    if not script:
        return None, None
    try:

        spec = {"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": script}]}]}}
        # Omitted entirely when nothing is restricted, so an unrestricted
        # session's argv is byte-for-byte what it was before this feature.
        rules = deny_rules(restrictions)
        if rules:
            spec["permissions"] = {"deny": rules}

        fd, settings = tempfile.mkstemp(prefix="nb-settings-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(spec, f)
    except OSError:
        try:
            os.unlink(script)
        except OSError:
            pass
        return None, None
    return settings, script


def claude_args(instructions=None, restrictions=None):
    """The extra argv Claude gets: instructions, skills, the hook, the rules.

    Built here rather than at the call site so every way of opening a Claude
    session — the right pane's button, the + tab, a reattach after a restart —
    gets the same treatment without remembering to ask for it.
    """
    from . import skills                    # deferred: skills is optional at import
    args = []
    path = system_prompt_file(instructions, restrictions)
    if path:
        args += ["--append-system-prompt-file", path]
    try:
        args += skills.plugin_args()
    except OSError:
        pass                         # no skills is not a reason to fail to open
    settings, _ = session_settings_file(restrictions)
    if settings:
        args += ["--settings", settings]
    return args


def codex_args(instructions=None):
    """The extra argv Codex gets: instructions and the current-target hook.

    Codex accepts per-launch developer instructions through `-c`, so there is
    no need to create or edit AGENTS.md in the user's repository. Its
    UserPromptSubmit hook has the same stdin/stdout contract this integration
    already uses for Claude: the prompt is posted to the app and `gusnb here`
    becomes developer context before the model sees the turn.

    The hook uses a stable app-state path so Codex can ask the user to trust it
    once. We deliberately do not bypass hook trust: that flag would also trust
    unrelated project hooks. Codex's command approvals and sandbox remain the
    user's own defaults.
    """
    args = []
    body = standing_instructions(instructions)
    if body:
        # JSON strings are also valid TOML basic strings, which keeps newlines,
        # quotes and non-ASCII text in one argv item without shell evaluation.
        args += ["-c", "developer_instructions=" + json.dumps(body)]

    script = focus_hook_file(stable=True)
    if script:
        command = json.dumps(shlex.quote(script))
        hook = ("[{hooks=[{type=\"command\",command=" + command
                + ",timeout=5,additionalContextLimit=12000}]}]")
        args += ["--enable", "hooks", "-c", "hooks.UserPromptSubmit=" + hook]
    return args


def bedrock_env():
    """Env that points `claude` at the gateway's Bedrock endpoint.

    Uses the one gateway credential the whole app shares — set in the settings
    modal, or falling back to the environment / .env — so a session is
    configured from the project's own config rather than from whatever the app
    happened to inherit from the shell that launched it.

    An AWS_BEARER_TOKEN_BEDROCK already in the environment wins, so an outer
    setup (or a user with their own Claude auth) is left alone.
    """
    if os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        return {}
    from . import llm                       # deferred: llm doesn't import terminals
    url, key = llm.gateway_config()
    if not key or not url:
        return {}
    return {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
        "ANTHROPIC_BEDROCK_BASE_URL": f"{url.rstrip('/')}/bedrock",
        "AWS_BEARER_TOKEN_BEDROCK": key,
    }


class Session:
    """One PTY and the clients watching it."""

    def __init__(self, sid, cwd, command=None, label=None, python=None):
        self.id = sid
        self.cwd = str(cwd)
        self.command = list(command or DEFAULT_COMMAND)
        executable = os.path.basename(self.command[0])
        if self.command[0] == SHELL_COMMAND[0]:
            self.kind = "shell"
        elif executable == os.path.basename(CODEX_COMMAND[0]):
            self.kind = "codex"
        else:
            self.kind = "claude"
        self.label = label or os.path.basename(self.cwd.rstrip("/")) or "/"
        self.python = python  # the notebook's selected interpreter, for venv activation
        self.pid = None
        self.fd = None
        self.alive = False
        self.exit_note = None
        self.rows, self.cols = 24, 80

        self._buffer = deque()
        self._buffered = 0
        self._clients = []              # queues, one per attached WebSocket
        self._lock = threading.RLock()

    # --- lifecycle ---

    def start(self):
        # Resolved before the fork: reading settings involves a lock and file
        # I/O, and after pty.fork() the child is single-threaded — a lock held
        # by another thread at fork time would never be released.
        #
        # NB_URL goes to every kind: it's the port this app is listening on, not
        # a secret, and `gusnb` is as useful from a shell as from either agent.
        # The gateway token is the opposite — Claude only, because putting a
        # credential in an interactive shell's environment leaks it into every
        # child process and into `env` output.
        extra_env = {"NB_URL": APP_URL}
        is_shell = self.kind == "shell"
        if self.kind == "claude":
            extra_env.update(bedrock_env())
        venv_env, activate_script = _venv_env(self.python)
        extra_env.update(venv_env)

        command = self.command
        if is_shell and activate_script:
            shell = self.command[0]
            command = [shell, "-l", "-c",
                       f'source "{activate_script}" && exec "{shell}" -l -i']

        pid, fd = pty.fork()
        if pid == 0:
            # Child: become the shell-less command in its own directory.
            try:
                os.chdir(self.cwd)
                os.environ["TERM"] = "xterm-256color"
                os.environ.update(extra_env)
                os.execvp(command[0], command)
            except Exception as e:                     # pragma: no cover - child
                os.write(2, f"cannot start {self.command[0]}: {e}\r\n".encode())
            os._exit(1)

        self.pid = pid
        self.fd = fd
        self.alive = True
        threading.Thread(target=self._read_loop, daemon=True).start()
        bus.publish("terminal_opened", terminal=self.id, cwd=self.cwd,
                    label=self.label)
        return self

    def _read_loop(self):
        """Drain the PTY forever: into the buffer, and out to every client."""
        while self.alive:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.2)
            except (OSError, ValueError):
                break
            if not r:
                continue
            try:
                data = os.read(self.fd, 8192)
            except OSError as e:
                if e.errno == errno.EINTR:
                    continue
                data = b""            # EIO — the child exited and closed the pty
            if not data:
                break
            self._store(data)
            self._fan_out(data)

        self._reap()

    def _reap(self):
        self.alive = False
        try:
            _, status = os.waitpid(self.pid, os.WNOHANG)
            code = os.waitstatus_to_exitcode(status) if status else 0
        except (ChildProcessError, OSError, ValueError):
            code = None
        self.exit_note = f"[{self.command[0]} exited{'' if code is None else f' ({code})'}]"
        note = f"\r\n\x1b[31m{self.exit_note}\x1b[0m\r\n".encode()
        self._store(note)
        self._fan_out(note)
        self._fan_out(None)           # wake senders so they can finish
        bus.publish("terminal_closed", terminal=self.id, note=self.exit_note)

    def _drop_temp_files(self):
        """Remove the temp files this session's argv points at.

        The instructions file and the settings file that carries the cell hook —
        one of each per Claude session, read only at startup, so leaving them
        behind would accumulate a pair per terminal opened for as long as the app
        runs. Deleting on close rather than right after exec avoids racing the
        child, which may not have read them yet.

        The settings file names the hook script, so that goes too: a stray
        executable in /tmp is worse litter than a stray JSON file.
        """
        for flag in ("--append-system-prompt-file", "--settings"):
            try:
                i = self.command.index(flag)
            except ValueError:
                continue
            if i + 1 >= len(self.command):
                continue
            path = self.command[i + 1]
            if flag == "--settings":
                try:
                    with open(path, encoding="utf-8") as f:
                        spec = json.load(f)
                    for group in spec.get("hooks", {}).get("UserPromptSubmit", []):
                        for h in group.get("hooks", []):
                            if h.get("command"):
                                os.unlink(h["command"])
                except (OSError, ValueError, KeyError):
                    pass
            try:
                os.unlink(path)
            except OSError:
                pass

    def close(self):
        """Terminate the session for good (the user closed the tab)."""
        self.alive = False
        self._drop_temp_files()
        if self.pid:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.kill(self.pid, sig)
                    break
                except OSError:
                    pass
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        self._fan_out(None)

    # --- buffer and clients ---

    def _store(self, data):
        with self._lock:
            self._buffer.append(data)
            self._buffered += len(data)
            while self._buffered > BUFFER_BYTES and len(self._buffer) > 1:
                self._buffered -= len(self._buffer.popleft())

    def scrollback(self):
        with self._lock:
            return b"".join(self._buffer)

    def _fan_out(self, data):
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(data)
            except Exception:
                pass

    def attach(self, q):
        with self._lock:
            self._clients.append(q)

    def detach(self, q):
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    # --- input ---

    def write(self, data):
        if self.fd is None or not self.alive:
            return
        if isinstance(data, str):
            data = data.encode()
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def resize(self, rows, cols):
        self.rows, self.cols = int(rows), int(cols)
        if self.fd is None:
            return
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                        struct.pack("HHHH", self.rows, self.cols, 0, 0))
            os.kill(self.pid, signal.SIGWINCH)
        except OSError:
            pass

    def to_json(self):
        return {
            "id": self.id,
            "cwd": self.cwd,
            "label": self.label,
            "command": self.command[0],
            # The OS pid, so a session that has stopped responding can be
            # inspected from a shell rather than guessed at.
            "pid": self.pid,
            # "shell", "claude", or "codex": the browser icons the tab by this, so a
            # session reattached after a reload still shows what it is.
            "kind": self.kind,
            "alive": self.alive,
            "note": self.exit_note,
        }


class SessionPool:
    def __init__(self):
        self._sessions = {}
        self._next = 1
        self._lock = threading.RLock()

    def create(self, cwd, command=None, label=None, python=None):
        cwd = str(cwd)
        if not os.path.isdir(cwd):
            raise ValueError(f"no such directory: {cwd}")
        with self._lock:
            sid = f"t{self._next}"
            self._next += 1
            s = Session(sid, cwd, command=command, label=label, python=python)
            self._sessions[sid] = s
        return s.start()

    def get(self, sid):
        return self._sessions.get(str(sid))

    def list(self):
        with self._lock:
            return [s.to_json() for s in self._sessions.values()]

    def close(self, sid):
        with self._lock:
            s = self._sessions.pop(str(sid), None)
        if s:
            s.close()
        return s is not None

    def close_all(self):
        with self._lock:
            sessions, self._sessions = list(self._sessions.values()), {}
        for s in sessions:
            s.close()
