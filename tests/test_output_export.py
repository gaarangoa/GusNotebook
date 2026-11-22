"""Provenance exports must retain the executed source and reject stale output."""

import copy
from html.parser import HTMLParser
import unittest

from gusnotebook import cellhistory
from gusnotebook.output_export import snapshot, render


class Elements(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.elements = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class OutputExportTests(unittest.TestCase):
    def setUp(self):
        self.cell = {'id': 'plot', 'source': 'display(HTML(chart))', 'execution_count': 3,
                     'outputs': [{'output_type': 'display_data', 'data': {
                         'text/html': '<div id="chart"></div><script>d3.select("#chart").text("Result")</script>'}}]}
        self.events = [{'id': 'run', 'kind': 'run', 'source': self.cell['source'],
                        'output': cellhistory.output_summary(self.cell['outputs'])}]

    def test_scripts_stay_in_opaque_frame_and_snapshot_keeps_source(self):
        payload = snapshot('/project/"study"/analysis.ipynb', self.cell, self.events)
        exported = render(payload)
        elements = Elements(exported).elements
        frame = next(attrs for tag, attrs in elements if tag == 'iframe')
        self.assertEqual(frame['sandbox'], 'allow-scripts')
        self.assertIn('d3.select("#chart")', frame['srcdoc'])
        self.assertIn('data-gusnb-frame-runtime', frame['srcdoc'])
        figure = next(attrs for tag, attrs in elements if tag == 'figure')
        self.assertEqual(figure['data-gusnb-notebook'], '/project/"study"/analysis.ipynb')
        self.assertEqual(figure['data-gusnb-snapshot'], payload['snapshot_id'])
        self.assertEqual(payload['code'], self.cell['source'])
        self.assertEqual(payload['history'], self.events)
        self.assertEqual(len([tag for tag, _ in elements if tag == 'script']), 2)

    def test_no_run_changed_code_or_changed_output_cannot_claim_provenance(self):
        for mutation in ('no_run', 'code', 'output'):
            cell, events = copy.deepcopy(self.cell), copy.deepcopy(self.events)
            if mutation == 'no_run':
                events = []
            elif mutation == 'code':
                cell['source'] = 'different()'
            else:
                cell['outputs'][0]['data']['text/html'] = 'fabricated output'
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'Run this cell'):
                snapshot('/analysis.ipynb', cell, events)

    def test_select_multiple_displayed_outputs_and_refuse_failed_runs(self):
        self.cell['outputs'].insert(0, {'output_type': 'stream', 'name': 'stdout', 'text': 'progress'})
        self.cell['outputs'].append({'output_type': 'display_data', 'data': {'text/plain': '<safe text>'}})
        self.events[-1]['output'] = cellhistory.output_summary(self.cell['outputs'])
        payload = snapshot('/analysis.ipynb', self.cell, self.events, 1)
        self.assertEqual(payload['output_mime'], 'text/plain')
        frame = next(attrs for tag, attrs in Elements(render(payload)).elements if tag == 'iframe')
        self.assertIn('<pre>&lt;safe text&gt;</pre>', frame['srcdoc'])
        for index in (-1, 2):
            with self.assertRaisesRegex(ValueError, 'No displayed output'):
                snapshot('/analysis.ipynb', self.cell, self.events, index)
        self.cell['outputs'].append({'output_type': 'error', 'ename': 'ValueError'})
        self.events[-1]['output'] = cellhistory.output_summary(self.cell['outputs'])
        with self.assertRaisesRegex(ValueError, 'execution error'):
            snapshot('/analysis.ipynb', self.cell, self.events)


if __name__ == '__main__':
    unittest.main()
