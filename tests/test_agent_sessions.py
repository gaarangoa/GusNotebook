"""Native-history discovery, bounded previews, and explicit resume routing."""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from gusnotebook.agent_sessions import AgentSessions, CodexHistory, ClaudeHistory, WINDOW
from gusnotebook.app import create_app, close_app
from gusnotebook import terminals


def write_session(root, provider, cwd, title='Analyse the data', session_id=None, source='cli'):
    session_id = session_id or str(uuid4())
    if provider == 'codex':
        path = root / 'sessions' / '2026' / '11' / '26' / f'rollout-{session_id}.jsonl'
        records = [dict(type='session_meta', payload=dict(id=session_id, cwd=str(cwd), source=source)),
                   dict(type='response_item', payload=dict(type='message', role='user', content=[dict(type='input_text', text=title)])),
                   dict(type='response_item', payload=dict(type='message', role='assistant', content=[dict(type='output_text', text='Here are the results.')]))]
    else:
        path = root / 'projects' / 'encoded-project' / (session_id + '.jsonl')
        records = [dict(type='user', sessionId=session_id, cwd=str(cwd), message=dict(role='user', content=title)),
                   dict(type='assistant', message=dict(role='assistant', content=[dict(type='text', text='Here are the results.')]))]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r) + '\n' for r in records))
    return session_id, path


class SessionFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.work = self.root / 'work'
        self.work.mkdir()
        self.codex = self.root / 'codex'
        self.claude = self.root / 'claude'
        self.state = self.root / 'bindings.json'
        self.providers = [CodexHistory(self.codex), ClaudeHistory(self.claude)]
        self.manager = AgentSessions(self.state, self.providers)


class AgentSessionTests(SessionFixture):
    def test_both_providers_exact_folder_children_and_symlink_alias(self):
        nested = self.work / 'nested'
        nested.mkdir()
        sibling = self.root / 'work-other'
        sibling.mkdir()
        a, _ = write_session(self.codex, 'codex', self.work)
        b, _ = write_session(self.claude, 'claude', self.work)
        c, _ = write_session(self.codex, 'codex', nested)
        write_session(self.claude, 'claude', sibling)
        write_session(self.codex, 'codex', self.work, source={'subagent': {'spawn': {}}})
        alias = self.root / 'alias'
        alias.symlink_to(self.work, target_is_directory=True)
        self.assertEqual({r['id'] for r in self.manager.catalog(alias)['sessions']}, {a, b})
        self.assertEqual({r['id'] for r in self.manager.catalog(self.work, True)['sessions']}, {a, b, c})

    def test_sqlite_index_and_older_rollouts_coexist_and_rename_is_used(self):
        sid, path = write_session(self.codex, 'codex', self.work)
        second, _ = write_session(self.codex, 'codex', self.work, 'Older CLI')
        with sqlite3.connect(self.codex / 'state_5.sqlite') as conn:
            conn.execute('create table threads (id text, cwd text, title text, rollout_path text, updated_at integer, archived integer)')
            conn.execute('insert into threads values (?, ?, ?, ?, ?, 0)', (sid, str(self.work), 'Indexed title', str(path), 12))
        (self.codex / 'session_index.jsonl').write_text(json.dumps(dict(id=sid, thread_name='Renamed conversation')) + '\n')
        rows = self.manager.catalog(self.work)['sessions']
        self.assertEqual({r['id'] for r in rows}, {sid, second})
        self.assertEqual(next(r for r in rows if r['id'] == sid)['title'], 'Renamed conversation')
        (self.codex / 'state_5.sqlite').write_bytes(b'not a database')
        self.assertEqual(len(self.manager.catalog(self.work)['sessions']), 2)

    def test_large_preview_is_bounded_and_omits_tools_and_hidden_thinking(self):
        sid, path = write_session(self.codex, 'codex', self.work)
        with path.open('a') as stream:
            stream.write(json.dumps(dict(type='response_item', payload=dict(type='function_call_output', output='x' * (WINDOW * 3)))) + '\n')
            stream.write('malformed json\n')
            stream.write(json.dumps(dict(type='response_item', payload=['malformed'])) + '\n')
            stream.write(json.dumps(dict(type='response_item', payload=dict(type='message', role='assistant', channel='analysis', content='private reasoning'))) + '\n')
            stream.write(json.dumps(dict(type='response_item', payload=dict(type='message', role='assistant', content='Visible answer'))) + '\n')
        row = self.manager.find(self.work, 'codex', sid)
        preview = self.manager.preview(row)
        self.assertTrue(preview['truncated'])
        self.assertEqual([m['text'] for m in preview['messages']], ['Visible answer'])
        self.assertLess(len(json.dumps(preview)), WINDOW)

    def test_claude_custom_title_and_only_text_messages(self):
        sid, path = write_session(self.claude, 'claude', self.work)
        with path.open('a') as stream:
            for row in [dict(type='custom-title', customTitle='Final analysis'),
                        dict(type='assistant', message=dict(role='assistant', content=[dict(type='thinking', thinking='hidden'), dict(type='text', text='Visible')])),
                        dict(type='user', message=dict(role='user', content=[dict(type='tool_result', content='tool output')]))]:
                stream.write(json.dumps(row) + '\n')
        row = self.manager.find(self.work, 'claude', sid)
        self.assertEqual(row['title'], 'Final analysis')
        self.assertEqual([m['text'] for m in self.manager.preview(row)['messages']], ['Analyse the data', 'Here are the results.', 'Visible'])

    def test_bindings_survive_restart_and_bad_ids_cannot_select_files(self):
        sid, _ = write_session(self.codex, 'codex', self.work)
        self.manager.remember('codex', sid, 'project-explicit')
        reopened = AgentSessions(self.state, self.providers)
        self.assertEqual(reopened.find(self.work, 'codex', sid)['project'], 'project-explicit')
        for value in ['--help', '../secrets', '$(echo broken)', str(uuid4())]:
            with self.assertRaises(ValueError):
                reopened.find(self.work, 'codex', value)
        with self.assertRaises(ValueError):
            reopened.find(self.root, 'codex', sid)


