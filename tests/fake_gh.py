"""Offline GitHub account fixture; never accesses a real credential store."""

import json
import os
from pathlib import Path
import sys
import time

root = Path(os.environ["GUSNOTEBOOK_FAKE_GH"])
account = root / "account.json"
args = sys.argv[1:]
if args == ["--version"]:
    print("gh version 1.2.3 (test fixture)")
elif args[:2] == ["auth", "login"]:
    assert all(flag in args for flag in ["--web", "--skip-ssh-key", "--git-protocol"])
    assert not sys.stdin.isatty()
    print("! First copy your one-time code: TEST-2468", file=sys.stderr, flush=True)
    print("Open https://github.com/login/device in your browser", file=sys.stderr, flush=True)
    while not (root / "login-ready").exists():
        time.sleep(.05)
    account.write_text(json.dumps({"login": "researcher", "token": "gho_TEST_ONLY_NEVER_VALID"}))
elif args[:2] == ["auth", "status"]:
    value = json.loads(account.read_text()) if account.exists() else None
    print(json.dumps({"hosts": {"github.com": [{"login": value["login"], "active": True, "state": "success"}] if value else []}}))
elif args[:2] == ["auth", "token"]:
    if not account.exists():
        sys.exit(1)
    print(json.loads(account.read_text())["token"])
else:
    sys.exit(1)
