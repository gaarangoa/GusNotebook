"""Git operations, credential boundaries, forwarding and cancellation."""

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from werkzeug.serving import make_server
from gusnotebook.app import create_app, close_app
from gusnotebook.git_auth import GitAuth
from gusnotebook.git_bridge import CredentialForward, github_query
from gusnotebook.git_manager import GitManager
from gusnotebook.git_process import GitError, redact


def wait_for(predicate):
    deadline = time.monotonic() + 8
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Git operation did not finish")
        time.sleep(.02)


class GitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "project"
        self.repo.mkdir()
        config = self.root / "gitconfig"
        config.write_text('[user]\nname = Git Test\nemail = test@example.invalid\n')
        fake = self.root / "gh"
        fake.write_text("#!" + sys.executable + "\n" + Path(__file__).with_name("fake_gh.py").read_text())
        fake.chmod(0o755)
        self.environment = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "0", "GIT_CONFIG_PARAMETERS": "", "GIT_ASKPASS": "/usr/bin/false", "SSH_ASKPASS": "/usr/bin/false",
            "GUSNOTEBOOK_GH": str(fake), "GUSNOTEBOOK_FAKE_GH": str(self.root), "GH_TOKEN": "", "GITHUB_TOKEN": ""})
        self.environment.start()
        self.git("init", "-q")
        self.manager = GitManager(self.repo)

    def tearDown(self):
        self.manager.close()
        self.environment.stop()
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], text=True, capture_output=True, check=True).stdout

    def operation(self, action, **body):
        self.manager.operate({"path": str(self.repo), "action": action, **body})
        wait_for(lambda: self.manager.job["state"] != "running")
        self.assertEqual(self.manager.job["state"], "done", self.manager.job)

    def fill(self, auth, host="github.com", cwd=None):
        return subprocess.run(["git", "credential", "fill"], input=f"protocol=https\nhost={host}\n\n",
            env={**auth.environment(), "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/usr/bin/false"},
            cwd=cwd or self.repo, text=True, capture_output=True, timeout=40)

    def test_status_diff_stage_unstage_commit_and_rename(self):
        name = "a file [literal].txt"
        (self.repo / name).write_text("first\n")
        self.assertEqual(self.manager.status(str(self.repo))["changes"][0]["status"], "??")
        self.assertIn("first", self.manager.diff(str(self.repo), name)["text"])
        self.operation("stage", files=[name])
        self.assertIn("+first", self.manager.diff(str(self.repo), name, True)["text"])
        self.operation("unstage", files=[name])
        self.assertTrue((self.repo / name).exists())
        self.assertFalse(self.manager.status(str(self.repo))["changes"][0]["staged"])
        self.operation("stage", files=[name])
        self.operation("commit", message="First revision")
        self.assertEqual(self.manager.status(str(self.repo))["changes"], [])
        self.git("mv", name, "renamed.txt")
        row = self.manager.status(str(self.repo))["changes"][0]
        self.assertEqual((row["path"], row["old_path"]), ("renamed.txt", name))
        self.operation("unstage", files=[name, "renamed.txt"])
        self.assertTrue((self.repo / "renamed.txt").exists())

    def test_push_pull_and_divergence_preserve_uncommitted_work(self):
        (self.repo / "file.txt").write_text("first\n")
        self.operation("stage", files=["file.txt"])
        self.operation("commit", message="First")
        bare = self.root / "origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        self.git("remote", "add", "origin", str(bare))
        branch = self.git("branch", "--show-current").strip()
        self.git("push", "-u", "origin", branch)
        (self.repo / "file.txt").write_text("second\n")
        self.operation("stage", files=["file.txt"])
        self.operation("commit", message="Second")
        self.assertEqual(self.manager.status(str(self.repo))["ahead"], 1)
        self.operation("push")
        self.operation("fetch")
        self.operation("pull")
        self.assertEqual(self.manager.status(str(self.repo))["ahead"], 0)
        (self.repo / "file.txt").write_text("local draft\n")
        self.operation("pull")
        self.assertEqual((self.repo / "file.txt").read_text(), "local draft\n")
        peer = self.root / "peer"
        subprocess.run(["git", "clone", "-q", "--branch", branch, str(bare), str(peer)], check=True)
        (peer / "remote.txt").write_text("remote change\n")
        for args in [("add", "remote.txt"), ("commit", "-qm", "Remote change"), ("push", "-q")]:
            subprocess.run(["git", "-C", str(peer), *args], check=True)
        (self.repo / "local.txt").write_text("local change\n")
        self.operation("stage", files=["local.txt"])
        self.operation("commit", message="Local change")
        head = self.git("rev-parse", "HEAD")
        self.manager.operate({"action": "pull"})
        wait_for(lambda: self.manager.job["state"] != "running")
        self.assertEqual(self.manager.job["state"], "error")
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual((self.repo / "file.txt").read_text(), "local draft\n")

    def test_cancel_operation_reaps_hook_and_leaves_index_reviewable(self):
        (self.repo / "file.txt").write_text("change\n")
        self.operation("stage", files=["file.txt"])
        hook = self.repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nsleep 60\n")
        hook.chmod(0o755)
        self.manager.operate({"action": "commit", "message": "Canceled commit"})
        wait_for(lambda: bool(self.manager.commands.children))
        self.manager.cancel()
        wait_for(lambda: self.manager.job["state"] != "running")
        self.assertEqual(self.manager.job["state"], "canceled")
        self.assertFalse(self.manager.commands.children)
        self.assertTrue(self.manager.status(str(self.repo))["changes"][0]["staged"])

    def test_invalid_mutation_and_literal_pathspecs(self):
        for files in [["../outside"], ["/tmp/file"], [], "file"]:
            with self.assertRaises(GitError):
                self.manager.operate({"action": "stage", "files": files})
        for action in ["reset --hard", "force-push", "shell"]:
            with self.assertRaises(GitError):
                self.manager.operate({"action": action})
        (self.repo / "*.txt").write_text("literal")
        (self.repo / "other.txt").write_text("other")
        self.operation("stage", files=["*.txt"])
        self.assertEqual(self.git("diff", "--cached", "--name-only").strip(), "*.txt")

    def test_device_login_cancellation_existing_login_and_secret_free_snapshot(self):
        auth = self.manager.auth
        auth.refresh(login=True)
        wait_for(lambda: "TEST-2468" in auth.snapshot()["activity"]["output"])
        auth.cancel()
        wait_for(lambda: auth.snapshot()["activity"]["state"] == "canceled")
        self.assertFalse((self.root / "account.json").exists())
        (self.root / "login-ready").touch()
        auth.refresh(login=True)
        wait_for(lambda: auth.snapshot()["activity"]["state"] != "running")
        self.assertEqual(auth.snapshot()["account"], {"login": "researcher", "logged_in": True})
        result = self.fill(auth)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue("gho_TEST_ONLY_NEVER_VALID" in result.stdout, "Did not receive the fixture credential")
        self.assertNotIn("gho_TEST_ONLY_NEVER_VALID", json.dumps(auth.snapshot()))
        auth.refresh()
        wait_for(lambda: auth.snapshot()["activity"]["state"] != "running")
        self.assertTrue(auth.snapshot()["account"]["logged_in"])
        self.assertFalse(auth.commands.children)

    def test_existing_credentials_win_and_forwarded_credentials_are_not_stored(self):
        helper = self.root / "native.py"
        helper.write_text("import sys\nif sys.argv[-1] == 'get': print('username=native\\npassword=native-test-password')\n")
        self.git("config", "credential.helper", "!" + sys.executable + " " + str(helper))
        self.assertTrue("password=native-test-password" in self.fill(self.manager.auth).stdout)
        self.git("config", "credential.helper", "store --file=" + str(self.root / "stored"))
        self.manager.auth.resolve = lambda *_args, **_kwargs: {"username": "shared", "password": "forwarded-test-password"}
        result = self.fill(self.manager.auth)
        self.assertTrue("forwarded-test-password" in result.stdout)
        approved = subprocess.run(["git", "credential", "approve"], input="protocol=https\nhost=github.com\nusername=shared\npassword=forwarded-test-password\n\n",
            env=self.manager.auth.environment(), cwd=self.repo, text=True, capture_output=True)
        self.assertEqual(approved.returncode, 0, approved.stderr)
        self.assertFalse((self.root / "stored").exists())
        self.assertEqual(self.git("config", "credential.helper").strip(), "store --file=" + str(self.root / "stored"))
        self.git("config", "credential.https://github.com/org.helper", "store --file=" + str(self.root / "path-store"))
        self.git("config", "credential.useHttpPath", "true")
        approved = subprocess.run(["git", "credential", "approve"], input="protocol=https\nhost=github.com\npath=org/repo\nusername=shared\npassword=forwarded-test-password\n\n",
            env=self.manager.auth.environment(), cwd=self.repo, text=True, capture_output=True)
        self.assertEqual(approved.returncode, 0)
        self.assertFalse((self.root / "stored").exists())
        self.assertFalse((self.root / "path-store").exists())

    def test_unrelated_hosts_never_receive_github_credentials(self):
        self.manager.auth.resolve = lambda *_args, **_kwargs: {"username": "shared", "password": "secret-test"}
        for host in ["github.com.evil.invalid", "evil.invalid", "github.com:8080"]:
            result = self.fill(self.manager.auth, host=host)
            self.assertNotIn("secret-test", result.stdout)
            self.assertNotEqual(result.returncode, 0)
        for query in [{"protocol": "http", "host": "github.com"}, {"protocol": "https", "host": None},
                      {"protocol": "https", "host": "github.com", "path": "bad\npath"}]:
            with self.assertRaises(GitError):
                github_query(query)
        self.assertNotIn("gho_TEST_SECRET", redact("failed https://user:gho_TEST_SECRET@github.com/org/repo"))

    def test_remote_sharing_uses_private_session_and_stops_on_disconnect(self):
        app = create_app({"WORK_DIR": str(self.repo), "STATE_DIR": self.root / "state", "START_WATCHERS": False,
            "AUTH_REQUIRED": False, "PREVIEW_SINGLE_PORT": True, "ALLOWED_HOSTS": ["localhost", "gusnotebook.localhost"]})
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        remote_auth = app.extensions["gusnotebook"].git.auth
        # Remote host has no gh account. Only the laptop resolves this credential.
        self.manager.auth.resolve = lambda *_args, **_kwargs: {"username": "laptop", "password": "shared-test-password"}
        forward = CredentialForward(server.server_port, self.manager.auth)
        try:
            wait_for(lambda: remote_auth.remote.active())
            result = self.fill(remote_auth)
            self.assertTrue("password=shared-test-password" in result.stdout, "Did not receive the fixture credential")
            client = app.test_client()
            self.assertEqual(client.post("/api/git/bridge/detach", json={}).status_code, 403)
            self.assertEqual(client.post("/api/git/bridge/poll", json={}, headers={"X-GusNotebook-Git-Session": "wrong"}).status_code, 400)
            self.assertEqual(client.post("/api/git/bridge/attach", json={}, headers={"X-GusNotebook-Git-Session": secrets.token_hex(32)}).status_code, 400)
            snapshot = client.get("/api/git/auth").get_json()
            self.assertTrue(snapshot["shared"])
            self.assertNotIn("shared-test-password", json.dumps(snapshot))
            self.assertNotIn(forward.secret, json.dumps(snapshot))
            forward.close()
            self.assertFalse(remote_auth.remote.active())
            self.assertNotEqual(self.fill(remote_auth).returncode, 0)
            self.assertIsNone(remote_auth.remote.secret)
        finally:
            forward.close()
            server.shutdown()
            thread.join(3)
            close_app(app)
            server.server_close()

    def test_git_sharing_reports_unsupported_and_invalid_remote_responses(self):
        forward = object.__new__(CredentialForward)
        forward.port, forward.secret = 12345, "test-sharing-session"
        for status, payload, message in [
                (404, b"<html>Not found</html>", "Update GusNotebook on the remote computer"),
                (405, b"<html>Method not allowed</html>", "does not support Git credential sharing"),
                (200, b"<html>Unexpected login page</html>", "unexpected Git sharing response"),
                (503, b'{"error":"Git sharing relay unavailable"}', "Git sharing relay unavailable")]:
            with self.subTest(status=status), patch("gusnotebook.git_bridge.http.client.HTTPConnection") as connection:
                response = connection.return_value.getresponse.return_value
                response.status = status
                response.read.return_value = payload
                with self.assertRaisesRegex(GitError, message):
                    forward.request("attach", {})
                connection.return_value.close.assert_called_once()

    def test_git_api_requires_auth_and_same_origin(self):
        app = create_app({"WORK_DIR": str(self.repo), "STATE_DIR": self.root / "state", "START_WATCHERS": False,
                          "AUTH_TOKEN": "test-token", "APP_BASE_URL": "/nb"})
        try:
            client = app.test_client()
            self.assertEqual(client.get("/nb/api/git").status_code, 401)
            client.post("/nb/auth", headers={"Authorization": "Bearer test-token"})
            self.assertEqual(client.get("/nb/api/git").status_code, 200)
            self.assertEqual(client.post("/nb/api/git/operation", json={"action": "init"}, headers={"Origin": "https://evil.invalid"}).status_code, 403)
            self.assertEqual(client.post("/nb/api/git/bridge/attach", json={}).status_code, 403)
            self.assertEqual(client.post("/nb/api/git/operation", json=[]).status_code, 400)
            self.assertEqual(client.get("/nb/api/git/diff?file=../outside").status_code, 400)
        finally:
            close_app(app)


if __name__ == "__main__":
    unittest.main()
