"""Project instructions follow terminal folders, never unrelated browser tabs."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gusnotebook.app import create_app, close_app
from gusnotebook import llm, terminals
from gusnotebook.project_agent import instructions_for, SLIDE_DECK_INSTRUCTIONS


class ProjectAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.app = create_app({'STATE_DIR': str(self.root / 'state'), 'WORK_DIR': str(self.root),
                               'START_WATCHERS': False, 'AUTH_TOKEN': 'test'})
        self.addCleanup(close_app, self.app)
        self.runtime = self.app.extensions['gusnotebook']
        self.workspace = self.runtime.store.current()
        self.client = self.app.test_client()
        self.client.environ_base.update(HTTP_AUTHORIZATION='Bearer test', HTTP_X_SESSION_ID=self.workspace.id)
        self.first = self.client.post('/api/projects', json={'name':'first', 'location':str(self.root)}).json
        self.second = self.client.post('/api/projects', json={'name':'second', 'location':str(self.root)}).json
        self.ordinary = SimpleNamespace(id='ordinary', project=None, cwd=str(self.root), workspace=self.workspace.id, kind='claude')
        self.project_agent = SimpleNamespace(id='project-agent', project=self.first['id'],
                                             cwd=self.first['path'], workspace=self.workspace.id, kind='codex')
        self.agents = {agent.id:agent for agent in (self.ordinary, self.project_agent)}
        lookup = patch.object(self.runtime.terms, 'get', side_effect=self.agents.get)
        lookup.start()
        self.addCleanup(lookup.stop)

    def request_as(self, agent, path='/api/projects/context'):
        return self.client.get(path, headers={'X-Terminal-Id': agent.id})

    def test_normal_agents_receive_no_project_rules_in_either_provider(self):
        with self.app.app_context(), patch.object(llm, 'load_settings', return_value={}), \
                patch.object(terminals, 'focus_hook_file', return_value=None):
            for extra, expected in (('Keep my custom preference.', False),
                                    (instructions_for(self.first), True)):
                claude = terminals.claude_args(extra)
                prompt_path = Path(claude[claude.index('--append-system-prompt-file') + 1])
                self.addCleanup(prompt_path.unlink, missing_ok=True)
                codex = terminals.codex_args(extra)
                codex_prompt = json.loads(next(arg.split('=',1)[1] for arg in codex
                                              if arg.startswith('developer_instructions=')))
                for prompt in (prompt_path.read_text(), codex_prompt):
                    self.assertEqual(SLIDE_DECK_INSTRUCTIONS.strip() in prompt, expected)
                    for rule in ('project context', 'layout_paths', 'Git paths', 'authorized inputs',
                                 'gusnb project data --file -', 'data/README.md', 'layout_paths.processed'):
                        self.assertEqual(rule in prompt, expected, rule)
                    self.assertIn('D3.js', prompt)
                    if not expected:
                        self.assertIn('Keep my custom preference.', prompt)

    def test_launch_infers_project_from_directory_and_preserves_subfolder(self):
        self.workspace.instructions = 'Keep my workspace preference.'
        for kind in ('claude','codex'):
            for cwd, linked in ((str(self.root), False), (self.first['path'], True),
                                (str(Path(self.first['path'])/'code/notebooks'), True)):
                made = SimpleNamespace(id='created', kind=kind, to_json=lambda:{'id':'created'})
                body = {'kind':kind, 'cwd':cwd}
                with patch.object(self.runtime.terms, 'create', return_value=made) as create, \
                        patch.object(terminals, 'command_for', return_value=[kind]) as command:
                    response = self.client.post('/api/terminals', json=body)
                self.assertEqual(response.status_code, 200, response.json)
                self.assertEqual(create.call_args.kwargs['project'], self.first['id'] if linked else None)
                self.assertEqual(create.call_args.args[0], cwd)
                prompt = command.call_args.args[1]
                self.assertIn('Keep my workspace preference.', prompt)
                self.assertEqual('project context' in prompt, linked)
                self.assertEqual('html-slide-deck' in prompt, linked)

    def test_directory_changes_attach_switch_and_detach_existing_terminal(self):
        from urllib.parse import quote
        for folder, expected in ((self.first['path'], self.first['id']),
                                 (str(Path(self.first['path'])/'code'), self.first['id']),
                                 (self.second['path'], self.second['id']), (str(self.root), None)):
            response = self.client.get('/api/projects/context', headers={
                'X-Terminal-Id': self.ordinary.id, 'X-Terminal-Cwd':quote(folder, safe='')})
            self.assertEqual(response.status_code, 200 if expected else 404)
            self.assertEqual(self.ordinary.project, expected)
            self.assertEqual(self.ordinary.cwd, folder)
            if expected:
                self.assertIn('read that original path directly', response.json['instructions'])

    def test_prompt_hook_directory_selects_project_and_records_prompt(self):
        response = self.client.post('/api/prompt', json={'prompt':'Use the approved cohort', 'cwd':self.second['path']},
                                    headers={'X-Terminal-Id':self.ordinary.id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ordinary.project, self.second['id'])
        events = self.runtime.projects.view(self.second['id'])['events']
        self.assertTrue(any(e.get('text') == 'Use the approved cohort' for e in events))
        self.client.post('/api/prompt', json={'prompt':'General notebook work', 'cwd':str(self.root)},
                         headers={'X-Terminal-Id':self.ordinary.id})
        self.assertIsNone(self.ordinary.project)

    def test_nested_projects_take_precedence_and_prefix_siblings_do_not_match(self):
        nested = self.runtime.projects.create('nested', self.first['path'])
        self.assertEqual(self.runtime.projects.for_directory(Path(nested['path'])/'code')['id'], nested['id'])
        self.assertIsNone(self.runtime.projects.for_directory(Path(self.first['path']+'ish')))

    def test_explicit_create_open_keeps_selection_until_directory_changes(self):
        response = self.client.post('/api/projects/'+self.first['id']+'/open', json={},
                                    headers={'X-Terminal-Id':self.ordinary.id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.request_as(self.ordinary).json['id'], self.first['id'])
        self.client.post('/api/prompt', json={'prompt':'Move project', 'cwd':self.second['path']},
                         headers={'X-Terminal-Id':self.ordinary.id})
        self.assertEqual(self.request_as(self.ordinary).json['id'], self.second['id'])

    def test_dataset_record_links_agent_request_native_session_and_notebook_history(self):
        from uuid import uuid4
        native_id = str(uuid4())
        headers = {'X-Terminal-Id':self.project_agent.id}
        self.client.post('/api/prompt', json={'prompt':'Build the adult cohort', 'session_id':native_id,
                                             'cwd':self.first['path']}, headers=headers)
        raw = self.root/'ages.csv'
        raw.write_text('age\n17\n23\n')
        base = '/api/projects/'+self.first['id']
        source = self.client.post(base+'/sources', json={'sources':[{'path':str(raw), 'description':'Ages provided by the user'}]}, headers=headers)
        self.assertEqual(source.status_code, 200, source.json)
        added = self.client.post('/api/cells', json={'cell_type':'code', 'source':'cohort = raw[raw.age >= 18]'}, headers=headers)
        output = Path(self.first['path'])/'data/processed/cohort.csv'
        output.write_text('age\n23\n')
        metadata = {'path':'data/processed/cohort.csv', 'inputs':['ages'], 'description':'Adult cohort',
                    'transformation':'Keep participants aged 18 or older',
                    'producer':{'path':self.first['notebook'], 'cell_id':added.json['id']}}
        response = self.client.post(base+'/data', json=metadata, headers=headers)
        self.assertEqual(response.status_code, 200, response.json)
        dataset = response.json['processed_data'][0]
        self.assertEqual(dataset['activity']['agent_session'], native_id)
        self.assertEqual(dataset['activity']['actor'], 'Codex')
        request_event = next(e for e in response.json['events'] if e.get('text') == 'Build the adult cohort')
        self.assertEqual(dataset['activity']['request_id'], request_event['request_id'])
        notebook = json.loads(Path(self.first['notebook']).read_text())
        cell = next(c for c in notebook['cells'] if c['id'] == added.json['id'])
        event = cell['metadata']['gusnotebook_history'][-1]
        self.assertEqual(event['kind'], 'dataset')
        self.assertEqual(event['destination'], str(output))
        self.assertIn(metadata['transformation'], event['note'])
        self.assertEqual(event['prompt'], 'Build the adult cohort')
        self.assertFalse(dataset['producer']['execution_observed'])
        self.assertEqual(self.app.test_client().post(base+'/data', json=metadata).status_code, 401)
        self.assertEqual(self.client.get(base+'/data').json['processed_data'][0]['path'], metadata['path'])

    def test_context_does_not_follow_browser_tabs_or_other_workspaces(self):
        for active in (self.first['report'], self.second['notebook']):
            self.runtime.store.set_active(self.workspace.id, active)
            self.assertEqual(self.request_as(self.ordinary).status_code, 404)
            response = self.request_as(self.project_agent)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['id'], self.first['id'])
        self.assertEqual(self.client.get('/api/projects/context').status_code, 404)
        self.project_agent.workspace = 'unrelated-workspace'
        self.assertEqual(self.request_as(self.project_agent).status_code, 404)
        self.assertEqual(self.request_as(SimpleNamespace(id='unknown')).status_code, 404)

    def test_project_agent_does_not_receive_another_notebooks_focused_cell(self):
        notebook = self.runtime.notebooks.get(self.second['notebook'])
        cell = notebook.to_json()['cells'][0]
        with patch('gusnotebook.app.get_focus', return_value=(self.second['notebook'], cell['id'])):
            ordinary = self.request_as(self.ordinary, '/api/here')
            linked = self.request_as(self.project_agent, '/api/here')
        self.assertEqual(ordinary.json['cell']['id'], cell['id'])
        self.assertIsNone(linked.json['cell'])
        self.assertNotIn('notebook', linked.json)

    def test_project_notebook_is_the_default_despite_other_open_notebooks(self):
        response = self.client.post('/api/cells', json={'cell_type':'code', 'source':'project_value = 42'},
                                    headers={'X-Terminal-Id':self.project_agent.id})
        self.assertEqual(response.status_code, 200, response.json)
        cell_id = response.json['id']
        self.assertIsNotNone(self.runtime.notebooks.get(self.first['notebook']).cell_json(cell_id))
        self.assertIsNone(self.runtime.notebooks.get(self.second['notebook']).cell_json(cell_id))

    def test_project_agent_does_not_receive_another_reports_visual_selection(self):
        visual = {'path':self.second['report'], 'document':'<p>Second project</p>',
                  'start':0, 'end':21, 'id':1, 'html':'<p>Second project</p>', 'text':'Second project'}
        with patch('gusnotebook.app.get_markup_focus', return_value=(visual, None)):
            self.assertEqual(self.request_as(self.ordinary, '/api/here').json['kind'], 'markup')
            self.assertIsNone(self.request_as(self.project_agent, '/api/here').json['cell'])
        visual['path'] = self.first['report']
        with patch('gusnotebook.app.get_markup_focus', return_value=(visual, None)):
            self.assertEqual(self.request_as(self.project_agent, '/api/here').json['kind'], 'markup')

    def test_git_paths_match_legacy_layout_without_excluding_its_notebooks(self):
        legacy = instructions_for({'layout':'legacy'})
        current = instructions_for(self.first)
        self.assertIn('`notebooks/`', legacy)
        self.assertIn('`pyproject.toml`', legacy)
        self.assertNotIn('`code/notebooks/`', legacy)
        self.assertIn('`code/notebooks/`', current)
        self.assertIn('`code/uv.lock`', current)
        self.assertIn('Exclude tests in `tests/`', legacy)
        self.assertIn('Exclude tests in `code/tests/`', current)


if __name__ == '__main__':
    unittest.main()
