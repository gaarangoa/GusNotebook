"""GitHub CLI sign-in and an app-scoped Git credential helper."""

import hmac
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import socketserver
import sys
import tempfile
import threading

from .git_bridge import RemoteCredentials, credentials, github_query
from .git_process import Commands, GitError, redact


def executable(name, variable):
    candidate = os.environ.get(variable) or shutil.which(name)
    if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
        return str(Path(candidate).resolve())
    return None


def base_environment(environment):
    env = dict(environment)
    # A nested GusNotebook must not recurse through its parent's helper.
    if "GUSNOTEBOOK_GIT_BASE_COUNT" in env:
        count = int(env.pop("GUSNOTEBOOK_GIT_BASE_COUNT"))
        for index in range(count, int(env.get("GIT_CONFIG_COUNT", "0"))):
            env.pop(f"GIT_CONFIG_KEY_{index}", None)
            env.pop(f"GIT_CONFIG_VALUE_{index}", None)
        env["GIT_CONFIG_COUNT"] = str(count)
    for key in ("GUSNOTEBOOK_GIT_SOCKET", "GUSNOTEBOOK_GIT_SECRET"):
        env.pop(key, None)
    return env


class GitAuth:
    def __init__(self, work):
        self.work = str(work)
        self.env = base_environment(os.environ)
        self.commands = Commands()
        self.lock = threading.RLock()
        self.account = {}
        self.job = None
        self.worker = None
        self.server = self.server_thread = self.temporary = None
        self.secret = secrets.token_hex(32)
        self.remote = RemoteCredentials()

    def snapshot(self):
        with self.lock:
            gh = executable("gh", "GUSNOTEBOOK_GH")
            job = None if self.job is None else {k: v for k, v in self.job.items() if k != "cancel"}
            return {"account": dict(self.account), "activity": job, "gh_available": bool(gh),
                    "shared": self.remote.active(), "shared_account": self.remote.account if self.remote.active() else None}

    def refresh(self, login=False):
        with self.lock:
            if self.commands.closed.is_set():
                raise GitError("GusNotebook is shutting down")
            if self.job and self.job["state"] == "running":
                return
            gh = executable("gh", "GUSNOTEBOOK_GH")
            if not gh:
                raise GitError("Install GitHub CLI (gh) to sign in: https://cli.github.com")
            job = {"state": "running", "kind": "login" if login else "refresh", "output": "", "error": None,
                   "cancel": threading.Event()}
            self.job = job
            self.worker = threading.Thread(target=self._refresh, args=(gh, job, login), daemon=True, name="gusnb-github-login")
            self.worker.start()

    def _refresh(self, gh, job, login):
        try:
            env = {**self.env, "GH_BROWSER": "/usr/bin/true", "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}
            if login:
                def output(text):
                    with self.lock:
                        job["output"] = redact(job["output"] + text)[-8000:]
                result = self.commands.run([gh, "auth", "login", "--hostname", "github.com", "--web",
                    "--git-protocol", "https", "--skip-ssh-key"], env=env, cwd=self.work,
                    cancel=job["cancel"], timeout=600, output=output)
                if result.returncode:
                    raise GitError("GitHub sign-in did not complete. Check the instructions above and retry.")
            result = self.commands.run([gh, "auth", "status", "--hostname", "github.com", "--active", "--json", "hosts"],
                                       env=env, cwd=self.work, cancel=job["cancel"], timeout=20)
            if result.returncode:
                raise GitError("Cannot check GitHub sign-in. Update GitHub CLI and check your connection.")
            entries = json.loads(result.stdout).get("hosts", {}).get("github.com", [])
            active = next((entry for entry in entries if entry.get("active")), {})
            with self.lock:
                self.account = {"login": active.get("login", ""), "logged_in": active.get("state") == "success"}
                job["state"] = "done"
                if login:
                    job["output"] = ""
        except (GitError, OSError, ValueError, TypeError, AttributeError) as exc:
            with self.lock:
                job.update(state="error", error=redact(exc))
        finally:
            if job["cancel"].is_set():
                with self.lock:
                    job.update(state="canceled", output="", error=None)

    def cancel(self):
        with self.lock:
            if self.job:
                self.job["cancel"].set()

    def resolve(self, query, cwd=None, remote=True):
        query = github_query(query)
        if self.commands.closed.is_set():
            return None
        # Run the user's original Git configuration without our helper. Existing
        # credentials win. Disable interactive prompts in background requests.
        env = {**self.env, "GUSNOTEBOOK_GIT_RESOLVING": "1", "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
        env.setdefault("GIT_ASKPASS", "/usr/bin/false")
        git = executable("git", "GUSNOTEBOOK_GIT")
        if git:
            try:
                result = self.commands.run([git, "credential", "fill"], env=env,
                    cwd=cwd or str(Path.home()), data="".join(f"{k}={v}\n" for k, v in query.items()) + "\n", timeout=5)
                found = credentials(dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line))
                if result.returncode == 0 and found:
                    return found
            except (OSError, GitError):
                pass
        gh = executable("gh", "GUSNOTEBOOK_GH")
        if gh:
            try:
                result = self.commands.run([gh, "auth", "token", "--hostname", "github.com"], env=env, timeout=5)
                found = credentials({"username": "x-access-token", "password": result.stdout.strip()})
                if result.returncode == 0 and found:
                    return found
            except (OSError, GitError):
                pass
        return self.remote.get(query) if remote else None

    def environment(self, environment=None):
        with self.lock:
            if self.commands.closed.is_set():
                raise GitError("GusNotebook is shutting down")
            if self.server is None:
                self._serve()
            env = base_environment(environment if environment is not None else self.env)
            count = int(env.get("GIT_CONFIG_COUNT", "0"))
            env["GUSNOTEBOOK_GIT_BASE_COUNT"] = str(count)
            helper = "!" + shlex.join([sys.executable, str(Path(__file__).with_name("git_credential.py"))])
            # Reset helpers for this host in this process only. Our resolver calls
            # the original helpers first. Git's approval cannot persist a forwarded
            # token in a remote user's existing store/cache helper.
            for value in ("", helper):
                env[f"GIT_CONFIG_KEY_{count}"] = "credential.https://github.com.helper"
                env[f"GIT_CONFIG_VALUE_{count}"] = value
                count += 1
            env.update(GIT_CONFIG_COUNT=str(count), GUSNOTEBOOK_GIT_SOCKET=self.server.server_address,
                       GUSNOTEBOOK_GIT_SECRET=self.secret)
            return env

    def _serve(self):
        auth = self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(40)
                try:
                    value = json.loads(self.rfile.readline(16384))
                    secret = value.get("secret", "")
                    if not isinstance(secret, str) or not hmac.compare_digest(secret, auth.secret):
                        return
                    result = auth.resolve(value.get("query"), cwd=value.get("cwd"))
                    self.wfile.write(json.dumps(result).encode() + b"\n")
                except (ValueError, OSError, GitError, TypeError, AttributeError):
                    return
        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
            def handle_error(self, *_):
                pass  # Never log request bodies containing credentials.
        self.temporary = tempfile.TemporaryDirectory(prefix="gusnb-git-", dir="/tmp")
        self.server = Server(str(Path(self.temporary.name) / "auth.sock"), Handler)
        os.chmod(self.server.server_address, 0o600)
        self.server_thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=.1), daemon=True)
        self.server_thread.start()

    def close(self):
        self.cancel()
        self.remote.close()
        self.commands.close()
        if self.worker:
            self.worker.join(timeout=3)
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(timeout=2)
            self.temporary.cleanup()
