"""Session credential requests through an already authenticated private tunnel.

The local client polls the remote app. No inbound port on the laptop, no token
in browser storage, and no GitHub password is retained between requests.
"""

import hmac
import http.client
import json
import secrets
import threading
import time

from .git_process import GitError, redact


def github_query(value):
    if not isinstance(value, dict) or value.get("protocol") != "https" or not isinstance(value.get("host"), str) or value["host"].lower() not in {"github.com", "github.com:443"}:
        raise GitError("Credential sharing supports HTTPS on github.com only")
    query = {"protocol": "https", "host": "github.com"}
    for key in ("path", "username"):
        text = value.get(key, "")
        if not isinstance(text, str) or len(text) > 2048 or any(ord(c) < 32 for c in text):
            raise GitError("Invalid credential request")
        if text:
            query[key] = text
    return query


def credentials(value):
    if not isinstance(value, dict):
        return None
    result = {key: value.get(key) for key in ("username", "password")}
    if any(not isinstance(v, str) or not v or len(v) > 16000 or any(ord(c) < 32 for c in v) for v in result.values()):
        return None
    return result


class RemoteCredentials:
    def __init__(self):
        self.condition = threading.Condition()
        self.secret = None
        self.seen = 0
        self.account = None
        self.pending = {}

    def active(self):
        with self.condition:
            return self.secret is not None and time.monotonic() - self.seen < 30

    def attach(self, secret):
        if not isinstance(secret, str) or len(secret) != 64 or not all(c in "0123456789abcdef" for c in secret):
            raise GitError("Invalid sharing session")
        with self.condition:
            if self.active() and not hmac.compare_digest(self.secret, secret):
                raise GitError("Another local connection is already providing Git credentials")
            if self.secret != secret:
                self.close()
            self.secret, self.seen = secret, time.monotonic()

    def check(self, secret):
        if not self.active() or not isinstance(secret, str) or not hmac.compare_digest(secret, self.secret):
            raise GitError("Git credential sharing session expired")

    def poll(self, secret, replies, account):
        with self.condition:
            self.check(secret)
            self.seen = time.monotonic()
            self.account = account[:100] if isinstance(account, str) else None
            if not isinstance(replies, dict) or len(replies) > 32:
                raise GitError("Invalid credential replies")
            for key, value in replies.items():
                if key in self.pending:
                    self.pending[key].update(done=True, value=credentials(value))
            self.condition.notify_all()
            if not any(not job["done"] for job in self.pending.values()):
                self.condition.wait(2)
            self.check(secret)
            return {key: job["query"] for key, job in self.pending.items() if not job["done"]}

    def get(self, query):
        with self.condition:
            if not self.active():
                return None
            if len(self.pending) >= 32:
                return None
            key = secrets.token_hex(12)
            job = {"query": github_query(query), "done": False, "value": None}
            self.pending[key] = job
            self.condition.notify_all()
            try:
                self.condition.wait_for(lambda: job["done"] or not self.active(), timeout=30)
                return job["value"] if self.active() else None
            finally:
                self.pending.pop(key, None)

    def close(self, secret=None):
        with self.condition:
            if secret is not None:
                self.check(secret)
            self.secret, self.account = None, None
            for job in self.pending.values():
                job.update(done=True, value=None)
            self.condition.notify_all()


class CredentialForward:
    def __init__(self, port, auth):
        self.port, self.auth = port, auth
        self.secret = secrets.token_hex(32)
        self.stop = threading.Event()
        self.close_lock = threading.Lock()
        self.closed = False
        self.error = None
        self.connected = False
        self.thread = threading.Thread(target=self._run, daemon=True, name="gusnb-git-forward")
        self.thread.start()

    def request(self, action, body):
        # Never follow redirects or consult HTTP proxy environment variables.
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("POST", "/api/git/bridge/" + action, json.dumps(body),
                {"Host": f"gusnotebook.localhost:{self.port}", "Content-Type": "application/json",
                 "X-GusNotebook-Git-Session": self.secret})
            response = connection.getresponse()
            payload = response.read(1024 * 1024)
            if response.status in {404, 405}:
                raise GitError("This remote workspace does not support Git credential sharing. "
                               "Update GusNotebook on the remote computer, then retry Git sharing.")
            try:
                data = json.loads(payload)
            except ValueError as exc:
                raise GitError(f"The remote workspace returned an unexpected Git sharing response (HTTP {response.status}). "
                               "Retry Git sharing; if this continues, update the remote GusNotebook installation.") from exc
            if not isinstance(data, dict):
                raise GitError("Invalid Git sharing response from remote workspace")
            if response.status != 200:
                raise GitError(data.get("error", "Update the remote GusNotebook to enable Git credential sharing"))
            return data
        finally:
            connection.close()

    def _run(self):
        try:
            self.request("attach", {})
            self.connected = True
            replies = {}
            while not self.stop.is_set():
                jobs = self.request("poll", {"replies": replies, "account": self.auth.account.get("login")})
                replies = {}
                if not isinstance(jobs, dict) or len(jobs) > 32:
                    raise GitError("Invalid credential requests from remote workspace")
                # Return each result before resolving the next request. This
                # keeps the lease alive even when a native helper is slow.
                if jobs and not self.stop.is_set():
                    key, query = next(iter(jobs.items()))
                    replies[key] = self.auth.resolve(query, remote=False)
        except GitError as exc:
            if not self.stop.is_set():
                self.error = redact(exc)
        except (OSError, ValueError, http.client.HTTPException):
            if not self.stop.is_set():
                self.error = "Git credential sharing lost its connection. Use Retry Git sharing in tunnel details to reconnect."
        finally:
            self.connected = False
            try:
                self.request("detach", {})
            except (OSError, ValueError, GitError, http.client.HTTPException):
                pass

    def close(self):
        with self.close_lock:
            if self.closed:
                return
            self.closed = True
            self.stop.set()
            try:
                self.request("detach", {})
            except (OSError, ValueError, GitError, http.client.HTTPException):
                pass
            self.thread.join(timeout=6)
