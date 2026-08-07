"""Where GusNotebook keeps its state, and where its own code lives.

These are two different questions, and conflating them is what stops an app
being installable. Before this module every path came from
``Path(__file__).parent``: the settings file, the sessions store, the skills
plugin and the default notebook all lived beside the source. That works exactly
once — in a checkout you own. Installed with pip or uv the source sits in
``site-packages``, which may be read-only, is wiped on upgrade, and is shared
between every project you'd ever open.

So:

* **Code** — ``templates/``, and nothing else. Found relative to this file,
  because that genuinely is where it is. Read-only.
* **State** — settings, sessions, skills. One per user, under a config
  directory, honouring ``XDG_CONFIG_HOME`` / ``XDG_DATA_HOME`` on Linux and
  falling back to the same locations on macOS rather than
  ``~/Library/Application Support``: a user who edits ``settings.json`` by hand
  (which the README tells them they can) should find it somewhere typeable.
* **Work** — the directory the user ran ``gusnotebook`` in. That's the project
  they meant, the way ``jupyter lab`` means the directory you launched it from,
  so it's the file browser's root and where a new notebook goes. Never the
  install directory.

``GUSNOTEBOOK_HOME`` overrides the state directory wholesale, which is what the
test suites use: they mutate real settings, and pointing them at a temp home is
better than trusting them to put back what they changed.
"""

import os
import pathlib

APP_NAME = "gusnotebook"

# The default document, created on first launch in the state directory so a bare
# `gusnotebook` in a directory with no notebooks still opens onto something.
DEFAULT_NOTEBOOK = "notebook.ipynb"

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent

# Where the user launched us. Captured at import, not read per call: the server
# never chdirs, but a kernel or a PTY child can, and "the directory the user
# started me in" must not drift underneath the file browser mid-session.
LAUNCH_DIR = pathlib.Path.cwd()


def _base():
    """The state directory, created on demand.

    One directory rather than the XDG config/data split: the four things in it
    (settings, sessions, skills, the launch notebook) are all small, all
    hand-editable, and a user looking for "where does this keep its stuff"
    should find one answer. `mkdir` here rather than at import so merely
    importing the package touches no disk.
    """
    override = os.environ.get("GUSNOTEBOOK_HOME")
    if override:
        base = pathlib.Path(override).expanduser()
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        root = pathlib.Path(xdg).expanduser() if xdg else pathlib.Path.home() / ".config"
        base = root / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def state_dir():
    return _base()


def state(*parts):
    """A path inside the state directory, with its parents made."""
    p = _base().joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def work_dir():
    """The directory the app was launched in — the user's project."""
    return LAUNCH_DIR


def template_dir():
    return PACKAGE_DIR / "templates"


def static_dir():
    """The stylesheet and the page's JS — code, so beside the package.

    Stated explicitly for the same reason `template_dir` is: `Flask(__name__)`
    guesses both from the module's location, and a guess that happens to be
    right in a checkout is not the same as an answer. Read-only, replaced on
    upgrade, and never a place to write to.
    """
    return PACKAGE_DIR / "static"
