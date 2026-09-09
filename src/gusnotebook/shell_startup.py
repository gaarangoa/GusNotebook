"""Apply the notebook environment after the user's interactive shell startup."""

import os
from pathlib import Path
import shlex
import tempfile


SUPPORTED = {"bash", "zsh", "fish"}


def prepare(command, activate, environment, uv=None):
    """Return argv and a temporary startup directory, updating the child env.

    User startup files can replace PATH. Restore missing launcher paths and
    source the environment in the final interactive shell, so its prompt and
    deactivate function belong to that shell too. Never edit user dotfiles.
    """
    shell = command[0]
    kind = Path(shell).name
    old_venv = environment.pop("VIRTUAL_ENV", None)
    environment.pop("VIRTUAL_ENV_PROMPT", None)
    entries = list(dict.fromkeys(p for p in environment.get("PATH", "").split(os.pathsep)
                                if p and (not old_venv or p != str(Path(old_venv) / "bin"))))
    if uv and str(Path(uv).parent) not in entries:
        entries.insert(0, str(Path(uv).parent))
    environment["PATH"] = os.pathsep.join(entries)
    paths = " ".join(shlex.quote(p) for p in entries)

    if kind == "fish":
        # Fish provides an explicit hook after all its startup files.
        script = ""
        if activate:
            script += "if set -q VIRTUAL_ENV; and functions -q deactivate; deactivate; end\n"
        script += f"for _gusnb_bin in {paths}; contains -- $_gusnb_bin $PATH; or set -gx PATH $PATH $_gusnb_bin; end\n"
        script += "set -e _gusnb_bin\n"
        if activate:
            script += "source " + shlex.quote(activate + ".fish") + "\n"
        return [*command, "-i", "-C", script], None

    # A profile may activate its own venv. Deactivate it before restoring paths;
    # otherwise its saved PATH would discard uv during the next activation.
    script = ""
    if activate:
        script += 'if [ -n "${VIRTUAL_ENV-}" ] && typeset -f deactivate >/dev/null; then deactivate; fi\n'
    script += f"for _gusnb_bin in {paths}; do\n"
    script += '  case ":$PATH:" in *":$_gusnb_bin:"*) ;; *) PATH="${PATH:+$PATH:}$_gusnb_bin" ;; esac\ndone\n'
    script += "unset _gusnb_bin\nexport PATH\n"
    if activate:
        script += ". " + shlex.quote(activate) + "\n"

    temporary = tempfile.TemporaryDirectory(prefix="gusnb-shell-")
    directory = Path(temporary.name)
    try:
        if kind == "bash":
            rc = directory / "bashrc"
            user_rc = shlex.quote(str(Path.home() / ".bashrc"))
            rc.write_text(f"if [ -r {user_rc} ]; then . {user_rc}; fi\n" + script)
            # The outer login shell reads the login profile once. The final
            # interactive shell reads .bashrc, then activates the environment.
            inner = shlex.join([shell, "--rcfile", str(rc), "-i"])
            return [*command, "-c", "exec " + inner], temporary

        # Zsh reads .zlogin after .zshrc, so activation must come last. Forward
        # each startup file using the user's ZDOTDIR, including changes made
        # by earlier files, then restore it for nested shells and logout.
        environment["_GUSNB_ZDOTDIR_SET"] = "1" if "ZDOTDIR" in environment else "0"
        environment["_GUSNB_ZDOTDIR"] = environment.get("ZDOTDIR", "")
        environment["ZDOTDIR"] = str(directory)
        restore = ('if [[ $_GUSNB_ZDOTDIR_SET == 1 ]]; then\n'
                   '  export ZDOTDIR="$_GUSNB_ZDOTDIR"\nelse\n  unset ZDOTDIR\nfi\n')
        remember = ('_GUSNB_ZDOTDIR_SET=${+ZDOTDIR}\n_GUSNB_ZDOTDIR=${ZDOTDIR-}\n'
                    "export ZDOTDIR=" + shlex.quote(str(directory)) + "\n")
        for name in (".zshenv", ".zprofile", ".zshrc", ".zlogin"):
            forward = (f'if [[ -r "${{ZDOTDIR-$HOME}}/{name}" ]]; then\n'
                       f'  source "${{ZDOTDIR-$HOME}}/{name}"\nfi\n')
            final = "unset _GUSNB_ZDOTDIR_SET _GUSNB_ZDOTDIR\n" + script
            (directory / name).write_text(restore + forward + (final if name == ".zlogin" else remember))
        return [*command, "-i"], temporary
    except BaseException:
        temporary.cleanup()
        raise
