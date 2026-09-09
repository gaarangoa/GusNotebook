"""Offline Dev Tunnels CLI fixture. The client forwards real TCP bytes."""

import json
import os
from pathlib import Path
import select
import signal
import socket
import socketserver
import sys
import threading


root = Path(os.environ["GUSNOTEBOOK_FAKE_TUNNEL"])
state_file = root / "tunnel.json"
args = [arg for arg in sys.argv[1:] if arg not in {"--json", "--nologo"}]
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\n")


def state():
    return json.loads(state_file.read_text()) if state_file.exists() else {}


def save(value):
    tmp = state_file.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value))
    tmp.replace(state_file)


def output(value):
    print(json.dumps(value), flush=True)


if args[:2] == ["user", "show"]:
    output({"status": "Logged in", "provider": "GitHub", "username": "test-user"})
elif args[:2] == ["user", "login"]:
    output({"status": "Logged in"})
elif args[0] == "show":
    if not state():
        sys.exit(2)
    output({"tunnel": state()})
elif args[0] == "create":
    if state():
        sys.exit(1)
    value = {"tunnelId": args[1], "labels": ["gusnotebook"], "hostConnections": 0, "ports": []}
    save(value)
    output({"tunnel": value})
elif args[:2] == ["access", "list"]:
    output({"accessControlEntries": []})
elif args[:2] == ["port", "list"]:
    output({"ports": state()["ports"]})
elif args[:2] == ["port", "create"]:
    value = state()
    value["ports"] = [{"portNumber": int(args[args.index("--port-number") + 1]), "protocol": "http"}]
    save(value)
    output({"port": value["ports"][0]})
elif args[:2] == ["port", "delete"]:
    value = state()
    value["ports"] = []
    save(value)
    output({"status": "Deleted"})
elif args[0] == "list":
    output({"tunnels": [state()] if state() else []})
elif args[0] == "host":
    value = state()
    value["hostConnections"] = 1
    save(value)
    def stop(*_):
        value = state()
        value["hostConnections"] = 0
        save(value)
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    print("Ready to accept connections for tunnel: " + args[1], flush=True)
    threading.Event().wait()
elif args[0] == "connect":
    port = state()["ports"][0]["portNumber"]
    class Forward(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                with socket.create_connection(("127.0.0.1", port)) as remote:
                    pair = [self.request, remote]
                    while True:
                        readable, _, _ = select.select(pair, [], [], 1)
                        for source in readable:
                            data = source.recv(65536)
                            if not data:
                                return
                            (remote if source is self.request else self.request).sendall(data)
            except OSError:
                pass
    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
    with Server(("127.0.0.1", 0), Forward) as server:
        print(f"SSH: Forwarding from 127.0.0.1:{server.server_address[1]} to host port {port}.", flush=True)
        server.serve_forever()
else:
    print("Unexpected fake devtunnel command", file=sys.stderr)
    sys.exit(1)
