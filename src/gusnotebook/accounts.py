"""One account workflow over the CLIs' separately stored authorizations."""

import threading

from .git_process import GitError, redact
from .tunnels import TunnelError


class Accounts:
    def __init__(self, git, tunnels, installer=None):
        self.git = git
        self.tunnels = tunnels
        self.installer = installer
        self.lock = threading.RLock()
        self.job = None
        self.worker = None
        self.closed = False

    def snapshot(self, local):
        with self.lock:
            activity = None if self.job is None or (self.job["local"] and not local) else {
                key: value for key, value in self.job.items() if key not in {"cancel", "local"}}
            if activity and "tools" in activity:
                activity["tools"] = [dict(tool) for tool in activity["tools"]]
        tunnels = self.tunnels.snapshot() if local else None
        return {"git": self.git.snapshot(), "tunnels": None if tunnels is None else {
            key: tunnels[key] for key in ("account", "activity", "cli_error")}, "activity": activity,
                "setup": self.installer.snapshot() if self.installer and local else None}

    def start(self, local, login=False, provider="github", setup=False):
        if setup and (not local or not self.installer):
            raise GitError("Open GusNotebook locally on this computer to install account tools")
        login = login or setup
        if provider not in {"github", "microsoft"}:
            raise GitError("Choose GitHub or Microsoft")
        if provider == "microsoft" and not local:
            raise GitError("Open GusNotebook locally to sign in for tunnels")
        with self.lock:
            if self.closed:
                raise GitError("GusNotebook is shutting down")
            if self.job and self.job["state"] == "running":
                if login and self.job["kind"] not in {"login", "setup"}:
                    raise GitError("Account check is still running. Try again when it finishes.")
                return
            job = {"state": "running", "kind": "setup" if setup else "login" if login else "refresh",
                   "phase": "install" if setup else "git" if provider == "github" else "tunnels", "error": None,
                   "local": local, "cancel": threading.Event()}
            if setup:
                job["tools"] = [{"name": name, "state": "pending", "received": 0, "total": 0}
                                for name in (["gh", "devtunnel"] if provider == "github" else ["devtunnel"])]
            self.job = job
            self.worker = threading.Thread(target=self._run, args=(job, local, login, provider),
                                           daemon=True, name="gusnb-accounts")
            self.worker.start()

    def _check_cancel(self, job):
        if job["cancel"].is_set():
            raise GitError("Sign-in canceled")

    def _wait(self, job, manager, allow_error=False):
        while True:
            self._check_cancel(job)
            activity = manager.snapshot().get("activity")
            if not activity or activity["state"] != "running":
                if activity and activity["state"] == "error" and not allow_error:
                    raise GitError(activity["error"] or "Could not check account")
                if activity and activity["state"] == "canceled":
                    raise GitError("Sign-in canceled")
                return
            job["cancel"].wait(.1)

    def _perform(self, job, manager, callback, allow_error=False):
        # Starting a child and canceling it must not race across phase changes.
        with self.lock:
            self._check_cancel(job)
            callback()
        self._wait(job, manager, allow_error)

    def _run(self, job, local, login, provider):
        try:
            if job["kind"] == "setup":
                for tool in job["tools"]:
                    def progress(**values):
                        with self.lock:
                            tool.update(values)
                    self.installer.install(tool["name"], job["cancel"], progress)
                with self.lock:
                    self._check_cancel(job)
                    job["phase"] = "git" if provider == "github" else "tunnels"
            if provider == "github":
                if self.git.snapshot()["gh_available"]:
                    self._perform(job, self.git, self.git.refresh, allow_error=True)
                    auth = self.git.snapshot()
                    if login and not auth["shared"] and (
                            not auth["account"].get("logged_in") or auth["activity"]["state"] == "error"):
                        self._perform(job, self.git, lambda: self.git.refresh(login=True))
                        if not self.git.snapshot()["account"].get("logged_in"):
                            raise GitError("GitHub sign-in did not complete. Try again.")
                elif login and not self.git.snapshot()["shared"]:
                    raise GitError("Install GitHub CLI (gh) to sign in: https://cli.github.com")
            self._check_cancel(job)
            if local and not self.tunnels.snapshot()["cli_error"]:
                with self.lock:
                    self._check_cancel(job)
                    job["phase"] = "tunnels"
                self._perform(job, self.tunnels, self.tunnels.refresh, allow_error=login)
                account = self.tunnels.snapshot()["account"] or {}
                github_login = self.git.snapshot()["account"].get("login", "")
                matches = (provider != "github" or not github_login or
                           account.get("username", "").lower() == github_login.lower())
                if login and not (account.get("logged_in") and account.get("provider", "").lower() == provider and matches):
                    self._perform(job, self.tunnels, lambda: self.tunnels.refresh(provider))
                    account = self.tunnels.snapshot()["account"] or {}
                    if provider == "github" and github_login and account.get("username", "").lower() != github_login.lower():
                        raise GitError(f"Git and tunnels use different GitHub accounts. Sign in to tunnels as {github_login}.")
            with self.lock:
                job["state"] = "done"
        except (GitError, TunnelError, OSError) as exc:
            with self.lock:
                job.update(state="error", error=redact(exc))
                if job["phase"] == "install":
                    current = next((tool for tool in job["tools"] if tool["state"] != "available"), None)
                    if current:
                        current["state"] = "failed"
        finally:
            if job["cancel"].is_set():
                with self.lock:
                    job.update(state="canceled", error=None)
                    if job["phase"] == "install":
                        for tool in job["tools"]:
                            if tool["state"] not in {"available", "pending"}:
                                tool["state"] = "canceled"

    def cancel(self, local=True):
        with self.lock:
            if self.job and self.job["state"] == "running":
                if self.job["local"] and not local:
                    raise GitError("Open GusNotebook locally to cancel this account workflow")
                self.job["cancel"].set()
                if self.job["phase"] == "git":
                    self.git.cancel()
                elif self.job["phase"] == "tunnels":
                    self.tunnels.cancel_login()

    def close(self):
        with self.lock:
            self.closed = True
        self.cancel()
        if self.worker:
            self.worker.join(timeout=3)
