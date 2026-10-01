"""Central account workflow: reuse, cancellation, identities and API boundaries."""

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from gusnotebook.app import create_app, close_app


def wait_for(predicate):
    deadline = time.monotonic() + 8
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Account workflow did not finish")
        time.sleep(.02)


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.gh = self.root / "github"
        self.tunnels = self.root / "tunnels"
        self.gh.mkdir()
        self.tunnels.mkdir()
        for name, source in [("gh", "fake_gh.py"), ("devtunnel", "fake_devtunnel.py")]:
            executable = self.root / name
            executable.write_text("#!" + sys.executable + "\n" + Path(__file__).with_name(source).read_text())
            executable.chmod(0o755)
        (self.tunnels / "account.json").write_text('{"status":"Logged out"}')
        (self.tunnels / "device-login").touch()
        self.env = patch.dict(os.environ, {
            "GUSNOTEBOOK_GH": str(self.root / "gh"), "GUSNOTEBOOK_FAKE_GH": str(self.gh),
            "GUSNOTEBOOK_DEVTUNNEL": str(self.root / "devtunnel"), "GUSNOTEBOOK_FAKE_TUNNEL": str(self.tunnels),
            "GUSNOTEBOOK_FAKE_TUNNEL_USERNAME": "researcher", "GH_TOKEN": "", "GITHUB_TOKEN": ""})
        self.env.start()
        self.app = create_app({"WORK_DIR": self.root, "STATE_DIR": self.root / "state",
                               "START_WATCHERS": False, "AUTH_REQUIRED": False})
        self.client = self.app.test_client()
        self.accounts = self.app.extensions["gusnotebook"].accounts

    def tearDown(self):
        close_app(self.app)
        self.env.stop()
        self.temp.cleanup()

    def done(self):
        wait_for(lambda: self.accounts.job["state"] != "running")

    def signed_in(self):
        (self.gh / "account.json").write_text(json.dumps({"login": "researcher", "token": "gho_TEST_ONLY_NEVER_VALID"}))
        (self.tunnels / "account.json").write_text(json.dumps({"status": "Logged in", "provider": "GitHub", "username": "researcher"}))

    def test_one_action_runs_both_authorizations_and_reuses_them(self):
        self.assertEqual(self.client.post('/api/accounts', json={"login": True}).status_code, 202)
        wait_for(lambda: "TEST-2468" in (self.accounts.git.snapshot()["activity"] or {}).get("output", ""))
        (self.gh / "login-ready").touch()
        wait_for(lambda: "TEST-1234" in (self.accounts.tunnels.snapshot()["activity"] or {}).get("output", ""))
        self.assertEqual(self.accounts.job["phase"], "tunnels")
        (self.tunnels / "login-ready").touch()
        self.done()
        self.assertEqual(self.accounts.job["state"], "done", self.accounts.job)
        snapshot = self.client.get('/api/accounts').get_json()
        self.assertTrue(snapshot["git"]["account"]["logged_in"])
        self.assertTrue(snapshot["tunnels"]["account"]["logged_in"])
        self.assertNotIn("gho_TEST_ONLY_NEVER_VALID", json.dumps(snapshot))
        previous = (self.gh / "account.json").stat().st_mtime_ns
        calls = (self.tunnels / "calls.jsonl").read_text()
        self.client.post('/api/accounts', json={"login": True})
        self.done()
        self.assertEqual((self.gh / "account.json").stat().st_mtime_ns, previous)
        self.assertEqual((self.tunnels / "calls.jsonl").read_text().count('"login"'), calls.count('"login"'))

    def test_cancel_first_step_never_starts_tunnel_login(self):
        self.client.post('/api/accounts', json={"login": True})
        wait_for(lambda: "TEST-2468" in (self.accounts.git.snapshot()["activity"] or {}).get("output", ""))
        self.client.delete('/api/accounts')
        self.done()
        self.assertEqual(self.accounts.job["state"], "canceled")
        self.assertIsNone(self.accounts.tunnels.activity)
        self.assertFalse((self.gh / "account.json").exists())

    def test_mismatched_github_accounts_are_not_reported_as_connected(self):
        self.signed_in()
        (self.tunnels / "account.json").write_text('{"status":"Logged in","provider":"GitHub","username":"other-user"}')
        with patch.dict(os.environ, {"GUSNOTEBOOK_FAKE_TUNNEL_USERNAME": "other-user"}):
            (self.tunnels / "login-ready").touch()
            self.client.post('/api/accounts', json={"login": True})
            self.done()
        self.assertEqual(self.accounts.job["state"], "error")
        self.assertIn("different GitHub accounts", self.accounts.job["error"])

    def test_remote_workspace_checks_git_without_local_tunnel_access(self):
        self.signed_in()
        self.app.config["PREVIEW_SINGLE_PORT"] = True
        self.client.post('/api/accounts', json={"login": True})
        self.done()
        self.assertIsNone(self.accounts.tunnels.activity)
        self.assertIsNone(self.client.get('/api/accounts').get_json()["tunnels"])
        self.assertEqual(self.client.post('/api/accounts', json={"login": True, "provider": "microsoft"}).status_code, 400)

    def test_local_workflow_cannot_be_canceled_from_a_remote_request(self):
        self.client.post('/api/accounts', json={"login": True})
        wait_for(lambda: "TEST-2468" in (self.accounts.git.snapshot()["activity"] or {}).get("output", ""))
        self.app.config["PREVIEW_SINGLE_PORT"] = True
        self.assertIsNone(self.client.get('/api/accounts').get_json()["activity"])
        self.assertEqual(self.client.delete('/api/accounts').status_code, 400)
        self.assertEqual(self.accounts.job["state"], "running")
        self.app.config["PREVIEW_SINGLE_PORT"] = False
        self.client.delete('/api/accounts')
        self.done()

    def test_authentication_origin_and_body_validation(self):
        secured = create_app({"WORK_DIR": self.root, "STATE_DIR": self.root / "secured-state",
                              "START_WATCHERS": False, "AUTH_TOKEN": "accounts-test"})
        try:
            self.assertEqual(secured.test_client().get('/api/accounts').status_code, 401)
        finally:
            close_app(secured)
        self.assertEqual(self.client.post('/api/accounts', json=[]).status_code, 400)
        self.assertEqual(self.client.post('/api/accounts', json={"provider": "unknown"}).status_code, 400)
        self.assertEqual(self.client.post('/api/accounts', json={}, headers={"Origin": "https://evil.invalid"}).status_code, 403)


if __name__ == '__main__':
    unittest.main()
