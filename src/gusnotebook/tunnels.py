"""Account-authenticated remote connections using Microsoft's devtunnel CLI.

The CLI owns OAuth credentials and the encrypted transport. GusNotebook manages
one private, labelled tunnel with one port. Clients forward that port to loopback;
they never start a local notebook, kernel, or agent.
"""

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import webbrowser


INSTALL_URL = "https://learn.microsoft.com/azure/developer/dev-tunnels/get-started#install"
LABEL = "gusnotebook"
_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9.-]{1,98}[a-zA-Z0-9]\Z")
_FORWARD = re.compile(r"Forwarding from 127\.0\.0\.1:(\d+) to host port (\d+)")


class TunnelError(RuntimeError):
    pass


def tunnel_name(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise TunnelError("Use a tunnel name of 3–100 letters, digits, hyphens or dots.")
    return value


def executable():
    override = os.environ.get("GUSNOTEBOOK_DEVTUNNEL")
    candidates = [override] if override else [shutil.which("devtunnel"),
        str(Path.home() / ".devtunnel/bin/devtunnel"),
        str(Path.home() / ".local/bin/devtunnel")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    raise TunnelError("Microsoft's devtunnel CLI is required. Install it on both computers: "
                      + INSTALL_URL + " (or set GUSNOTEBOOK_DEVTUNNEL to its executable).")


class DevTunnels:
    def __init__(self, command=None):
        self.command = command or executable()

    def json(self, *args, missing=False):
        try:
            result = subprocess.run([self.command, *map(str, args), "--json", "--nologo"],
                                    capture_output=True, text=True, timeout=45)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TunnelError(f"Cannot run devtunnel: {exc}") from exc
        if missing and result.returncode == 2:
            return None
        if result.returncode:
            raise TunnelError(f"devtunnel {' '.join(map(str, args[:2]))} failed: "
                              + (result.stderr.strip() or "check your login and network connection"))
        try:
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise ValueError("expected an object")
            return value
        except ValueError as exc:
            raise TunnelError("Unexpected devtunnel response. Update the devtunnel CLI and retry.") from exc

    def login(self, provider=None, device=False):
        status = self.json("user", "show")
        logged_in = str(status.get("status", "")).lower() == "logged in"
        if logged_in and (not provider or str(status.get("provider", "")).lower() == provider):
            return
        provider = provider or "github"
        print(f"Sign in to Dev Tunnels with {provider}. Use the same account on both computers.", flush=True)
        args = [self.command, "user", "login", "--github" if provider == "github" else "--entra"]
        if device:
            args.append("--use-device-code-auth")
        try:
            result = subprocess.run(args)
        except OSError as exc:
            raise TunnelError(f"Cannot start tunnel sign-in: {exc}") from exc
        if result.returncode or str(self.json("user", "show").get("status", "")).lower() != "logged in":
            raise TunnelError("Tunnel sign-in did not complete. Retry with --tunnel-login github or microsoft.")

    def show(self, name, missing=False):
        result = self.json("show", tunnel_name(name), missing=missing)
        if result is None:
            return None
        detail = result.get("tunnel")
        if not isinstance(detail, dict) or not detail.get("tunnelId"):
            raise TunnelError("The devtunnel response did not include tunnel details. Update the CLI.")
        return detail

    def private(self, name, port=None):
        args = ["access", "list", name]
        if port is not None:
            args += ["--port-number", str(port)]
        entries = self.json(*args).get("accessControlEntries")
        if not isinstance(entries, list) or any(not isinstance(e, dict) or e.get("isDeny") is not True for e in entries):
            raise TunnelError("This tunnel has shared or unrecognized access rules. Choose a new private "
                              "GusNotebook tunnel name; access rules were not changed.")

    def ports(self, name):
        result = self.json("port", "list", name)
        # An empty tunnel produces only a warning, not {"ports": []}. The CLI
        # may remove the cluster suffix from the name in that warning.
        if any(result == {"warning": f"No ports found for tunnel {candidate}."}
               for candidate in (name, name.rsplit(".", 1)[0])):
            return []
        ports = result.get("ports")
        if not isinstance(ports, list):
            raise TunnelError("Cannot read the tunnel's ports: unexpected devtunnel port list response. "
                              "Check devtunnel --version and report this error.")
        for port in ports:
            if not isinstance(port, dict) or type(port.get("portNumber")) is not int or not 0 < port["portNumber"] < 65536:
                raise TunnelError("Invalid port in the devtunnel response.")
        return ports

    @staticmethod
    def owned(detail):
        labels = detail.get("labels")
        if not isinstance(labels, list) or LABEL not in labels:
            raise TunnelError("This is not a GusNotebook tunnel. Choose a new name or use --list-tunnels.")

    def prepare(self, name, port):
        name = tunnel_name(name)
        detail = self.show(name, missing=True)
        if detail is None:
            created = self.json("create", name, "--labels", LABEL, "--description", "GusNotebook remote workspace",
                                "--expiration", "30d")
            detail = created.get("tunnel")
            if not isinstance(detail, dict) or not detail.get("tunnelId"):
                raise TunnelError("The tunnel could not be created. Check devtunnel list before retrying.")
        self.owned(detail)
        name = tunnel_name(detail["tunnelId"])
        if detail.get("hostConnections", 0):
            raise TunnelError("That tunnel is already running. Connect to it or choose another name.")
        self.private(name)
        ports = self.ports(name)
        if len(ports) > 1:
            raise TunnelError("This tunnel has extra ports. Choose a new GusNotebook tunnel name.")
        for previous in ports:
            self.private(name, previous["portNumber"])
            if previous["portNumber"] != port:
                self.json("port", "delete", name, "--port-number", previous["portNumber"])
                ports = []
        if not ports:
            self.json("port", "create", name, "--port-number", port, "--protocol", "http",
                      "--host-header", "unchanged", "--origin-header", "unchanged")
        self.private(name, port)
        return name

    def list(self):
        entries = self.json("list", "--labels", LABEL).get("tunnels")
        if not isinstance(entries, list):
            raise TunnelError("Cannot read the tunnel list. Update the devtunnel CLI.")
        if not entries:
            print("No GusNotebook tunnels found for this account.")
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("labels"), list) and LABEL in entry["labels"]:
                print(f"{entry.get('tunnelId', '?')}  {'running' if entry.get('hostConnections') else 'offline'}")

    def host(self, name):
        return TunnelProcess([self.command, "host", name, "--host-header", "unchanged",
                              "--origin-header", "unchanged", "--nologo"], hosting=True)

    def connect(self, name):
        detail = self.show(name)
        self.owned(detail)
        name = tunnel_name(detail["tunnelId"])
        self.private(name)
        ports = self.ports(name)
        if len(ports) != 1:
            raise TunnelError("Expected one GusNotebook port. Start the remote app with --tunnel first.")
        port = ports[0]["portNumber"]
        self.private(name, port)
        return TunnelProcess([self.command, "connect", name, "--nologo"], remote_port=port)


class TunnelProcess:
    """Keep CLI output drained, report readiness, and reap the owned child."""

    def __init__(self, command, hosting=False, remote_port=None):
        self.hosting = hosting
        self.remote_port = remote_port
        self.local_port = None
        self.ready = threading.Event()
        self.done = threading.Event()
        self.stopping = False
        self.failure = None
        try:
            self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                                            start_new_session=True)
        except OSError as exc:
            raise TunnelError(f"Cannot start devtunnel: {exc}") from exc
        self.reader = threading.Thread(target=self._read, name="gusnb-tunnel", daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                line = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", line).strip()
                match = _FORWARD.search(line)
                if self.hosting and "Ready to accept connections for tunnel:" in line:
                    self.ready.set()
                elif match and int(match[2]) == self.remote_port:
                    self.local_port = int(match[1])
                    if 0 < self.local_port < 65536:
                        self.ready.set()
                # The browser relay URL is not the local-client workflow.
                if line and not line.startswith(("Hosting port ", "Connecting to host tunnel relay ")):
                    print("[tunnel] " + line, flush=True)
        finally:
            code = self.process.wait()
            if not self.stopping:
                self.failure = f"Tunnel stopped (exit {code}). Check the connection and restart the command."
            self.done.set()

    def wait_ready(self, timeout=60):
        import time
        deadline = time.monotonic() + timeout
        try:
            while not self.ready.wait(.1):
                if self.done.is_set():
                    raise TunnelError(self.failure or "Tunnel stopped before connecting.")
                if time.monotonic() >= deadline:
                    raise TunnelError("Tunnel connection timed out. Check your login, remote host, and network.")
            if self.done.is_set():
                raise TunnelError(self.failure or "Tunnel disconnected.")
        except BaseException:
            self.close()
            raise

    def close(self):
        self.stopping = True
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
            except ProcessLookupError:
                pass
        self.reader.join(timeout=2)
        self.process.stdout.close()


def add_arguments(parser):
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--tunnel", metavar="NAME", help="host this remote workspace in a private Dev Tunnel")
    modes.add_argument("--connect", metavar="NAME", help="connect to a remote GusNotebook without starting a local server")
    modes.add_argument("--list-tunnels", action="store_true", help="list your GusNotebook tunnels")
    parser.add_argument("--tunnel-login", choices=["github", "microsoft"],
                        help="account provider (existing login, or GitHub on first use)")
    parser.add_argument("--device-code", action="store_true", help="sign in using a code on another computer")


def connect_client(cli, name, no_browser=False):
    tunnel = cli.connect(name)
    try:
        tunnel.wait_ready()
        url = f"http://gusnotebook.localhost:{tunnel.local_port}/"
        print(f"Remote GusNotebook — {url}\nFiles, kernels and terminals run on the remote computer.\n"
              "Keep this command running. Ctrl-C disconnects this computer only.", flush=True)
        if not no_browser:
            webbrowser.open(url)
        tunnel.done.wait()
        raise TunnelError(tunnel.failure or "Tunnel disconnected.")
    except KeyboardInterrupt:
        pass
    finally:
        tunnel.close()
