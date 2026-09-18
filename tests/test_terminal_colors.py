"""Real shell highlighting and prompt setup without changing user dotfiles."""

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import shlex
import tempfile
import time
import unittest
from unittest.mock import patch

from gusnotebook import terminals


class TerminalColorTests(unittest.TestCase):
    @contextmanager
    def shell(self, kind='zsh', rc='', no_color=''):
        shell = shutil.which(kind)
        if not shell:
            self.skipTest(f'{kind} is not installed')
        with tempfile.TemporaryDirectory(prefix='gusnb-colors-') as temporary:
            root = Path(temporary).resolve()
            config = root / 'config'
            config.mkdir()
            startup = config / ('.zshrc' if kind == 'zsh' else '.bashrc')
            startup.write_text(rc or "PS1='COLOR_READY> '\n")
            command = [shell, '-l'] if kind == 'zsh' else [shell, '--noprofile', '-l']
            with patch.dict(os.environ, {'ZDOTDIR': str(config), 'NO_COLOR': no_color}), \
                    patch.object(terminals, 'SHELL_COMMAND', command), \
                    patch('gusnotebook.shell_startup.Path.home', return_value=config):
                session = terminals.Session('colors-test', root, command=command).start()
            try:
                self.wait_for(session, lambda data: b'\x1b]777;gusnotebook;' in data and b'>' in data)
                yield session, root
            finally:
                session.close()
            self.assertEqual(startup.read_text(), rc or "PS1='COLOR_READY> '\n")

    def wait_for(self, session, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            data = session.scrollback()
            if predicate(data):
                return data
            time.sleep(.03)
        self.fail(session.scrollback().decode(errors='replace'))

    def test_zsh_colors_commands_strings_options_and_unknown_commands(self):
        with self.shell() as (session, root):
            before = len(session.scrollback())
            session.write('echo --example "hello" .')
            data = self.wait_for(session, lambda data: all(color in data[before:]
                for color in (b'\x1b[32m', b'\x1b[33m', b'\x1b[36m')))
            self.assertIn(b'hello', data)
            session.write('\x03')
            session.write('gusnb_command_that_does_not_exist')
            self.wait_for(session, lambda data: b'\x1b[31m' in data[before:])
            session.write('\x03')
            session.write("printf 'executed' > result.txt\r")
            self.wait_for(session, lambda _data: (root / 'result.txt').exists())
            self.assertEqual((root / 'result.txt').read_text(), 'executed')

    def test_no_color_disables_our_prompt_and_highlighter(self):
        with self.shell(no_color='1') as (session, _root):
            before = len(session.scrollback())
            session.write('echo "hello"')
            data = self.wait_for(session, lambda data: b'hello' in data[before:])
            self.assertNotIn(b'\x1b[32m', data)
            self.assertNotIn(b'\x1b[33m', data)
            self.assertNotIn(b'\x1b[34m', data)

    def test_zsh_uses_minimal_prompt_instead_of_profile_prompt(self):
        with self.shell(rc="PROMPT='%F{magenta}COLOR_READY>%f '\n") as (session, _root):
            data = session.scrollback()
            self.assertNotIn(b'COLOR_READY', data)
            self.assertIn(b'\x1b[34m', data)

    def test_bash_plain_prompt_gets_color(self):
        with self.shell(kind='bash') as (session, _root):
            self.assertIn(b'\x1b[34m', session.scrollback())

    def test_header_follows_directory_and_environment_changes(self):
        for kind in ('zsh', 'bash'):
            with self.subTest(shell=kind), self.shell(kind=kind) as (session, root):
                folder = root / 'some path;with%chars'
                folder.mkdir()
                session.write('cd ' + shlex.quote(str(folder)) + '; export VIRTUAL_ENV="/tmp/test env"\r')
                encoded = str(folder).replace('%', '%25').replace(';', '%3B')
                marker = ('\x1b]777;gusnotebook;test env;' + os.environ['USER'] + ';' + encoded + '\x07').encode()
                self.wait_for(session, lambda data: marker in data)
                before = len(session.scrollback())
                session.write('unset VIRTUAL_ENV CONDA_DEFAULT_ENV\r')
                self.wait_for(session, lambda data: b'\x1b]777;gusnotebook;;' in data[before:])


if __name__ == '__main__':
    unittest.main()
