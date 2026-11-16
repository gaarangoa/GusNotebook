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
from gusnotebook.git_process import GitError


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

    def refresh(self):
        self.assertEqual(self.client.post('/api/accounts', json={}).status_code, 202)
        self.done()

    def logout(self):
        self.assertEqual(self.client.post('/api/accounts/logout').status_code, 202)
        self.done()
        return self.client.get('/api/accounts').get_json()

    def test_logout_clears_both_accounts_and_allows_signing_in_again(self):
        self.signed_in()
        self.refresh()
        state = self.logout()
        self.assertEqual(state['activity']['state'], 'done', state)
        self.assertFalse(state['git']['account']['logged_in'])
        self.assertFalse(state['tunnels']['account']['logged_in'])
        self.assertFalse((self.gh / 'account.json').exists())
        self.assertEqual(json.loads((self.tunnels / 'account.json').read_text())['status'], 'Logged out')
        self.assertIn(['auth', 'logout', '--hostname', 'github.com', '--user', 'researcher'],
                      [json.loads(line) for line in (self.gh / 'calls.jsonl').read_text().splitlines()])
        # Already signed-out accounts can be logged out again without errors.
        self.assertEqual(self.logout()['activity']['state'], 'done')
        (self.gh / 'login-ready').touch()
        (self.tunnels / 'login-ready').touch()
        self.client.post('/api/accounts', json={'login': True})
        self.done()
        self.assertEqual(self.accounts.job['state'], 'done')
        self.assertTrue(self.accounts.git.account['logged_in'])
        self.assertTrue(self.accounts.tunnels.account['logged_in'])

    def test_logout_also_signs_out_a_microsoft_tunnel_account(self):
        self.signed_in()
        (self.tunnels / 'account.json').write_text('{"status":"Logged in","provider":"Microsoft","username":"researcher"}')
        state = self.logout()
        self.assertEqual(state['activity']['state'], 'done')
        self.assertFalse(state['tunnels']['account']['logged_in'])

    def test_logout_attempts_both_services_and_reports_partial_failures(self):
        for failed, successful in [('git', 'tunnels'), ('tunnels', 'git')]:
            with self.subTest(failed=failed):
                self.signed_in()
                self.refresh()
                marker = (self.gh if failed == 'git' else self.tunnels) / 'logout-fail'
                marker.touch()
                state = self.logout()
                self.assertEqual(state['activity']['state'], 'error')
                self.assertTrue(state[failed]['account']['logged_in'])
                self.assertFalse(state[successful]['account']['logged_in'])
                self.assertNotIn('gho_TEST_ONLY_NEVER_VALID', json.dumps(state))
                marker.unlink()
                self.assertEqual(self.logout()['activity']['state'], 'done')

    def test_logout_reports_environment_credentials_without_erasing_them(self):
        self.signed_in()
        self.refresh()
        self.accounts.git.env['GH_TOKEN'] = 'gho_TEST_ONLY_NEVER_VALID'
        state = self.logout()
        self.assertEqual(state['activity']['state'], 'error')
        self.assertIn('GH_TOKEN', state['activity']['error'])
        self.assertNotIn('gho_TEST_ONLY_NEVER_VALID', json.dumps(state))
        self.assertTrue(state['git']['account']['logged_in'])
        self.assertFalse(state['tunnels']['account']['logged_in'])
        self.assertTrue((self.gh / 'account.json').exists())

    def test_logout_keeps_other_github_accounts_and_reports_the_new_active_user(self):
        self.signed_in()
        (self.gh / 'other-account.json').write_text('{"login":"another-user","token":"gho_OTHER_TEST"}')
        state = self.logout()
        self.assertEqual(state['activity']['state'], 'done')
        self.assertEqual(state['git']['account'], {'login': 'another-user', 'logged_in': True})

    def test_remote_logout_detaches_shared_credentials_without_touching_local_tunnels(self):
        self.signed_in()
        self.app.config['PREVIEW_SINGLE_PORT'] = True
        hub = self.accounts.git.remote
        hub.attach('a' * 64)
        state = self.logout()
        self.assertEqual(state['activity']['state'], 'done')
        self.assertFalse(state['git']['shared'])
        self.assertIsNone(state['tunnels'])
        self.assertIsNone(self.accounts.tunnels.activity)
        self.assertEqual(json.loads((self.tunnels / 'account.json').read_text())['status'], 'Logged in')
        with self.assertRaises(GitError):
            hub.poll('a' * 64, {}, 'researcher')

    def test_signout_blocks_concurrent_account_actions_and_survives_reload(self):
        self.signed_in()
        (self.tunnels / 'hold-logout').touch()
        self.assertEqual(self.client.post('/api/accounts/logout').status_code, 202)
        wait_for(lambda: (self.accounts.tunnels.snapshot()['activity'] or {}).get('kind') == 'logout')
        self.assertEqual(self.client.get('/api/accounts').get_json()['activity']['kind'], 'logout')
        self.assertEqual(self.client.post('/api/accounts/logout').status_code, 400)
        self.assertEqual(self.client.post('/api/accounts', json={'login': True}).status_code, 400)
        self.app.config['PREVIEW_SINGLE_PORT'] = True
        self.assertIsNone(self.client.get('/api/accounts').get_json()['activity'])
        self.assertEqual(self.client.post('/api/accounts/logout').status_code, 400)
        self.assertEqual(self.client.delete('/api/accounts').status_code, 400)
        self.app.config['PREVIEW_SINGLE_PORT'] = False
        (self.tunnels / 'hold-logout').unlink()
        self.done()
        self.assertEqual(self.accounts.job['state'], 'done')

    def test_signout_during_login_is_rejected(self):
        self.client.post('/api/accounts', json={'login': True})
        wait_for(lambda: 'TEST-2468' in (self.accounts.git.snapshot()['activity'] or {}).get('output', ''))
        self.assertEqual(self.client.post('/api/accounts/logout').status_code, 400)
        self.assertEqual(self.accounts.job['kind'], 'login')

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
            self.assertEqual(secured.test_client().post('/api/accounts/logout').status_code, 401)
        finally:
            close_app(secured)
        self.assertEqual(self.client.post('/api/accounts', json=[]).status_code, 400)
        self.assertEqual(self.client.post('/api/accounts', json={"provider": "unknown"}).status_code, 400)
        self.assertEqual(self.client.post('/api/accounts', json={}, headers={"Origin": "https://evil.invalid"}).status_code, 403)
        self.assertEqual(self.client.post('/api/accounts/logout', headers={"Origin": "https://evil.invalid"}).status_code, 403)


if __name__ == '__main__':
    unittest.main()
