"""Real interactive shells must retain uv and the selected notebook environment."""

import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import venv

from gusnotebook import terminals


class TerminalEnvironmentTests(unittest.TestCase):
    def exercise_shell(self, kind, profile_environment=False):
        shell = shutil.which(kind)
        if not shell:
            self.skipTest(f"{kind} is not installed")
        with tempfile.TemporaryDirectory(prefix="gusnb-terminal-env-") as temporary:
            root = Path(temporary).resolve()
            selected = root / "analysis env's"
            venv.EnvBuilder(with_pip=False, symlinks=True).create(selected)
            python = selected / "bin/python"
            config = root / "config"
            config.mkdir()
            # Simulate profiles that replace PATH after the launcher sets it.
            if kind == "bash":
                (config / ".bashrc").write_text('export PATH=/usr/bin:/bin\nexport GUSNB_RC_READ=yes\n')
                if profile_environment:
                    previous = root / "profile env"
                    venv.EnvBuilder(with_pip=False, symlinks=True).create(previous)
                    with (config / ".bashrc").open("a") as rc:
                        rc.write(". " + shlex.quote(str(previous / "bin/activate")) + "\n")
                command = [shell, "--noprofile", "-l"]
            else:
                for name in (".zshenv", ".zprofile", ".zshrc", ".zlogin"):
                    (config / name).write_text('export PATH=/usr/bin:/bin\n'
                        f'export GUSNB_ORDER="${{GUSNB_ORDER-}}{name},"\n')
                command = [shell, "-l"]
            uv = root / "tools" / "uv"
            uv.parent.mkdir()
            uv.write_text("#!/bin/sh\nprintf 'fixture-uv\\n'\n")
            uv.chmod(0o755)
            probe = root / "probe.py"
            probe.write_text('''import json, os, shutil, sys
from pathlib import Path
Path(sys.argv[1]).write_text(json.dumps({"python": sys.executable, "prefix": sys.prefix,
    "venv": os.environ.get("VIRTUAL_ENV"), "uv": shutil.which("uv"),
    "rc": os.environ.get("GUSNB_RC_READ"), "order": os.environ.get("GUSNB_ORDER"),
    "zdotdir": os.environ.get("ZDOTDIR")}))
''')
            active = root / "active.json"
            inactive = root / "inactive.json"
            with patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "ZDOTDIR": str(config),
                    "GUSNOTEBOOK_UV": str(uv), "GUSNB_ORDER": ""}), \
                    patch.object(terminals, "SHELL_COMMAND", command), \
                    patch("gusnotebook.shell_startup.Path.home", return_value=config):
                session = terminals.Session("env-test", root, command=command, python=str(python)).start()
            startup = Path(session._shell_startup.name)
            try:
                script = (shlex.join(["python", str(probe), str(active)]) + "; deactivate; "
                          + shlex.join([sys.executable, str(probe), str(inactive)]) + "; exit\r")
                session.write(script)
                deadline = time.monotonic() + 10
                while not inactive.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(inactive.exists(), session.scrollback().decode(errors="replace"))
                self.assertTrue(active.exists(), session.scrollback().decode(errors="replace"))
                data = json.loads(active.read_text())
                self.assertEqual(data["prefix"], str(selected))
                self.assertEqual(data["python"], str(python))
                self.assertEqual(data["venv"], str(selected))
                self.assertEqual(data["uv"], str(uv))
                after = json.loads(inactive.read_text())
                self.assertIsNone(after["venv"])
                self.assertEqual(after["uv"], str(uv))
                if kind == "bash":
                    self.assertEqual(data["rc"], "yes")
                else:
                    self.assertEqual(data["order"], ".zshenv,.zprofile,.zshrc,.zlogin,")
                    self.assertEqual(data["zdotdir"], str(config))
            finally:
                session.close()
            self.assertFalse(startup.exists())

    def test_bash_activates_after_rc_and_finds_uv(self):
        self.exercise_shell("bash")

    def test_zsh_activates_after_login_and_preserves_zdotdir(self):
        self.exercise_shell("zsh")

    def test_profile_environment_is_replaced_without_losing_uv(self):
        self.exercise_shell("bash", profile_environment=True)


if __name__ == "__main__":
    unittest.main()
