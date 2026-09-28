"""Git credential-helper executable; stdlib only, invoked by Git in any venv.

Only Git receives the returned password. Never print diagnostics or credentials
to the terminal, persist credentials, or handle non-GitHub hosts here.
"""

import json
import os
import socket
import sys


def main():
    if sys.argv[-1:] != ["get"] or os.environ.get("GUSNOTEBOOK_GIT_RESOLVING"):
        return
    values = {}
    for line in sys.stdin.read(16384).splitlines():
        key, sep, value = line.partition("=")
        if sep and key in {"protocol", "host", "path", "username"}:
            values[key] = value
    if values.get("protocol") != "https" or values.get("host", "").lower() not in {"github.com", "github.com:443"}:
        return
    request = {"query": values, "cwd": os.getcwd(), "secret": os.environ.get("GUSNOTEBOOK_GIT_SECRET", "")}
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(40)
            client.connect(os.environ["GUSNOTEBOOK_GIT_SOCKET"])
            client.sendall(json.dumps(request).encode() + b"\n")
            with client.makefile("rb") as stream:
                credentials = json.loads(stream.readline(32768))
        if not isinstance(credentials, dict):
            return
        for key in ("username", "password"):
            value = credentials.get(key)
            if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
                return
        sys.stdout.write(f'username={credentials["username"]}\npassword={credentials["password"]}\n\n')
    except (OSError, ValueError, KeyError):
        return


if __name__ == "__main__":
    main()
