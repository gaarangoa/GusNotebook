"""Project-only dataset lineage, original inputs, and preserved human notes."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import nbformat

from gusnotebook import cellhistory
from gusnotebook.project_files import ProjectError, digest
from gusnotebook.projects import ProjectManager
from test_projects import FixtureRunner


class ProjectDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.manager = ProjectManager(self.base/'state', FixtureRunner())
        self.addCleanup(self.manager.close)
        self.project = self.manager.create('study', str(self.base))
        self.pid = self.project['id']
        self.root = Path(self.project['path'])
        self.raw = self.base/'measurements.csv'
        self.raw.write_text('age,value\n15,1\n20,2\n35,3\n')
        (self.root/'data/access.yaml').write_text(json.dumps({
            'version':1, 'files':[], 'directories':[str(self.base)]}))
        self.manager.sources(self.pid, [{'path':str(self.raw), 'description':'Original measurement cohort'}])
        self.code = self.root/'code/cohort.py'
        self.code.write_text('def cohort(frame):\n    return frame[frame.age >= 18]\n')
        self.output = self.root/'data/processed/cohort.csv'
        self.output.write_text('age,value\n20,2\n35,3\n')
        self.spec = dict(path='data/processed/cohort.csv', inputs=['measurements'],
                         producer={'path':'code/cohort.py'}, description='Adults with measured values',
                         transformation='Exclude participants younger than 18', parameters={'minimum_age':18})
        self.actor = dict(actor='Codex', terminal='agent-1', session='workspace',
                          agent_session='native-session', request_id='request-1')

    def record(self, spec=None):
        return self.manager.record_data(self.pid, spec or self.spec, self.actor)

    def test_records_original_inputs_code_context_and_readable_documentation(self):
        original = self.raw.read_bytes()
        project = self.record()
        dataset = project['processed_data'][0]
        self.assertEqual(dataset['inputs'][0]['path'], str(self.raw))
        self.assertEqual(dataset['producer']['source_sha256'], digest(self.code.read_bytes()))
        self.assertFalse(dataset['producer']['execution_observed'])
        self.assertEqual(dataset['activity'], self.actor)
        self.assertNotIn('sha256', dataset)  # data itself is not hashed
        self.assertEqual(self.raw.read_bytes(), original)
        self.assertFalse((self.root/'data/inputs').exists())
        readme = (self.root/'data/README.md').read_text()
        for value in ('Original measurement cohort', 'Adults with measured values',
                      'Exclude participants younger than 18', '../code/cohort.py', 'Codex', str(self.raw)):
            self.assertIn(value, readme)
        portable = json.loads((self.root/'data/lineage.json').read_text())
        self.assertEqual(portable, self.manager.data(self.pid))
        self.assertEqual(portable['processed_data'][0]['inputs'][0]['file_revision'],
                         portable['raw_inputs'][0]['file_revision'])

    def test_revisions_persist_without_dataset_copies_and_preserve_human_notes(self):
        self.record()
        readme = self.root/'data/README.md'
        readme.write_text('Human note: approved cohort definition.\n\n' + readme.read_text() + '\nFollow-up note.\n')
        self.output.write_text('age,value\n35,3\n')
        changed = {**self.spec, 'transformation':'Use adults aged at least 30', 'parameters':{'minimum_age':30}}
        self.record(changed)
        reloaded = ProjectManager(self.base/'state', FixtureRunner())
        self.addCleanup(reloaded.close)
        project = reloaded.view(self.pid)
        self.assertEqual(len(project['processed_data']), 1)
        history = [e for e in project['events'] if e['kind'] == 'processed_data_recorded']
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]['dataset']['parameters']['minimum_age'], 18)
        self.assertEqual(history[1]['dataset']['parameters']['minimum_age'], 30)
        self.assertIn('Human note:', readme.read_text())
        self.assertIn('Follow-up note.', readme.read_text())
        self.assertEqual(list((self.root/'data/processed').iterdir()), [self.output])

    def test_processed_inputs_keep_revision_links_and_reject_cycles_and_changed_inputs(self):
        first = self.record()['processed_data'][0]
        derived = self.root/'data/processed/summary.csv'
        derived.write_text('count\n2\n')
        second = {**self.spec, 'path':'data/processed/summary.csv', 'inputs':[self.spec['path']]}
        recorded = self.record(second)['processed_data'][-1]
        self.assertEqual(recorded['inputs'][0]['recorded_at'], first['recorded_at'])
        self.assertEqual(recorded['inputs'][0]['kind'], 'processed')
        with self.assertRaisesRegex(ProjectError, 'circular'):
            self.record({**self.spec, 'inputs':[second['path']]})
        self.output.write_text('changed\n1\n')
        with self.assertRaisesRegex(ProjectError, 'Approved input changed'):
            self.record(second)

    def test_invalid_relationships_paths_and_producers_leave_catalog_unchanged(self):
        outside = self.base/'outside.csv'
        outside.write_text('x\n1\n')
        alias = self.root/'data/processed/alias.csv'
        alias.symlink_to(outside)
        for changes in ({'path':str(outside)}, {'path':'data/processed/alias.csv'}, {'inputs':[]},
                        {'inputs':['unknown']}, {'inputs':['measurements',str(self.raw)]},
                        {'producer':{'path':'code/missing.py'}}, {'description':''},
                        {'producer':{'path':'../outside.py'}}, {'parameters':[]}):
            with self.subTest(changes=changes), self.assertRaises((ProjectError, OSError)):
                self.record({**self.spec, **changes})
        self.assertEqual(self.manager.data(self.pid)['processed_data'], [])
        self.manager.sources(self.pid, [{'id':'raw_output', 'path':str(self.output)}])
        with self.assertRaisesRegex(ProjectError, 'raw input'):
            self.record()

    def test_notebook_cell_links_only_claim_matching_observed_execution(self):
        path = self.root/'code/notebooks/analysis.ipynb'
        notebook = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell('cohort = raw[raw.age >= 18]', id='cohort')])
        cellhistory.append(notebook.cells[0], 'run', source=notebook.cells[0].source,
                           execution_count=3, output={'errors':[]})
        nbformat.write(notebook, path)
        spec = {**self.spec, 'producer':{'path':'code/notebooks/analysis.ipynb', 'cell_id':'cohort'}}
        producer = self.record(spec)['processed_data'][0]['producer']
        self.assertTrue(producer['execution_observed'])
        self.assertEqual(producer['execution']['execution_count'], 3)
        self.assertIn('cell `cohort`', (self.root/'data/README.md').read_text())
        notebook.cells[0].source += '\n# code changed since the run'
        nbformat.write(notebook, path)
        self.assertFalse(self.record(spec)['processed_data'][0]['producer']['execution_observed'])
        with self.assertRaisesRegex(ProjectError, 'existing code cell'):
            self.record({**spec, 'producer':{'path':str(path), 'cell_id':'missing'}})
        for cells in ([None], [{'id':'cohort', 'cell_type':'code', 'source':42}],
                      [{'id':'cohort', 'cell_type':'code', 'source':'', 'metadata':[]} ]):
            path.write_text(json.dumps({'cells':cells}))
            with self.subTest(cells=cells), self.assertRaisesRegex(ProjectError, 'notebook'):
                self.record(spec)

    def test_large_processed_files_are_recorded_without_full_reads_or_copies(self):
        with self.output.open('r+b') as stream:
            stream.truncate(100 * 1024 ** 3)
        dataset = self.record()['processed_data'][0]
        self.assertEqual(dataset['size'], 100 * 1024 ** 3)
        self.assertEqual(list((self.root/'data/processed').iterdir()), [self.output])
        self.assertLess((self.root/'data/lineage.json').stat().st_size, 20000)
        self.assertLess(sum(p.stat().st_size for p in (self.base/'state').rglob('*') if p.is_file()), 50000)

    def test_synthetic_inputs_are_explicit_and_readme_markers_are_protected(self):
        self.record({**self.spec, 'synthetic':True, 'inputs':[], 'parameters':{'random_seed':42}})
        readme = self.root/'data/README.md'
        self.assertIn('Synthetic data (declared)', readme.read_text())
        broken = readme.read_text().replace('<!-- gusnb:data:end -->', 'Human-edited marker')
        readme.write_text(broken)
        project = self.record()
        self.assertIn('catalog markers', project['projection_warning'])
        self.assertEqual(readme.read_text(), broken)
        self.assertEqual(json.loads((self.root/'data/lineage.json').read_text()), self.manager.data(self.pid))

    def test_existing_projects_get_docs_without_rewriting_their_notebooks(self):
        notebook = self.root/'code/notebooks/analysis.ipynb'
        original = notebook.read_bytes()
        self.output.unlink()
        self.output.parent.rmdir()
        (self.root/'data/README.md').write_text('# Existing notes\nKeep this.\n')
        (self.root/'data/lineage.json').unlink()
        project = self.manager.use_git(self.pid)
        self.assertTrue(Path(project['data_readme']).is_file())
        self.assertTrue(self.output.parent.is_dir())
        self.assertIn('Keep this.', Path(project['data_readme']).read_text())
        self.assertEqual(notebook.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
