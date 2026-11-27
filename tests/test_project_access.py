"""Manual YAML input policies, bounded discovery and project-only app checks."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gusnotebook import project_access
from gusnotebook.project_files import ProjectError
from gusnotebook.projects import ProjectManager
from test_projects import FixtureRunner


class ProjectAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.manager = ProjectManager(self.base/'state', FixtureRunner())
        self.addCleanup(self.manager.close)
        self.project = self.manager.create('study', str(self.base))
        self.pid = self.project['id']
        self.root = Path(self.project['path'])
        self.policy = self.root/'data/access.yaml'
        self.raw = self.base/'cohort.csv'
        self.raw.write_text('age\n17\n22\n')

    def allow(self, files=(), directories=()):
        self.policy.write_text(json.dumps({'version':1, 'files':list(map(str, files)),
                                          'directories':list(map(str, directories))}))

    def register(self, path=None):
        return self.manager.sources(self.pid, [{'path':str(path or self.raw)}])

    def test_no_implicit_grant_and_denial_happens_before_file_inspection(self):
        self.assertEqual(self.manager.inputs(self.pid)['status'], 'empty')
        self.assertFalse(self.manager.inputs(self.pid)['filesystem_isolation'])
        with patch('gusnotebook.projects.inspect_source') as inspect:
            with self.assertRaisesRegex(ProjectError, 'not approved'):
                self.register()
            inspect.assert_not_called()
        self.assertEqual(self.manager.view(self.pid)['sources'], [])
        self.allow(files=[self.raw])
        registered = self.register()
        source = registered['sources'][0]
        self.assertEqual(source['access_revision'], self.manager.inputs(self.pid)['revision'])
        self.assertEqual(source['path'], str(self.raw))
        self.assertFalse((self.root/'data/inputs').exists())
        self.assertEqual(self.manager.inputs(self.pid, str(self.raw))['entries'][0]['kind'], 'file')

    def test_revocation_is_immediate_and_old_registrations_do_not_restore_access(self):
        self.allow(files=[self.raw])
        self.register()
        self.allow()
        refreshed = self.manager.use_git(self.pid)
        self.assertEqual(refreshed['events'][-1]['kind'], 'input_policy_observed')
        self.assertEqual(refreshed['events'][-1]['policy']['status'], 'empty')
        self.assertEqual(self.manager.view(self.pid)['sources'], [])
        self.assertEqual(self.manager.data(self.pid)['raw_inputs'], [])
        with self.assertRaisesRegex(ProjectError, 'not approved'):
            self.manager.save_analysis(self.pid, 'cohort', {}, ['cohort'])
        # Authoritative history is retained; editing a manifest cannot grant access.
        self.assertEqual(len(self.manager.projects[self.pid]['sources']), 1)
        (self.root/'data/.gusnotebook/project.json').write_text(json.dumps({'sources':[{'path':str(self.raw)}]}))
        reloaded = ProjectManager(self.base/'state', FixtureRunner())
        self.addCleanup(reloaded.close)
        self.assertEqual(reloaded.view(self.pid)['sources'], [])

    def test_directories_are_bounded_nonrecursive_and_do_not_include_prefix_siblings(self):
        folder = self.base/'approved'
        folder.mkdir()
        for number in range(7):
            (folder/f'{number}.csv').write_text('value\n1\n')
        subfolder = folder/'nested'
        subfolder.mkdir()
        (subfolder/'deep.csv').write_text('value\n2\n')
        (folder/'outside-link').symlink_to(self.raw)
        self.allow(directories=[folder])
        result, paths = self.manager.inputs(self.pid, str(folder), limit=2), []
        while True:
            self.assertLessEqual(len(result['entries']), 2)
            paths.extend(e['path'] for e in result['entries'])
            if result['next_offset'] is None:
                break
            result = self.manager.inputs(self.pid, str(folder), result['next_offset'], 2)
        self.assertEqual(len(paths), 8)
        self.assertNotIn(str(subfolder/'deep.csv'), paths)
        self.assertNotIn(str(folder/'outside-link'), paths)
        self.assertEqual(len(self.manager.inputs(self.pid, str(subfolder))['entries']), 1)
        self.register(subfolder/'deep.csv')
        for denied in (folder/'../cohort.csv', self.base/'approved-other/file.csv'):
            with self.subTest(path=denied), self.assertRaisesRegex(ProjectError, 'not approved'):
                self.manager.inputs(self.pid, str(denied))
        with self.assertRaises((ProjectError, OSError)):
            self.register(folder/'outside-link')

    def test_files_do_not_grant_directory_contents_or_sibling_files(self):
        self.allow(files=[self.base])
        with self.assertRaisesRegex(ProjectError, 'under directories'):
            self.manager.inputs(self.pid, str(self.base))
        with self.assertRaisesRegex(ProjectError, 'not approved'):
            self.register()
        alias = self.base/'alias'
        alias.symlink_to(self.base, target_is_directory=True)
        self.allow(directories=[alias])
        with self.assertRaises((ProjectError, OSError)):
            self.manager.inputs(self.pid, str(alias))
        with self.assertRaises((ProjectError, OSError)):
            self.register(alias/'cohort.csv')

    def test_invalid_policy_fails_closed_without_erasing_human_content(self):
        cases = ('', 'version: 1\nfiles: []\nfiles: []\ndirectories: []',
                 'version: 1\nfiles: &inputs []\ndirectories: *inputs',
                 '!!python/object/apply:os.system [echo unsafe]',
                 'version: true\nfiles: []\ndirectories: []',
                 'version: 1\nfiles: /data/file\ndirectories: []',
                 'version: 1\nfiles: []\ndirectories: []\nallow_all: true',
                 'x' * (64*1024+1))
        for raw in cases:
            self.policy.write_text(raw)
            with self.subTest(raw=raw[:100]):
                with self.assertRaises(ProjectError):
                    self.register()
                status = self.manager.use_git(self.pid)['data_access']
                self.assertEqual(status['status'], 'invalid')
                self.assertEqual(status['files'], [])
                self.assertEqual(self.policy.read_text(), raw)
        self.policy.unlink()
        with self.assertRaises(ProjectError):
            self.register()

    def test_relative_paths_resolve_from_project_and_existing_policies_are_preserved(self):
        raw = self.root/'data/manually-provided.csv'
        raw.write_text('value\n2\n')
        self.policy.write_text('version: 1\nfiles:\n  - data/manually-provided.csv\ndirectories: []\n# Human note\n')
        original = self.policy.read_bytes()
        self.assertEqual(self.manager.inputs(self.pid)['files'], [str(raw)])
        self.manager.use_git(self.pid)
        self.assertEqual(self.policy.read_bytes(), original)
        self.register(raw)

    def test_old_projects_receive_empty_policy_without_inferring_approval(self):
        self.allow(files=[self.raw])
        self.register()
        self.policy.unlink()
        project = self.manager.use_git(self.pid)
        self.assertEqual(project['data_access']['status'], 'empty')
        self.assertEqual(project['sources'], [])
        self.assertEqual(self.raw.read_text(), 'age\n17\n22\n')

    def test_revoked_raw_input_cannot_be_used_in_new_dataset_lineage(self):
        self.allow(files=[self.raw])
        self.register()
        output = self.root/'data/processed/cohort.csv'
        output.write_text('age\n22\n')
        code = self.root/'code/prepare.py'
        code.write_text('cohort = data[data.age >= 18]\n')
        spec = {'path':'data/processed/cohort.csv', 'inputs':['cohort'],
                'description':'Adults', 'transformation':'Age at least 18', 'producer':{'path':'code/prepare.py'}}
        self.allow()
        with patch('gusnotebook.project_data.verify_reference') as verify:
            with self.assertRaisesRegex(ProjectError, 'not approved'):
                self.manager.record_data(self.pid, spec)
            verify.assert_not_called()

    def test_structured_run_rechecks_policy_after_analysis_was_saved(self):
        self.allow(files=[self.raw])
        self.register()
        self.manager.prepare(self.pid)
        self.manager.jobs[self.pid]['_thread'].join(10)
        self.manager.save_analysis(self.pid, 'cohort', {}, ['cohort'])
        self.allow()
        with patch.object(self.manager.runner, 'execute') as execute:
            self.manager.run(self.pid, 'cohort')
            self.manager.jobs[self.pid]['_thread'].join(10)
            self.assertEqual(self.manager.view(self.pid)['job']['status'], 'failed')
            execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
