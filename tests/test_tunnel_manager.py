"""Saved tunnel lifecycle, cancellation, and local API boundaries."""

import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from gusnotebook.app import create_app, close_app
from gusnotebook.tunnel_manager import TunnelManager
from gusnotebook.tunnels import DevTunnels, TunnelError


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Tunnel worker did not finish")
        time.sleep(.01)


class Forward:
    def __init__(self):
        self.local_port = 19444
        self.failure = None
        self.done = threading.Event()
        self.close = Mock(side_effect=self.done.set)

    def wait_ready(self):
        pass


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "tunnels.json"
        self.manager = TunnelManager(self.path)
        self.forward = Forward()
        self.detail = {"tunnelId": "research.usw2", "labels": ["gusnotebook"], "hostConnections": 1}
        self.cli = Mock()
        self.cli.json.return_value = {"status": "Logged in", "provider": "GitHub", "username": "researcher"}
        self.cli.show.return_value = self.detail
        self.cli.entries.return_value = [self.detail]
        self.cli.connect.return_value = self.forward
        self.cli.owned = DevTunnels.owned
        self.manager._cli = Mock(return_value=self.cli)
        self.entry = self.manager.add({"name": "Research", "tunnel": "research"})

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def row(self):
        return self.manager.snapshot()["saved"][0]

    def test_saved_entries_persist_names_but_not_processes_or_credentials(self):
        duplicate = self.manager.add({"name": "Duplicate", "tunnel": "RESEARCH"})
        self.assertEqual(duplicate["id"], self.entry["id"])
        self.manager.rename(self.entry["id"], "Workstation")
        restored = TunnelManager(self.path)
        try:
            self.assertEqual(restored.snapshot()["saved"][0]["name"], "Workstation")
            self.assertEqual(restored.snapshot()["saved"][0]["state"], "unknown")
            self.assertIsNone(restored.account)
            self.assertEqual(set(json.loads(self.path.read_text())[0]), {"name", "id", "tunnel", "last_connected"})
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        finally:
            restored.close()

    def test_invalid_entries_and_corrupt_registry_do_not_destroy_existing_data(self):
        original = self.path.read_bytes()
        for body in [{"tunnel": "--evil"}, {"tunnel": "https://service/url"},
                     {"name": "bad\nname", "tunnel": "research"}, {"tunnel": 42}]:
            with self.assertRaises(TunnelError):
                self.manager.add(body)
        self.assertEqual(self.path.read_bytes(), original)
        self.path.write_text("broken registry")
        restored = TunnelManager(self.path)
        try:
            self.assertIn("Cannot read saved tunnels", restored.snapshot()["error"])
            with self.assertRaises(TunnelError):
                restored.add({"tunnel": "another"})
            self.assertEqual(self.path.read_text(), "broken registry")
            self.assertEqual(restored.saved, {})
        finally:
            restored.close()

    def test_save_failure_rolls_back_add_rename_and_remove(self):
        with patch.object(self.manager, "_save", side_effect=OSError("disk full")):
            for callback in [lambda: self.manager.add({"tunnel": "another"}),
                             lambda: self.manager.rename(self.entry["id"], "New name"),
                             lambda: self.manager.remove(self.entry["id"])]:
                with self.assertRaises(OSError):
                    callback()
                self.assertEqual(list(self.manager.saved.values()), [self.entry])
                self.assertEqual(self.manager.hidden, set())

    def test_invalid_id_type_in_registry_is_reported_without_breaking_startup(self):
        self.path.write_text(json.dumps([{"id": 42, "tunnel": "research"}]))
        restored = TunnelManager(self.path)
        try:
            self.assertIn("Invalid saved tunnel ID", restored.snapshot()["error"])
            self.assertEqual(restored.snapshot()["saved"], [])
        finally:
            restored.close()

    def test_discovery_refresh_and_missing_cli_are_reported_without_erasing_saved_entries(self):
        self.manager.refresh()
        wait_for(lambda: self.manager.activity["state"] != "running")
        self.assertEqual(self.manager.snapshot()["discovered"][0]["state"], "available")
        self.assertEqual(self.manager.snapshot()["account"]["username"], "researcher")
        self.cli.entries.side_effect = TunnelError("Service unavailable")
        self.manager.refresh()
        wait_for(lambda: self.manager.activity["state"] != "running")
        with patch("gusnotebook.tunnel_manager.executable", side_effect=TunnelError("Install devtunnel")):
            state = self.manager.snapshot()
        self.assertEqual(state["cli_error"], "Install devtunnel")
        self.assertEqual(state["activity"]["error"], "Service unavailable")
        self.assertEqual(len(state["saved"]), 1)

    def test_repeated_connect_and_canonical_alias_reuse_one_forward(self):
        identifier = self.entry["id"]
        for _ in range(10):
            self.manager.connect(identifier)
        wait_for(lambda: self.row()["state"] == "connected")
        alias = self.manager.add({"name": "Alias", "tunnel": "other-alias"})
        self.manager.connect(alias["id"])
        wait_for(lambda: self.manager.connections[alias["id"]]["state"] == "connected")
        self.cli.connect.assert_called_once()
        self.assertEqual(self.manager.saved[alias["id"]]["tunnel"], "research.usw2")
        self.assertEqual(self.row()["url"], "http://gusnotebook.localhost:19444/")
        self.assertTrue(self.row()["last_connected"])
        self.assertNotIn("process", self.path.read_text())
        self.manager.disconnect(identifier)
        wait_for(lambda: not self.manager.workers)
        self.assertEqual(self.row()["state"], "available")
        self.assertIsNone(self.row()["url"])
        self.assertNotIn("connected", [row["state"] for row in self.manager.snapshot()["saved"]])

    def test_remove_disconnects_and_does_not_delete_remote_tunnel(self):
        self.manager.connect(self.entry["id"])
        wait_for(lambda: self.row()["state"] == "connected")
        self.manager.remove(self.entry["id"])
        wait_for(lambda: not self.manager.workers)
        self.assertEqual(self.manager.snapshot()["saved"], [])
        self.assertTrue(self.forward.done.is_set())
        self.assertEqual(self.cli.json.call_args_list[0].args, ("user", "show"))
        self.assertEqual(self.cli.json.call_count, 1)
        self.assertEqual(json.loads(self.path.read_text()), [{"tunnel": "research.usw2", "hidden": True}])
        self.assertEqual(self.manager.snapshot()["discovered"], [])

    def test_removed_tunnel_stays_hidden_on_refresh_and_restart_and_can_be_added_again(self):
        self.manager.refresh()
        wait_for(lambda: self.manager.activity["state"] != "running")
        self.assertEqual(len(self.manager.snapshot()["discovered"]), 1)
        discovered = self.manager.add({"tunnel": "research.usw2"})
        self.manager.remove(discovered["id"])
        self.manager.refresh()
        wait_for(lambda: self.manager.activity["state"] != "running")
        self.assertEqual(self.manager.snapshot()["discovered"], [])
        restored = TunnelManager(self.path)
        try:
            restored.discovered = dict(self.manager.discovered)
            self.assertEqual(restored.snapshot()["discovered"], [])
            restored.add({"tunnel": "research.usw2"})
            self.assertEqual(restored.hidden, set())
            self.assertEqual(len(restored.snapshot()["saved"]), 2)
        finally:
            restored.close()

    def test_removing_short_id_hides_its_discovered_regional_id(self):
        self.manager.refresh()
        wait_for(lambda: self.manager.activity["state"] != "running")
        self.manager.remove(self.entry["id"])
        self.assertEqual(self.manager.snapshot()["saved"], [])
        self.assertEqual(self.manager.snapshot()["discovered"], [])
        self.manager.add({"tunnel": "research.usw2"})
        self.assertEqual(self.manager.hidden, set())

    def test_offline_unowned_and_logged_out_errors_do_not_start_forward(self):
        for detail, account, error in [
                ({**self.detail, "hostConnections": 0}, "Logged in", "offline"),
                ({**self.detail, "labels": ["vscode"]}, "Logged in", "not a GusNotebook"),
                (self.detail, "Logged out", "Sign in")]:
            self.cli.show.return_value = detail
            self.cli.json.return_value = {"status": account}
            self.manager.connect(self.entry["id"])
            wait_for(lambda: not self.manager.workers)
            self.assertEqual(self.row()["state"], "error")
            self.assertIn(error, self.row()["error"])
        self.cli.connect.assert_not_called()

    def test_forward_failure_can_be_retried_and_shutdown_closes_it(self):
        self.manager.connect(self.entry["id"])
        wait_for(lambda: self.row()["state"] == "connected")
        self.forward.failure = "Relay disconnected"
        self.forward.done.set()
        wait_for(lambda: self.row()["state"] == "error")
        self.assertEqual(self.row()["error"], "Relay disconnected")
        second = Forward()
        self.cli.connect.return_value = second
        self.manager.connect(self.entry["id"])
        wait_for(lambda: self.row()["state"] == "connected")
        with self.assertRaisesRegex(TunnelError, "Disconnect"):
            self.manager.refresh("microsoft")
        self.manager.close()
        self.assertTrue(second.done.is_set())
        self.assertFalse(self.manager.workers)

    def test_git_retry_reuses_transport_and_aliases_and_closes_replacement_on_disconnect(self):
        self.manager.git_auth = Mock()
        first = Mock(connected=True, error=None)
        second = Mock(connected=True, error=None)
        with patch("gusnotebook.git_bridge.CredentialForward", side_effect=[first, second]) as factory:
            self.manager.connect(self.entry["id"])
            wait_for(lambda: self.row()["state"] == "connected")
            url = self.row()["url"]
            first.connected, first.error = False, "Git sharing lost its connection"
            alias = self.manager.add({"name": "Alias", "tunnel": "other-alias"})
            self.manager.connect(alias["id"])
            wait_for(lambda: self.manager.connections[alias["id"]]["state"] == "connected")
            self.manager.retry_git_sharing(alias["id"])
            wait_for(lambda: self.row()["git_sharing"] and not self.row()["git_retrying"])
            first.close.assert_called_once()
            self.assertEqual(factory.call_count, 2)
            factory.assert_called_with(self.forward.local_port, self.manager.git_auth)
            self.cli.connect.assert_called_once()
            self.assertEqual(self.row()["url"], url)
            self.assertIsNone(self.row()["git_error"])
            self.assertEqual(self.row()["state"], "connected")
            self.manager.disconnect(alias["id"])
            wait_for(lambda: not self.manager.workers)
            second.close.assert_called()
            self.assertTrue(self.forward.done.is_set())

    def test_git_retry_is_deduplicated_and_cannot_restart_after_disconnect(self):
        self.manager.git_auth = Mock()
        started, release = threading.Event(), threading.Event()
        first = Mock(connected=False, error="Sharing stopped")
        def slow_close():
            if not started.is_set():
                started.set()
                release.wait(3)
        first.close.side_effect = slow_close
        with patch("gusnotebook.git_bridge.CredentialForward", return_value=first) as factory:
            self.manager.connect(self.entry["id"])
            wait_for(lambda: self.row()["state"] == "connected")
            self.manager.retry_git_sharing(self.entry["id"])
            self.assertTrue(started.wait(3))
            for _ in range(10):
                self.manager.retry_git_sharing(self.entry["id"])
            self.assertTrue(self.row()["git_retrying"])
            self.manager.disconnect(self.entry["id"])
            release.set()
            wait_for(lambda: not self.manager.workers)
            factory.assert_called_once()
            self.assertFalse(self.row()["git_retrying"])
        with self.assertRaisesRegex(TunnelError, "Connect this tunnel"):
            self.manager.retry_git_sharing(self.entry["id"])

    def test_cancel_during_metadata_query_never_starts_forward(self):
        started, release = threading.Event(), threading.Event()
        def slow_show(_name):
            started.set()
            release.wait(3)
            return self.detail
        self.cli.show.side_effect = slow_show
        self.manager.connect(self.entry["id"])
        self.assertTrue(started.wait(3))
        self.manager.disconnect(self.entry["id"])
        release.set()
        wait_for(lambda: not self.manager.workers)
        self.cli.connect.assert_not_called()
        self.assertNotEqual(self.row()["state"], "connected")

    def test_cancellation_reaps_an_actual_stalled_cli_process(self):
        cancel = threading.Event()
        errors = []
        def query():
            try:
                self.manager._run([sys.executable, "-c", "import time; time.sleep(60)"], cancel)
            except TunnelError as error:
                errors.append(str(error))
        worker = threading.Thread(target=query)
        worker.start()
        wait_for(lambda: bool(self.manager.children))
        process = next(iter(self.manager.children))
        cancel.set()
        worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertIsNotNone(process.poll())
        self.assertFalse(self.manager.children)
        self.assertEqual(errors, ["Canceled"])


