"""Session actions target native history, preserve projects, and reject live deletes."""

import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from gusnotebook.agent_sessions import AgentSessions
from gusnotebook.agent_session_actions import SessionActionError, codex_request
from test_agent_sessions import SessionFixture, write_session
from gusnotebook.app import create_app, close_app


class SessionMutationTests(SessionFixture):
    def test_claude_rename_appends_native_metadata_and_custom_title_wins(self):
        sid, path = write_session(self.claude, 'claude', self.work)
        original = path.read_bytes()
        row = self.manager.find(self.work, 'claude', sid)
        changed = self.manager.rename(row, '  PCA results α <table>  ')
        self.assertEqual(changed['title'], 'PCA results α <table>')
        self.assertTrue(path.read_bytes().startswith(original))
        self.assertEqual(json.loads(path.read_text().splitlines()[-1]),
                         dict(type='custom-title', customTitle='PCA results α <table>', sessionId=sid))
        with path.open('a') as stream:
            stream.write(json.dumps(dict(type='summary', summary='Automatic summary')) + '\n')
        reopened = AgentSessions(self.state, self.providers)
        self.assertEqual(reopened.find(self.work, 'claude', sid)['title'], 'PCA results α <table>')

    def test_claude_delete_only_removes_selected_transcript_and_children(self):
        sid, path = write_session(self.claude, 'claude', self.work)
        other_id, other = write_session(self.claude, 'claude', self.work)
        children = path.parent / sid / 'subagents'
        children.mkdir(parents=True)
        (children/'agent-test.jsonl').write_text('child history')
        notebook = self.work/'analysis.ipynb'
        notebook.write_text('notebook stays')
        self.manager.remember('claude', sid, 'project-kept')
        self.manager.delete(self.manager.find(self.work, 'claude', sid))
        self.assertFalse(path.exists())
        self.assertFalse(children.exists())
        self.assertTrue(other.exists())
        self.assertEqual(notebook.read_text(), 'notebook stays')
        self.assertNotIn('claude:'+sid, AgentSessions(self.state, self.providers).bindings)
        self.assertEqual([r['id'] for r in self.manager.catalog(self.work)['sessions']], [other_id])

    def test_invalid_titles_do_not_touch_history(self):
        sid, path = write_session(self.claude, 'claude', self.work)
        original = path.read_bytes()
        row = self.manager.find(self.work, 'claude', sid)
        for title in ['', '  ', None, ['title'], 'x'*181, 'line\nbreak', '\x1b[31mred']:
            with self.assertRaises(ValueError):
                self.manager.rename(row, title)
        self.assertEqual(path.read_bytes(), original)

    def test_deleted_or_symlinked_histories_cannot_mutate_outside_native_store(self):
        sid, path = write_session(self.claude, 'claude', self.work)
        row = self.manager.find(self.work, 'claude', sid)
        outside = self.work/'keep.txt'
        outside.write_text('keep')
        path.unlink()
        with self.assertRaises(ValueError):
            self.manager.rename(row, 'No resurrection')
        self.assertFalse(path.exists())
        path.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.manager.delete(row)
        self.assertEqual(outside.read_text(), 'keep')
        path.unlink()
        write_session(self.claude, 'claude', self.work, session_id=sid)
        (path.parent/sid).symlink_to(self.work, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.manager.delete(row)
        self.assertTrue(path.exists())
        self.assertTrue(outside.exists())

    def test_codex_actions_use_native_protocol_and_specific_id(self):
        sid, path = write_session(self.codex, 'codex', self.work)
        row = self.manager.find(self.work, 'codex', sid)
        def native(root, method, params):
            self.assertEqual(root, self.codex)
            self.assertEqual(params['threadId'], sid)
            if method == 'thread/name/set':
                (root/'session_index.jsonl').write_text(json.dumps(dict(id=sid, thread_name=params['name']))+'\n')
            elif method == 'thread/delete':
                path.unlink()
            else:
                self.fail(method)
        with patch('gusnotebook.agent_sessions.codex_request', side_effect=native) as request:
            self.assertEqual(self.manager.rename(row, 'Named Codex session')['title'], 'Named Codex session')
            self.manager.delete(row)
        self.assertEqual(request.call_count, 2)
        self.assertFalse(path.exists())

    def test_protocol_handshake_response_and_timeout(self):
        executable = self.root/'fake-codex'
        executable.write_text('#!'+sys.executable+'\n'+'''import json, sys
first = json.loads(sys.stdin.readline())
assert first['method'] == 'initialize'
print(json.dumps({'id': first['id'], 'result': {}}), flush=True)
assert json.loads(sys.stdin.readline())['method'] == 'initialized'
request = json.loads(sys.stdin.readline())
print(json.dumps({'method':'notification','params':{}}), flush=True)
print(json.dumps({'id':request['id'], 'result':{'method':request['method']}}), flush=True)
sys.stdin.read()
''')
        executable.chmod(0o755)
        with patch('gusnotebook.agent_session_actions.shutil.which', return_value=str(executable)):
            result = codex_request(self.root, 'thread/name/set', {'threadId':'example','name':'new'})
            self.assertEqual(result, {'method':'thread/name/set'})
        executable.write_text('#!'+sys.executable+'\nimport time\ntime.sleep(10)\n')
        with patch('gusnotebook.agent_session_actions.shutil.which', return_value=str(executable)):
            with self.assertRaisesRegex(SessionActionError, 'too long'):
                codex_request(self.root, 'thread/delete', {'threadId':'example'}, timeout=.1)


class MutationRoutesTests(SessionFixture):
    def setUp(self):
        super().setUp()
        self.app = create_app(dict(STATE_DIR=str(self.root/'state'), WORK_DIR=str(self.work),
                                  START_WATCHERS=False, AUTH_TOKEN='test'))
        self.addCleanup(close_app, self.app)
        self.runtime = self.app.extensions['gusnotebook']
        self.runtime.agent_sessions = self.manager
        self.workspace = self.runtime.store.current()
        self.client = self.app.test_client()
        self.client.environ_base.update(HTTP_AUTHORIZATION='Bearer test', HTTP_X_SESSION_ID=self.workspace.id)
        self.sid, self.path = write_session(self.claude, 'claude', self.work)
        self.body = dict(folder=str(self.work), provider='claude', id=self.sid)

    def test_rename_is_persistent_and_delete_is_real(self):
        response = self.client.patch('/api/agent-sessions', json={**self.body, 'title':'Saved title'})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['session']['title'], 'Saved title')
        response = self.client.delete('/api/agent-sessions', json=self.body)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.client.get('/api/agent-sessions', query_string={'folder':str(self.work)}).json['total'], 0)

    @patch('gusnotebook.terminals.Session.start', lambda s: (setattr(s, 'alive', True) or s))
    @patch('gusnotebook.terminals.claude_args', return_value=[])
    @patch('gusnotebook.terminals.shutil.which', return_value='/fake/claude')
    def test_live_rename_updates_terminal_and_delete_requires_close(self, which, args):
        opened = self.client.post('/api/terminals', json=dict(cwd=str(self.work), kind='claude', resume=self.sid)).json
        terminal = self.runtime.terms.get(opened['id'])
        response = self.client.patch('/api/agent-sessions', json={**self.body, 'title':'Live session name'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(terminal.label, 'Live session name')
        self.assertTrue(response.json['session']['running'])
        self.assertEqual(response.json['terminals'], [terminal.id])
        terminal.workspace = 'another-workspace'
        self.assertEqual(self.client.delete('/api/agent-sessions', json=self.body).status_code, 409)
        self.assertTrue(self.path.exists())
        self.runtime.terms.close(terminal.id)
        self.assertEqual(self.client.delete('/api/agent-sessions', json=self.body).status_code, 200)

    def test_auth_scope_validation_and_native_failure(self):
        self.assertEqual(self.app.test_client().delete('/api/agent-sessions', json=self.body).status_code, 401)
        for wrong in [dict(folder=str(self.root)), dict(provider='codex'), dict(id='../history')]:
            self.assertEqual(self.client.delete('/api/agent-sessions', json={**self.body, **wrong}).status_code, 400)
        with patch.object(self.providers[1], 'delete', side_effect=OSError('Permission denied')):
            response = self.client.delete('/api/agent-sessions', json=self.body)
        self.assertEqual(response.status_code, 400)
        self.assertIn('Permission denied', response.json['error'])
        self.assertTrue(self.path.exists())
        self.assertEqual(self.client.patch('/api/agent-sessions', json={**self.body, 'title':'\n'}).status_code, 400)
        self.assertEqual(self.client.patch('/api/agent-sessions', json=[]).status_code, 400)
        self.assertEqual(self.client.patch('/api/agent-sessions', json={**self.body, 'title':'x'*13000}).status_code, 413)


if __name__ == '__main__':
    unittest.main()
