"""Plotting guidance reaches both agents; starter upgrades preserve user edits."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from gusnotebook import llm, skills, terminals
from gusnotebook.plotting import D3_EXAMPLE, D3_INSTRUCTIONS


class PlottingInstructionsTests(unittest.TestCase):
    def test_both_agents_receive_default_without_user_settings(self):
        with patch.object(llm, 'load_settings', return_value={}), \
                patch.object(terminals, 'focus_hook_file', return_value=None):
            path = Path(terminals.system_prompt_file())
            try:
                self.assertIn(D3_INSTRUCTIONS, path.read_text())
            finally:
                path.unlink()
            args = terminals.codex_args()
            prompt = next(arg.split('=', 1)[1] for arg in args if arg.startswith('developer_instructions='))
            self.assertIn(D3_INSTRUCTIONS, json.loads(prompt))

    def test_explicit_alternative_and_workspace_instructions_are_kept(self):
        with patch.object(llm, 'load_settings', return_value={'claude_instructions': 'Label axes in seconds.'}):
            prompt = terminals.standing_instructions('Use Matplotlib for this notebook.')
        self.assertIn('unless the user\nexplicitly requests a different library', prompt)
        self.assertIn('Label axes in seconds.', prompt)
        self.assertIn('Use Matplotlib for this notebook.', prompt)

    def test_inline_generation_gets_same_default_and_user_override(self):
        client = Mock()
        client.chat.completions.create.return_value = Mock(
            choices=[Mock(message=Mock(content='print(1)'))], usage=None)
        with patch.object(llm, 'load_settings', return_value={'inline_llm_model': 'test'}), \
                patch.object(llm, 'client', return_value=client):
            llm.generate('Plot this with Matplotlib.')
        messages = client.chat.completions.create.call_args.kwargs['messages']
        self.assertIn(D3_INSTRUCTIONS, messages[0]['content'])
        self.assertIn('Plot this with Matplotlib.', messages[1]['content'])

    def test_example_is_executable_and_escapes_script_terminators(self):
        output = []
        code = skills.code_of(D3_EXAMPLE).replace('"label": "A"', '"label": "</script><script>bad()</script>"')
        with patch('IPython.display.display', side_effect=output.append):
            exec(code, {})
        self.assertEqual(len(output), 1)
        html = output[0].data
        self.assertEqual(html.count('</script>'), 1)
        self.assertIn(r'\u003c/script>', html)


class PlottingStarterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, value in {'PLUGIN_DIR': root, 'SKILLS_DIR': root / 'skills',
                            'MANIFEST': root / '.claude-plugin/plugin.json'}.items():
            p = patch.object(skills, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_new_install_uses_d3(self):
        skills.install_starters()
        self.assertIn(D3_INSTRUCTIONS, skills.get('plot-defaults')['body'])

    def test_untouched_legacy_starter_is_upgraded(self):
        skills.save(*skills.LEGACY_PLOT_DEFAULTS)
        skills.install_starters()
        self.assertIn(D3_INSTRUCTIONS, skills.get('plot-defaults')['body'])
        skills.install_starters()  # idempotent

    def test_customized_or_deleted_starter_is_preserved(self):
        original = skills.save(*skills.LEGACY_PLOT_DEFAULTS)
        custom = original['body'] + '\nUse our house palette.'
        skills.save('plot-defaults', 'Custom figure defaults', custom, sid='plot-defaults')
        skills.install_starters()
        self.assertEqual(skills.get('plot-defaults')['body'], custom)
        skills.save('my-other-skill', 'Keep this', 'My instructions')
        skills.delete('plot-defaults')
        skills.install_starters()
        self.assertEqual([s['id'] for s in skills.all_skills()], ['my-other-skill'])


if __name__ == '__main__':
    unittest.main()