class TunnelRoutesTests(unittest.TestCase):
    def test_api_auth_locality_prefix_and_remote_landing_navigation(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = create_app({"WORK_DIR": temporary, "STATE_DIR": Path(temporary) / "state",
                              "START_WATCHERS": False, "AUTH_TOKEN": "test-token", "APP_BASE_URL": "/nb",
                              "ALLOWED_HOSTS": ["localhost", "100.79.110.127", "gusnotebook.localhost"]})
            try:
                client = app.test_client()
                self.assertEqual(client.get("/nb/api/tunnels").status_code, 401)
                client.post("/nb/auth", headers={"Authorization": "Bearer test-token"})
                self.assertEqual(client.get("/nb/api/tunnels").status_code, 200)
                self.assertIn(b'id="sidebar-tunnels"', client.get("/nb/").data)
                launch = client.get("/nb/tunnels/connect")
                self.assertEqual(launch.status_code, 200)
                self.assertIn(b'/nb/static/tunnel-connect.js', launch.data)
                self.assertIn(b'data-base="/nb"', launch.data)
                for body in [[], None, {"tunnel": "https://example.com"}]:
                    self.assertEqual(client.post("/nb/api/tunnels", json=body).status_code, 400)
                self.assertEqual(client.post("/nb/api/tunnels", json={"tunnel": "research"}).status_code, 200)
                identifier = client.get("/nb/api/tunnels").get_json()["saved"][0]["id"]
                self.assertEqual(client.post(f"/nb/api/tunnels/{identifier}/git/retry").status_code, 400)
                with patch.object(app.extensions["gusnotebook"].tunnels, "retry_git_sharing") as retry:
                    self.assertEqual(client.post(f"/nb/api/tunnels/{identifier}/git/retry").status_code, 202)
                    retry.assert_called_once_with(identifier)
                self.assertEqual(client.post("/nb/api/tunnels", json={"tunnel": "evil"},
                                            headers={"Origin": "https://evil.invalid"}).status_code, 403)
                auth = {"Authorization": "Bearer test-token"}
                self.assertEqual(client.get("/nb/api/tunnels", base_url="http://100.79.110.127", headers=auth).status_code, 403)
                self.assertEqual(client.post(f"/nb/api/tunnels/{identifier}/git/retry", base_url="http://100.79.110.127", headers=auth).status_code, 403)
                self.assertNotIn(b'id="sidebar-tunnels"', client.get("/nb/", base_url="http://100.79.110.127", headers=auth).data)
                app.config["PREVIEW_SINGLE_PORT"] = True
                self.assertEqual(client.get("/nb/api/tunnels").status_code, 403)
                navigation = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate"}
                self.assertEqual(client.get("/nb/", headers=navigation).status_code, 200)
                self.assertEqual(client.get("/nb/api/tunnels", headers=navigation).status_code, 403)
                self.assertEqual(client.get("/nb/", headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)
            finally:
                close_app(app)


if __name__ == "__main__":
    unittest.main()