class AgentSessionRouteTests(SessionFixture):
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

    def test_api_filters_paginates_and_never_returns_storage_paths(self):
        for n in range(103):
            write_session(self.codex, 'codex', self.work, f'Analysis {n}')
        write_session(self.claude, 'claude', self.work, 'Special Claude title')
        data = self.client.get('/api/agent-sessions', query_string={'folder':str(self.work)}).json
        self.assertEqual(data['total'], 104)
        self.assertEqual(len(data['sessions']), 100)
        self.assertEqual(data['next_offset'], 100)
        self.assertNotIn('_path', json.dumps(data))
        filtered = self.client.get('/api/agent-sessions', query_string={'folder':str(self.work), 'provider':'claude', 'q':'special'}).json
        self.assertEqual(filtered['total'], 1)
        row = filtered['sessions'][0]
        preview = self.client.get('/api/agent-sessions/preview', query_string={'folder':str(self.work), 'provider':'claude', 'id':row['id']})
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(len(preview.json['messages']), 2)
        self.assertEqual(self.app.test_client().get('/api/agent-sessions').status_code, 401)

    def launch(self, provider, sid, cwd=None):
        return self.client.post('/api/terminals', json=dict(kind=provider, cwd=str(cwd or self.work), resume=sid))

    @patch('gusnotebook.terminals.Session.start', lambda s: (setattr(s, 'alive', True) or s))
    @patch('gusnotebook.terminals.codex_args', return_value=['-c', 'test=true'])
    @patch('gusnotebook.terminals.shutil.which', return_value='/fake/cli')
    def test_resume_uses_exact_id_and_reuses_live_terminal(self, which, args):
        sid, _ = write_session(self.codex, 'codex', self.work)
        response = self.launch('codex', sid)
        self.assertEqual(response.status_code, 200, response.json)
        terminal = self.runtime.terms.get(response.json['id'])
        self.assertEqual(terminal.command[-2:], ['resume', sid])
        self.assertIsNone(terminal.project)
        self.assertEqual(self.launch('codex', sid).json['id'], terminal.id)
        self.assertEqual(len(self.runtime.terms.list()), 1)
        self.assertEqual(self.launch('codex', '../secrets').status_code, 400)
        self.assertEqual(self.launch('claude', sid).status_code, 400)
        terminal.workspace = 'another-workspace'
        self.assertEqual(self.launch('codex', sid).status_code, 400)

    @patch('gusnotebook.terminals.Session.start', lambda s: (setattr(s, 'alive', True) or s))
    @patch('gusnotebook.terminals.claude_args', side_effect=lambda instructions, restrictions: ['--injected', instructions or ''])
    @patch('gusnotebook.terminals.shutil.which', return_value='/fake/cli')
    def test_resume_infers_project_from_saved_directory_and_survives_hook(self, which, args):
        project = self.client.post('/api/projects', json=dict(name='linked', location=str(self.work))).json
        sid, _ = write_session(self.claude, 'claude', project['path'])
        # The saved directory supplies context even for sessions created elsewhere.
        ordinary = self.launch('claude', sid, project['path']).json
        self.assertEqual(ordinary['project'], project['id'])
        self.runtime.terms.close(ordinary['id'])
        self.manager.remember('claude', sid, project['id'])
        linked = self.launch('claude', sid, project['path']).json
        self.assertEqual(linked['project'], project['id'])
        terminal = self.runtime.terms.get(linked['id'])
        self.assertEqual(terminal.command[-2:], ['--resume', sid])
        self.assertIn('html-slide-deck', terminal.command[3])
        new_id = str(uuid4())
        response = self.client.post('/api/prompt', json=dict(session_id=new_id), headers={'X-Terminal-Id':terminal.id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(terminal.agent_session, new_id)
        self.assertEqual(AgentSessions(self.state, self.providers).bindings['claude:' + new_id], project['id'])
        switched = self.client.post('/api/projects', json=dict(name='next', location=str(self.work)),
                                    headers={'X-Terminal-Id':terminal.id}).json
        self.assertEqual(AgentSessions(self.state, self.providers).bindings['claude:' + new_id], switched['id'])

    @patch('gusnotebook.terminals.shutil.which', return_value=None)
    def test_missing_cli_keeps_preview_but_cannot_resume(self, which):
        sid, _ = write_session(self.claude, 'claude', self.work)
        self.assertFalse(self.manager.find(self.work, 'claude', sid)['available'])
        self.assertEqual(self.launch('claude', sid).status_code, 400)


if __name__ == '__main__':
    unittest.main()
