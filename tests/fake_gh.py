"""Offline GitHub account fixture; never accesses a real credential store."""

import json
import os
from pathlib import Path
import sys
import time

root = Path(os.environ["GUSNOTEBOOK_FAKE_GH"])
account = root / "account.json"
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\n")
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
elif args[:2] == ["auth", "logout"]:
    assert args[2:] == ["--hostname", "github.com", "--user", json.loads(account.read_text())["login"]]
    if (root / "logout-fail").exists():
        print("Cannot sign out gho_TEST_ONLY_NEVER_VALID", file=sys.stderr)
        sys.exit(1)
    while (root / "hold-logout").exists():
        time.sleep(.05)
    account.unlink()
    other = root / "other-account.json"
    if other.exists():
        account.write_text(other.read_text())
        other.unlink()
elif args[:2] == ["auth", "token"]:
    if not account.exists():
        sys.exit(1)
    print(json.loads(account.read_text())["token"])
else:
    sys.exit(1)
