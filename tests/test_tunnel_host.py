"""Recover expired transports while retaining the server and its resources."""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from gusnotebook.app import close_app, create_app
from gusnotebook.tunnel_host import TunnelHost
from gusnotebook.tunnels import DevTunnels, TunnelError


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('Tunnel did not reach the expected state')
        time.sleep(.02)


class TunnelHostTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.command = self.root / 'devtunnel'
        self.command.write_text('#!' + sys.executable + '\n' +
                                Path(__file__).with_name('fake_devtunnel.py').read_text())
        self.command.chmod(0o755)
        self.env = patch.dict(os.environ, {'GUSNOTEBOOK_FAKE_TUNNEL': str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = patch('sys.stdout', new_callable=io.StringIO)
        self.output.start()
        self.addCleanup(self.output.stop)
        cli = DevTunnels(str(self.command))
        name = cli.prepare('research-test', 4477)
        self.host = TunnelHost(str(self.command), name, 4477, retry_delay=.02, max_retry_delay=.1)
        self.addCleanup(self.host.close)
        self.host.start()

    def calls(self, command):
        return [json.loads(line) for line in (self.root / 'calls.jsonl').read_text().splitlines()
                if line and json.loads(line)[0] == command]

    def test_crash_and_expiry_without_exit_both_recover(self):
        for marker in ('crash-host', 'expire-host'):
            old = self.host.process
            (self.root / marker).touch()
            wait_for(lambda: self.host.process is not None and self.host.process is not old
                     and self.host.snapshot()['state'] == 'connected')
            self.assertIsNotNone(old.process.poll())
            self.assertIsNone(self.host.process.process.poll())
        self.assertEqual(len(self.calls('host')), 3)
        self.assertEqual(len(self.calls('create')), 1)
        self.assertTrue(all('--allow-anonymous' not in call for call in self.calls('host')))

    def test_periodic_renewal_keeps_the_live_transport(self):
        old = self.host.process
        self.host.renew_at = 0
        self.host.wake.set()
        wait_for(lambda: len(self.calls('update')) >= 2 and self.host.renew_at > 0)
        self.assertIs(self.host.process, old)
        self.assertEqual(len(self.calls('host')), 1)
        self.assertEqual(self.calls('update')[-1], ['update', 'research-test', '--expiration', '30d'])

    def test_expired_login_can_be_repaired_then_manually_renewed(self):
        (self.root / 'fail-renew').touch()
        (self.root / 'crash-host').touch()
        wait_for(lambda: self.host.snapshot()['state'] == 'retrying')
        self.assertIn('expired', self.host.snapshot()['error'])
        self.assertTrue(self.host.thread.is_alive())
        (self.root / 'fail-renew').unlink()
        self.host.request_renew()
        wait_for(lambda: self.host.snapshot()['state'] == 'connected')
        self.assertIsNone(self.host.snapshot()['error'])

    def test_management_failure_does_not_close_a_working_transport(self):
        old = self.host.process
        (self.root / 'fail-renew').touch()
        self.host.renew_at = 0
        self.host.wake.set()
        wait_for(lambda: self.host.snapshot()['error'] is not None)
        self.assertIs(self.host.process, old)
        self.assertIsNone(old.process.poll())
        self.assertEqual(self.host.snapshot()['state'], 'connected')

    def test_manual_renewal_replaces_only_the_transport(self):
        old = self.host.process
        self.host.request_renew()
        wait_for(lambda: self.host.snapshot()['state'] == 'connected' and self.host.process is not old)
        self.assertEqual(self.host.name, 'research-test')
        self.assertIsNotNone(old.process.poll())

    def test_shutdown_cancels_pending_commands_and_prevents_restart(self):
        old = self.host.process
        (self.root / 'hold-show').touch()
        self.host.request_renew()
        wait_for(lambda: bool(self.host.children))
        children = list(self.host.children)
        self.host.close()
        self.assertFalse(self.host.thread.is_alive())
        self.assertIsNotNone(old.process.poll())
        self.assertTrue(all(child.poll() is not None for child in children))
        self.assertEqual(self.host.snapshot()['state'], 'stopped')
        with self.assertRaises(TunnelError):
            self.host.request_renew()


class TunnelHostRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.app = create_app({'WORK_DIR': str(self.root), 'STATE_DIR': str(self.root / 'state'),
                               'START_WATCHERS': False, 'AUTH_TOKEN': 'test'})
        self.client = self.app.test_client()

    def tearDown(self):
        close_app(self.app)
        self.temporary.cleanup()

    def test_host_routes_require_auth_and_report_unsupported_servers(self):
        self.assertEqual(self.client.post('/api/tunnel-host', json={}).status_code, 401)
        self.client.post('/auth', headers={'Authorization': 'Bearer test'})
        self.assertEqual(self.client.get('/api/tunnel-host').status_code, 409)
        host = Mock()
        host.snapshot.return_value = {'name': 'research', 'state': 'connected', 'error': None}
        host.request_renew.return_value = {'name': 'research', 'state': 'reconnecting', 'error': None}
        self.app.extensions['gusnotebook_tunnel_host'] = host
        self.assertEqual(self.client.get('/api/tunnel-host').json['state'], 'connected')
        response = self.client.post('/api/tunnel-host', json={})
        self.assertEqual(response.status_code, 202)
        host.request_renew.assert_called_once()
        self.assertEqual(self.client.post('/api/tunnel-host', json={},
                         headers={'Origin': 'https://unrelated.example'}).status_code, 403)


if __name__ == '__main__':
    unittest.main()
