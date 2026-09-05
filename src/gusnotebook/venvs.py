"""Find Python environments a notebook could use for its kernel.

The kernel is launched from an interpreter path (see kernel.py), so an
"environment" here is just a python binary plus a label. We look in the places
venvs conventionally live relative to the notebook, then in the usual
user-level homes (conda, virtualenvwrapper, pyenv, uv).
"""

import os
import subprocess
from pathlib import Path

# Directory names commonly used for an in-project venv.
LOCAL_NAMES = (".venv", "venv", ".env", "env")

# User-level directories that each contain many environments.
ENV_HOMES = (
    "~/.virtualenvs",              # virtualenvwrapper
    "~/miniconda3/envs",
    "~/anaconda3/envs",
    "~/opt/miniconda3/envs",
    "~/opt/anaconda3/envs",
    "~/.conda/envs",
    "~/.pyenv/versions",
    "~/Library/Caches/uv/environments",
)

# Standalone interpreters (not venvs) worth offering as a base to run on.
SYSTEM_GLOBS = (
    "/usr/local/bin/python3.*",
    "/opt/homebrew/bin/python3.*",
    "/usr/bin/python3",
    "/Library/Frameworks/Python.framework/Versions/*/bin/python3",
)

# How far up from the notebook to look for a project venv.
PARENT_DEPTH = 3

_version_cache = {}


def python_bin(prefix):
    """The interpreter inside an environment directory, or None."""
    p = Path(prefix)
    for rel in ("bin/python3", "bin/python", "Scripts/python.exe"):
        cand = p / rel
        try:
            if cand.is_file() and os.access(cand, os.X_OK):
                return cand
        except OSError:
            # A directory picker can encounter protected siblings (mounted
            # trash folders are a common Linux example). One unreadable entry
            # is not evidence that the directory containing it is unreadable.
            continue
    return None


def version_of(python):
    """'3.12.1' for an interpreter, or None if it won't run. Cached."""
    key = str(python)
    if key in _version_cache:
        return _version_cache[key]
    ver = None
    try:
        out = subprocess.run(
            [str(python), "-c",
             "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            ver = out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        ver = None
    _version_cache[key] = ver
    return ver


def has_ipykernel(python):
    """A kernel can only start if ipykernel is importable in that env."""
    try:
        out = subprocess.run(
            [str(python), "-c", "import ipykernel"],
            capture_output=True, timeout=15,
        )
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def describe(prefix_or_python, label=None, origin=None, probe=True):
    """Build one entry for the picker, or None if there's no usable python."""
    p = Path(prefix_or_python).expanduser()
    py = p if p.is_file() else python_bin(p)
    if py is None:
        return None
    prefix = py.parent.parent if p.is_file() else p
    entry = {
        "python": str(py),
        "prefix": str(prefix),
        "label": label or prefix.name,
        "origin": origin or "",
        "version": version_of(py) if probe else None,
    }
    if probe:
        entry["ipykernel"] = has_ipykernel(py)
    return entry


def system_pythons(probe=True):
    """Base interpreters on this machine, so there's always something to pick."""
    import glob
    out = []
    for pattern in SYSTEM_GLOBS:
        for hit in sorted(glob.glob(pattern)):
            p = Path(hit)
            if not p.is_file() or p.name.endswith("-config"):
                continue
            out.append(describe(p, label=p.name, origin="system", probe=probe))
    return out


def _dedup_key(entry):
    """What makes two entries the same environment.

    For a venv it's the prefix — never the binary, since a venv's bin/python3
    is a symlink to the base interpreter it was built from, and keying on that
    would fold every venv sharing a base Python into one entry. Also means
    `.venv/bin/python` and `.venv/bin/python3` count as the same env.
    For a bare interpreter (several pythons in one /bin) it's the binary.
    """
    prefix = Path(entry["prefix"])
    if (prefix / "pyvenv.cfg").is_file() or (prefix / "conda-meta").is_dir():
        return os.path.realpath(prefix)
    return os.path.realpath(entry["python"])


def _dedup(entries):
    seen, out = set(), []
    for e in entries:
        if e is None:
            continue
        key = _dedup_key(e)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def discover(near=None, include_homes=True, probe=True, current=None, registered=()):
    """Environments to offer for `near` (a notebook path or directory).

    Nearest-first: the environment already in use, the notebook's own
    directory, then its parents, then the user-level environment homes, then
    whatever is currently running.
    """
    found = []

    if current:
        found.append(describe(current, origin="in use", probe=probe))

    for entry in registered:
        found.append(describe(entry["prefix"], label=entry.get("label"),
                              origin="created with uv", probe=probe))

    if near:
        start = Path(near).expanduser()
        if start.is_file() or start.suffix:
            start = start.parent
        try:
            start = start.resolve()
        except OSError:
            pass
        for depth, d in enumerate([start] + list(start.parents)[:PARENT_DEPTH]):
            for name in LOCAL_NAMES:
                cand = d / name
                if not cand.is_dir():
                    continue
                rel = name if depth == 0 else f"{'../' * depth}{name}"
                found.append(describe(cand, label=name, origin=rel, probe=probe))

    if include_homes:
        for home in ENV_HOMES:
            hp = Path(home).expanduser()
            if not hp.is_dir():
                continue
            try:
                children = sorted(hp.iterdir(), key=lambda x: x.name.lower())
            except OSError:
                continue
            for child in children:
                if child.is_dir():
                    found.append(describe(
                        child, label=child.name, origin=home, probe=probe))

    import sys
    # "app default", not "current": this is the interpreter a notebook falls back
    # to, which is only the current one when nothing else was chosen.
    found.append(describe(sys.executable, label="running app",
                          origin="app default", probe=probe))
    found.extend(system_pythons(probe=probe))

    return _dedup(found)


def validate(python_or_prefix):
    """Resolve a user-supplied path to a usable interpreter.

    Raises ValueError with a reason the UI can show.
    """
    p = Path(python_or_prefix).expanduser()
    if not p.is_absolute():
        raise ValueError("path must be absolute")

    if p.is_file():
        py = p
    elif p.is_dir() and p.name.lower() in ("bin", "scripts"):
        # People commonly paste the directory displayed by `which python` or a
        # file picker. Accept it just like its environment root.
        names = (("python.exe",) if p.name.lower() == "scripts"
                 else ("python3", "python"))
        py = next((p / name for name in names
                   if (p / name).is_file() and os.access(p / name, os.X_OK)), None)
    else:
        py = python_bin(p)
    if py is None:
        raise ValueError(f"no python found in {p}")
    if not os.access(py, os.X_OK):
        raise ValueError(f"not executable: {py}")

    ver = version_of(py)
    if ver is None:
        raise ValueError(f"{py} did not run")
    return {
        "python": str(py),
        "prefix": str(py.parent.parent),
        "version": ver,
        "ipykernel": has_ipykernel(py),
        "label": py.parent.parent.name,
    }
