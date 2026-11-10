"""Atomic Quill persistence preserves concurrent execution and audited code."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gusnotebook.notebook import Registry
from gusnotebook.persistence import ExternalChangeError


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'document.gspy'
        self.doc = Registry().get(self.path)
        self.prose = self.doc.nb.cells[0].id
        self.code = self.doc.add_cell('code', 'answer = 42')['id']

    def blocks(self, text='Study results'):
        return [{'id': self.prose, 'cell_type': 'markdown', 'source': '<h1>' + text + '</h1>',
                 'delta': [{'insert': text}, {'insert': '\n', 'attributes': {'header': 1}}]},
                {'id': self.code, 'cell_type': 'code'}]

    def test_prose_save_preserves_newer_code_and_outputs(self):
        version = self.doc.editor_version()
        self.doc.update_cell(self.code, source='answer = 43')
        self.doc.set_outputs(self.code, [{'output_type': 'stream', 'name': 'stdout', 'text': '43'}], 1)
        self.doc.save_document(self.blocks(), version)
        reopened = Registry().get(self.path)
        self.assertEqual(reopened.cell_json(self.code)['source'], 'answer = 43')
        self.assertEqual(reopened.cell_json(self.code)['outputs'][0]['text'], '43')
        self.assertEqual(reopened.cell_json(self.prose)['rich_text'][1]['attributes']['header'], 1)
        self.assertEqual(reopened.cell_json(self.prose)['source'], '<h1>Study results</h1>')

    def test_external_text_or_order_conflicts(self):
        for mutate in (lambda: self.doc.update_cell(self.prose, source='Agent prose'),
                       lambda: self.doc.move_cell(self.code, 0)):
            version = self.doc.editor_version()
            mutate()
            before = self.path.read_bytes()
            with self.assertRaises(ExternalChangeError):
                self.doc.save_document(self.blocks(), version)
            self.assertEqual(self.path.read_bytes(), before)

    def test_agent_source_invalidates_rich_metadata(self):
        self.doc.save_document(self.blocks(), self.doc.editor_version())
        self.doc.update_cell(self.prose, source='## Agent prose')
        self.assertIsNone(self.doc.cell_json(self.prose)['rich_text'])

    def test_delete_undo_restores_archived_code_and_keeps_audit(self):
        self.doc.save_document(self.blocks()[:1], self.doc.editor_version())
        self.assertIsNone(self.doc.cell_json(self.code))
        self.doc.save_document(self.blocks(), self.doc.editor_version())
        self.assertEqual(self.doc.cell_json(self.code)['source'], 'answer = 42')
        events = self.doc.cell_history(self.code)['events']
        self.assertIn('delete', [event['kind'] for event in events])
        self.assertIn('restore', [event['kind'] for event in events])

    def test_invalid_edits_and_failed_storage_are_atomic(self):
        for op in ({'insert': {'gspyPython': 'injected'}}, {'insert': 'text', 'attributes': {'onclick': 'bad'}}, {'retain': 10}):
            blocks = self.blocks()
            blocks[0]['delta'] = [op]
            before = self.path.read_bytes()
            with self.assertRaises(ValueError):
                self.doc.save_document(blocks, self.doc.editor_version())
            self.assertEqual(self.path.read_bytes(), before)
        before = self.doc.to_json()
        with patch('gusnotebook.gspy.atomic_write', side_effect=OSError('Disk full')):
            with self.assertRaises(OSError):
                self.doc.save_document(self.blocks(), self.doc.editor_version())
        self.assertEqual(self.doc.to_json(), before)

    def test_display_mode_persists_and_is_audited(self):
        self.doc.set_display_mode(self.code, 'background')
        self.assertEqual(Registry().get(self.path).cell_json(self.code)['display_mode'], 'background')
        self.assertIn('gusnotebook_display', self.doc.export_history())
        with self.assertRaises(ValueError):
            self.doc.set_display_mode(self.code, 'hidden')

    def test_new_python_ignores_browser_source(self):
        blocks = self.blocks() + [{'id': 'new_python', 'cell_type': 'code', 'source': 'untrusted()'}]
        self.doc.save_document(blocks, self.doc.editor_version())
        self.assertEqual(self.doc.cell_json('new_python')['source'], '')


if __name__ == '__main__':
    unittest.main()
