"""Saved local tunnel connections. Cloud credentials remain in devtunnel."""

import codecs
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
import uuid

from .persistence import atomic_write
from .tunnels import DevTunnels, TunnelError, executable, tunnel_name


def _stop(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
    except ProcessLookupError:
        pass


class TunnelManager:
    def __init__(self, registry_path):
        self.path = Path(registry_path)
        self.lock = threading.RLock()
        self.closed = False
        self.saved = {}
        self.discovered = {}
        self.connections = {}
        self.canonical = {}
        self.children = set()
        self.workers = set()
        self.account = None
        self.checked_at = None
        self.activity = None
        self.load_error = None
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, list):
                raise ValueError("expected a saved tunnel list")
            for row in data:
                entry = self._entry(row)
                self.saved[entry["id"]] = entry
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, TunnelError) as exc:
            self.saved = {}
            self.load_error = f"Cannot read saved tunnels: {exc}. Check {self.path}."

    @staticmethod
    def _entry(body):
        if not isinstance(body, dict):
            raise TunnelError("Enter a tunnel name and ID")
        name = body.get("name", body.get("tunnel", ""))
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
            raise TunnelError("Use a name of 1–100 characters")
        identifier = body.get("id")
        if identifier is not None and not isinstance(identifier, str):
            raise TunnelError("Invalid saved tunnel ID")
        return {"id": str(uuid.UUID(identifier)) if identifier else str(uuid.uuid4()),
                "name": name.strip(), "tunnel": tunnel_name(body.get("tunnel")),
                "last_connected": body.get("last_connected")}

    def _save(self):
        if self.load_error:
            raise TunnelError(self.load_error)
        atomic_write(self.path, json.dumps(list(self.saved.values()), indent=2) + "\n")

    def add(self, body):
        entry = self._entry({"name": body.get("name") or body.get("tunnel"), "tunnel": body.get("tunnel")})
        with self.lock:
            for old in self.saved.values():
                if old["tunnel"].lower() == entry["tunnel"].lower():
                    return dict(old)
            if len(self.saved) >= 200:
                raise TunnelError("At most 200 tunnels can be saved")
            self.saved[entry["id"]] = entry
            try:
                self._save()
            except Exception:
                self.saved.pop(entry["id"], None)
                raise
            return dict(entry)

    def rename(self, identifier, name):
        with self.lock:
            old = self._get(identifier)
            entry = self._entry({**old, "name": name})
            self.saved[identifier] = entry
            try:
                self._save()
            except Exception:
                self.saved[identifier] = old
                raise
            return dict(entry)

    def remove(self, identifier):
        with self.lock:
            old = self._get(identifier)
            del self.saved[identifier]
            try:
                self._save()
            except Exception:
                self.saved[identifier] = old
                raise
            self.disconnect(identifier)
            self.connections.pop(identifier, None)

    def _get(self, identifier):
        if identifier not in self.saved:
            raise TunnelError("That saved tunnel no longer exists")
        return self.saved[identifier]

    def _thread(self, callback):
        def run():
            try:
                callback()
            finally:
                with self.lock:
                    self.workers.discard(threading.current_thread())
        if self.closed:
            raise TunnelError("GusNotebook is shutting down")
        thread = threading.Thread(target=run, daemon=True, name="gusnb-tunnel-manager")
        self.workers.add(thread)
        thread.start()

    def _spawn(self, command, cancel):
        with self.lock:
            if self.closed or cancel.is_set():
                raise TunnelError("Canceled")
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            self.children.add(process)
            return process

    def _run(self, command, cancel, timeout=45, **_kwargs):
        process = self._spawn(command, cancel)
        deadline = time.monotonic() + timeout
        try:
            while True:
                if cancel.is_set() or self.closed:
                    raise TunnelError("Canceled")
                if time.monotonic() >= deadline:
                    raise TunnelError("Dev Tunnels did not respond in time. Check your connection and retry.")
                try:
                    output = process.communicate(timeout=.2)[0].decode("utf-8", errors="replace")
                    return subprocess.CompletedProcess(command, process.returncode, stdout=output, stderr=output)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            _stop(process)
            process.stdout.close()
            with self.lock:
                self.children.discard(process)

    def _cli(self, cancel):
        return DevTunnels(runner=lambda command, **kwargs: self._run(command, cancel, **kwargs))

    def _account(self, cli):
        status = cli.json("user", "show")
        account = {"logged_in": str(status.get("status", "")).lower() == "logged in",
                   "provider": str(status.get("provider", "")), "username": str(status.get("username", ""))}
        with self.lock:
            self.account = account
        return account

    def snapshot(self):
        with self.lock:
            try:
                executable()
                cli_error = None
            except TunnelError as exc:
                cli_error = str(exc)
            rows = []
            for entry in self.saved.values():
                detail = self.discovered.get(entry["tunnel"].lower())
                state = "unknown" if detail is None else ("available" if detail.get("hostConnections") else "offline")
                connection = self.connections.get(entry["id"], {})
                if connection.get("state") in {"connecting", "connected", "error"}:
                    state = connection["state"]
                rows.append({**entry, "state": state, "url": connection.get("url"),
                             "error": connection.get("error"), "local_port": connection.get("port")})
            activity = None if self.activity is None else {k: v for k, v in self.activity.items() if k != "cancel"}
            registered = {row["tunnel"].lower() for row in rows}
            discovered = [{"tunnel": item["tunnelId"], "name": item["tunnelId"],
                           "state": "available" if item.get("hostConnections") else "offline"}
                          for key, item in self.discovered.items() if key not in registered]
            return {"saved": rows, "discovered": discovered, "account": self.account,
                    "activity": activity, "checked_at": self.checked_at,
                    "cli_error": cli_error, "error": self.load_error}

    def refresh(self, provider=None):
        if provider is not None and provider not in {"github", "microsoft"}:
            raise TunnelError("Choose GitHub or Microsoft")
        with self.lock:
            if self.activity and self.activity["state"] == "running":
                return
            if provider and any(c["state"] in {"connecting", "connected"} for c in self.connections.values()):
                raise TunnelError("Disconnect your tunnels before starting a new sign-in")
            job = {"kind": "login" if provider else "refresh", "state": "running", "output": "", "error": None,
                   "cancel": threading.Event()}
            self.activity = job
            self._thread(lambda: self._refresh(job, provider))

    def _refresh(self, job, provider):
        try:
            cli = self._cli(job["cancel"])
            if provider:
                self._login(cli, provider, job)
            account = self._account(cli)
            entries = cli.entries() if account["logged_in"] else []
            with self.lock:
                if not job["cancel"].is_set():
                    self.discovered = {row["tunnelId"].lower(): row for row in entries}
                    self.checked_at = time.time()
                    job["state"] = "done"
        except (TunnelError, OSError, ValueError) as exc:
            with self.lock:
                job.update(state="error", error=str(exc))
        finally:
            if job["cancel"].is_set():
                with self.lock:
                    job.update(state="canceled", output="")

    def _login(self, cli, provider, job):
        process = self._spawn([cli.command, "user", "login", "--github" if provider == "github" else "--entra",
                               "--use-device-code-auth"], job["cancel"])
        def read():
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            while True:
                data = os.read(process.stdout.fileno(), 1024)
                if not data:
                    break
                with self.lock:
                    output = job["output"] + decoder.decode(data)
                    job["output"] = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", output)[-8000:]
        reader = threading.Thread(target=read, daemon=True, name="gusnb-tunnel-login")
        reader.start()
        deadline = time.monotonic() + 600
        try:
            while process.poll() is None:
                if job["cancel"].wait(.1) or self.closed:
                    raise TunnelError("Sign-in canceled")
                if time.monotonic() > deadline:
                    raise TunnelError("Sign-in timed out. Try signing in again.")
            if process.returncode or not self._account(cli)["logged_in"]:
                raise TunnelError("Sign-in did not complete. Try again.")
        finally:
            _stop(process)
            reader.join(timeout=2)
            process.stdout.close()
            with self.lock:
                self.children.discard(process)

    def cancel_login(self):
        with self.lock:
            if self.activity and self.activity["state"] == "running":
                self.activity["cancel"].set()

    def connect(self, identifier):
        with self.lock:
            entry = dict(self._get(identifier))
            if self.activity and self.activity["kind"] == "login" and self.activity["state"] == "running":
                raise TunnelError("Finish signing in before connecting")
            previous = self.connections.get(identifier)
            if previous and previous["state"] in {"connecting", "connected"}:
                return
            connection = {"state": "connecting", "cancel": threading.Event(), "url": None, "error": None}
            self.connections[identifier] = connection
            self._thread(lambda: self._connect(entry, connection))

    def _connect(self, entry, connection):
        process = None
        canonical = None
        cancel = connection["cancel"]
        try:
            cli = self._cli(cancel)
            if not self._account(cli)["logged_in"]:
                raise TunnelError("Sign in from the Tunnels sidebar, then try again")
            detail = cli.show(entry["tunnel"])
            cli.owned(detail)
            canonical = tunnel_name(detail["tunnelId"])
            with self.lock:
                if self.closed or cancel.is_set():
                    return
                other = self.canonical.get(canonical)
                if other and other["state"] in {"connecting", "connected"} and not other["cancel"].is_set():
                    old = self.saved[entry["id"]]
                    self.saved[entry["id"]] = {**old, "tunnel": canonical, "last_connected": other.get("connected_at")}
                    try:
                        self._save()
                    except Exception:
                        self.saved[entry["id"]] = old
                        raise
                    self.connections[entry["id"]] = other
                    return
                self.canonical[canonical] = connection
                self.discovered[canonical.lower()] = detail
            if detail.get("hostConnections") == 0:
                raise TunnelError("This tunnel is offline. Start GusNotebook with --tunnel on the remote computer.")
            process = cli.connect(canonical, detail=detail)
            with self.lock:
                connection["process"] = process
            if cancel.is_set() or self.closed:
                return
            process.wait_ready()
            with self.lock:
                if cancel.is_set() or self.closed:
                    return
                connection.update(state="connected", port=process.local_port, connected_at=time.time(),
                                  url=f"http://gusnotebook.localhost:{process.local_port}/")
                for identifier, saved in self.saved.items():
                    if self.connections.get(identifier) is connection:
                        saved.update(tunnel=canonical, last_connected=connection["connected_at"])
                self._save()
            while not process.done.wait(.2):
                if cancel.is_set() or self.closed:
                    return
            raise TunnelError(process.failure or "Disconnected. Connect again to reopen the workspace.")
        except (TunnelError, OSError, ValueError) as exc:
            with self.lock:
                if not cancel.is_set():
                    connection.update(state="error", error=str(exc), url=None)
        finally:
            if process:
                process.close()
            with self.lock:
                if canonical and self.canonical.get(canonical) is connection:
                    self.canonical.pop(canonical, None)
                if cancel.is_set():
                    connection.update(state="disconnected", error=None, url=None)

    def disconnect(self, identifier):
        with self.lock:
            connection = self.connections.get(identifier)
            if not connection:
                return
            connection["cancel"].set()
            connection.update(state="disconnected", error=None, url=None)
            process = connection.get("process")
        # A separate close interrupts wait_ready as well as an active forward.
        if process:
            process.close()

    def close(self):
        with self.lock:
            self.closed = True
            if self.activity:
                self.activity["cancel"].set()
            connections = list(self.connections.values())
            children = list(self.children)
            workers = list(self.workers)
            for connection in connections:
                connection["cancel"].set()
        for process in children:
            _stop(process)
        for connection in connections:
            if connection.get("process"):
                connection["process"].close()
        deadline = time.monotonic() + 6
        for worker in workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))
