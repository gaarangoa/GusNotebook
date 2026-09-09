"""Tunnel ownership, privacy, CLI lifecycle and preview origin boundaries."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from gusnotebook.app import create_app, close_app
from gusnotebook.tunnels import DevTunnels, TunnelError, TunnelProcess, tunnel_name


class TunnelTests(unittest.TestCase):
    def client(self, **detail):
        cli = DevTunnels("devtunnel-test")
        cli.show = Mock(return_value={"tunnelId": "research.usw2", "labels": ["gusnotebook"],
                                      "hostConnections": 0, **detail})
        cli.json = Mock(return_value={"accessControlEntries": []})
        cli.ports = Mock(return_value=[{"portNumber": 8888, "protocol": "http"}])
        return cli

    def test_reuse_checks_both_access_levels_and_never_grants_public_access(self):
        cli = self.client()
        self.assertEqual(cli.prepare("research", 8888), "research.usw2")
        self.assertEqual([call.args for call in cli.json.call_args_list], [
            ("access", "list", "research.usw2"),
            ("access", "list", "research.usw2", "--port-number", "8888"),
            ("access", "list", "research.usw2", "--port-number", "8888")])

    def test_refuses_shared_unknown_or_running_tunnels_without_mutations(self):
        for entries in [None, {}, [{}], [{"type": "Anonymous", "isDeny": False}],
                        [{"type": "Organizations", "isDeny": False}]]:
            cli = self.client()
            cli.json.return_value = {"accessControlEntries": entries}
            with self.subTest(entries=entries), self.assertRaises(TunnelError):
                cli.prepare("research", 8888)
        for detail in [{"labels": ["other-app"]}, {"hostConnections": 1}]:
            cli = self.client(**detail)
            with self.assertRaises(TunnelError):
                cli.prepare("research", 8888)
            cli.json.assert_not_called()

    def test_port_sharing_is_checked_even_when_tunnel_is_private(self):
        cli = self.client()
        cli.json.side_effect = [{"accessControlEntries": []}, {"accessControlEntries": [{"isDeny": False}]}]
        with self.assertRaises(TunnelError):
            cli.prepare("research", 8888)
        self.assertTrue(all(call.args[:2] == ("access", "list") for call in cli.json.call_args_list))

    def test_extra_ports_are_not_forwarded_or_modified(self):
        cli = self.client()
        cli.ports.return_value = [{"portNumber": 8888}, {"portNumber": 22}]
        with self.assertRaises(TunnelError):
            cli.prepare("research", 8888)
        with patch("gusnotebook.tunnels.TunnelProcess") as process, self.assertRaises(TunnelError):
            cli.connect("research")
        process.assert_not_called()

    def test_only_not_found_creates_a_tunnel(self):
        cli = DevTunnels("devtunnel-test")
        with patch("gusnotebook.tunnels.subprocess.run", return_value=Mock(returncode=2, stdout="", stderr="")):
            self.assertIsNone(cli.show("research", missing=True))
        with patch("gusnotebook.tunnels.subprocess.run", return_value=Mock(returncode=1, stdout="", stderr="offline")):
            with self.assertRaisesRegex(TunnelError, "offline"):
                cli.show("research", missing=True)

    def test_login_uses_requested_provider_and_device_flow(self):
        cli = self.client()
        cli.json.side_effect = [{"status": "Not logged in"}, {"status": "Logged in", "provider": "Microsoft"}]
        with patch("gusnotebook.tunnels.subprocess.run", return_value=Mock(returncode=0)) as run:
            cli.login("microsoft", device=True)
        self.assertEqual(run.call_args.args[0], ["devtunnel-test", "user", "login", "--entra", "--use-device-code-auth"])
        cli.json.side_effect = None
        cli.json.return_value = {"status": "Logged in", "provider": "GitHub"}
        with patch("gusnotebook.tunnels.subprocess.run") as run:
            cli.login()
        run.assert_not_called()

    def test_invalid_names_and_malformed_json_fail_closed(self):
        for value in ["--allow-anonymous", "name;touch /tmp/x", "https://a.example", "a/b", "a b", ""]:
            with self.subTest(value=value), self.assertRaises(TunnelError):
                tunnel_name(value)
        cli = DevTunnels("devtunnel-test")
        with patch("gusnotebook.tunnels.subprocess.run", return_value=Mock(returncode=0, stdout="invalid")):
            with self.assertRaises(TunnelError):
                cli.json("show", "research")

    def test_managed_process_detects_exit_and_reaps_on_timeout(self):
        with patch("sys.stdout", new_callable=io.StringIO):
            process = TunnelProcess([sys.executable, "-c", "raise SystemExit(3)"], hosting=True)
            with self.assertRaisesRegex(TunnelError, "exit 3"):
                process.wait_ready(timeout=2)
            self.assertIsNotNone(process.process.poll())
            process = TunnelProcess([sys.executable, "-c", "import time; time.sleep(10)"], hosting=True)
            with self.assertRaisesRegex(TunnelError, "timed out"):
                process.wait_ready(timeout=.1)
            self.assertIsNotNone(process.process.poll())

    def test_missing_cli_does_not_create_a_local_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = subprocess.run([sys.executable, "-m", "gusnotebook", "--connect", "research"],
                cwd=root, env={**os.environ, "GUSNOTEBOOK_HOME": str(root / "state"),
                               "GUSNOTEBOOK_DEVTUNNEL": str(root / "missing")}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("Install it on both computers", result.stderr)
            self.assertEqual(list(root.iterdir()), [])

    def test_proxy_flags_are_rejected_before_login_or_workspace_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for option in ["--trust-proxy", "--debug"]:
                result = subprocess.run([sys.executable, "-m", "gusnotebook", "--tunnel", "research", option],
                    cwd=root, env={**os.environ, "GUSNOTEBOOK_HOME": str(root / "state"),
                                   "GUSNOTEBOOK_DEVTUNNEL": str(root / "missing")}, capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn("omit proxy, debug", result.stderr)
            self.assertEqual(list(root.iterdir()), [])

    def test_single_port_previews_are_isolated_and_require_their_capability(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = root / "report.html"
            document.write_text("<h1>Report</h1>")
            app = create_app({"WORK_DIR": root, "STATE_DIR": root / "state", "START_WATCHERS": False,
                              "PREVIEW_SINGLE_PORT": True, "AUTH_REQUIRED": False,
                              "ALLOWED_HOSTS": ["gusnotebook.localhost"]})
            try:
                client = app.test_client()
                origin = "http://gusnotebook.localhost:3333"
                data = client.post("/api/preview", base_url=origin, json={"path": str(document),
                    "source": document.read_text(), "nonce": "test", "parent_origin": origin}).json
                preview = urlsplit(data["url"])
                self.assertEqual(preview.port, 3333)
                self.assertNotEqual(data["origin"], origin)
                self.assertTrue(preview.hostname.endswith(".gusnotebook.localhost"))
                self.assertEqual(client.get("/report.html", base_url=data["origin"]).status_code, 401)
                opened = client.get(data["url"])
                self.assertEqual(opened.status_code, 302)
                self.assertNotIn("access=", opened.headers["Location"])
                self.assertEqual(client.get(opened.headers["Location"], base_url=data["origin"]).status_code, 200)
                self.assertEqual(client.get("/api/notebook", base_url=data["origin"]).status_code, 404)
                self.assertEqual(client.get("/../state/settings.json", base_url=data["origin"]).status_code, 404)
                self.assertEqual(client.post("/api/cells", base_url=data["origin"]).status_code, 405)
                self.assertEqual(client.get("/api/notebook", base_url=origin,
                    headers={"Origin": data["origin"], "Sec-Fetch-Site": "same-site"}).status_code, 403)
                self.assertEqual(client.get("/api/notebook", base_url="http://unrelated.localhost:3333").status_code, 404)
                app.extensions["gusnotebook"].previews.close(document)
                self.assertEqual(client.get("/report.html", base_url=data["origin"]).status_code, 404)
            finally:
                close_app(app)


if __name__ == "__main__":
    unittest.main()
