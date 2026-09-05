"""Create notebook environments with uv and inspect their installed packages."""

import codecs
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from . import files, venvs
from .persistence import atomic_write


PACKAGE_SCRIPT = """
import importlib.metadata as metadata
import json, sys
from urllib.parse import unquote, urlsplit
packages = []
for dist in metadata.distributions():
    name = dist.metadata.get('Name')
    if not name:
        continue
    local_path, editable = None, False
    try:
        direct = json.loads(dist.read_text('direct_url.json') or '{}')
        url = urlsplit(direct.get('url', ''))
        if url.scheme == 'file':
            local_path = unquote(url.path)
            editable = bool(direct.get('dir_info', {}).get('editable'))
    except (ValueError, TypeError, AttributeError):
        pass
    packages.append(dict(name=name, version=dist.version,
                         local_path=local_path, editable=editable))
print(json.dumps(dict(version='.'.join(map(str, sys.version_info[:3])),
                     packages=sorted(packages, key=lambda p: p['name'].lower()))))
"""


def uv_binary():
    candidates = [os.environ.get("GUSNOTEBOOK_UV"), shutil.which("uv"),
                  str(Path(sys.executable).parent / "uv"),
                  str(Path.home() / ".local/bin/uv")]
    return next((str(Path(path).expanduser()) for path in candidates
                 if path and Path(path).expanduser().is_file()
                 and os.access(Path(path).expanduser(), os.X_OK)), None)


def _lines(value, label):
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list) or any(not isinstance(line, str) for line in value):
        raise ValueError(f"{label} must contain one entry per line")
    lines = [line.strip() for line in value if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) > 200 or sum(map(len, lines)) > 20000:
        raise ValueError(f"Too many {label.lower()}; use at most 200 entries")
    if any(line.startswith("-") or any(c in line for c in "\0\r\n") for line in lines):
        raise ValueError(f"Enter {label.lower()}, without command-line options")
    return list(dict.fromkeys(lines))


def _interpreter(path):
    if not isinstance(path, str) or not path.strip():
        raise ValueError("Choose an environment or Python interpreter")
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise ValueError("Environment paths must be absolute")
    if path.is_dir() and path.name.lower() in {"bin", "scripts"}:
        path = path.parent
    info = venvs.describe(path, probe=False)
    if not info:
        raise ValueError(f"No Python interpreter found in {path}")
    return info


def _package_result(info, output):
    try:
        data = json.loads(output)
        packages = data["packages"]
        return {**info, "version": data["version"], "packages": packages,
                "ipykernel": any(p["name"].lower() == "ipykernel" for p in packages)}
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("The interpreter returned an unreadable package list") from exc


def inspect_packages(path):
    """Read the selected interpreter's metadata; pip and uv are not required."""
    info = _interpreter(path)
    try:
        result = subprocess.run([info["python"], "-I", "-c", PACKAGE_SCRIPT],
                                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"Could not inspect this environment: {exc}") from exc
    if result.returncode:
        raise ValueError("Could not list installed packages: " + result.stderr[-2000:])
    return _package_result(info, result.stdout)


class EnvironmentManager:
    def __init__(self, registry_path, uv=None):
        self.registry_path = Path(registry_path)
        self.uv = uv or uv_binary()
        self._lock = threading.RLock()
        self._closed = False
        self.jobs = {}
        try:
            self._registered = json.loads(self.registry_path.read_text())
            if not isinstance(self._registered, list):
                self._registered = []
        except (OSError, ValueError):
            self._registered = []

    def registered(self):
        with self._lock:
            found = []
            for entry in self._registered:
                if not isinstance(entry, dict) or not isinstance(entry.get("prefix"), str):
                    continue
                try:
                    if Path(entry["prefix"]).is_dir():
                        found.append(dict(entry))
                except OSError:
                    continue
            return found

    def create(self, body, session):
        if not self.uv:
            raise ValueError("uv is not installed on this machine. Install uv and restart GusNotebook.")
        name, location = body.get("name"), body.get("location")
        if not isinstance(name, str) or not isinstance(location, str):
            raise ValueError("An environment name and location are required")
        if "\0" in name or len(name) > 120:
            raise ValueError("Choose a shorter environment name without null characters")
        parent = Path(location).expanduser()
        if not parent.is_absolute():
            raise ValueError("The location must be an absolute folder path")
        target = files.new_path(parent, name)
        requirements = _lines(body.get("packages", ""), "Packages")
        repositories = []
        for raw in _lines(body.get("repositories", ""), "Local repository paths"):
            path = Path(raw).expanduser()
            if not path.is_absolute() or not path.is_dir():
                raise ValueError(f"Local repository must be an existing absolute folder: {raw}")
            if not any((path / file).is_file() for file in ("pyproject.toml", "setup.py")):
                raise ValueError(f"{path.name} needs a pyproject.toml or setup.py to install")
            repositories.append(str(path.resolve()))
        python = body.get("python") or sys.executable
        if not isinstance(python, str) or python.startswith("-") or "\0" in python:
            raise ValueError("Choose a Python version or interpreter path")
        editable = body.get("editable", True)
        if not isinstance(editable, bool):
            raise ValueError("Editable must be true or false")
        with self._lock:
            if self._closed:
                raise ValueError("GusNotebook is shutting down")
            if sum(job["status"] in {"creating", "installing", "inspecting"} for job in self.jobs.values()) >= 2:
                raise ValueError("Two environments are already being created; wait for one to finish")
            # Reserve a new directory atomically. Never let uv replace an existing venv.
            target.mkdir()
            identity = (target.stat().st_dev, target.stat().st_ino)
            job = {"id": uuid.uuid4().hex, "session": session, "name": target.name,
                   "path": str(target), "status": "creating", "log": "", "error": None,
                   "created": time.time(), "environment": None,
                   "_cancel": threading.Event(), "_process": None}
            self.jobs[job["id"]] = job
            for key in list(self.jobs)[:-20]:
                if self.jobs[key]["status"] in {"ready", "failed", "cancelled"}:
                    self.jobs.pop(key)
            thread = threading.Thread(target=self._create, args=(job, python, requirements,
                                      repositories, editable, identity), daemon=True,
                                      name="gusnb-env-" + job["id"][:8])
            job["_thread"] = thread
            try:
                thread.start()
            except RuntimeError:
                self.jobs.pop(job["id"])
                target.rmdir()
                raise
            return self._view(job)

    @staticmethod
    def _view(job):
        return {key: value for key, value in job.items() if not key.startswith("_")}

    def get(self, job_id, session):
        with self._lock:
            job = self.jobs.get(job_id)
            if not job or job["session"] != session:
                raise ValueError("No such environment creation in this workspace")
            return self._view(job)

    def jobs_for(self, session):
        with self._lock:
            return [self._view(job) for job in reversed(list(self.jobs.values()))
                    if job["session"] == session]

    def _update(self, job, **values):
        with self._lock:
            job.update(values)

    @staticmethod
    def _kill(process):
        if process and process.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass

    def cancel(self, job_id, session):
        with self._lock:
            self.get(job_id, session)
            job = self.jobs[job_id]
            if job["status"] not in {"ready", "failed", "cancelled"}:
                job["_cancel"].set()
                self._kill(job["_process"])
            return self._view(job)

    def _run(self, job, command, log=True):
        environment = dict(os.environ)
        for key in ("UV_SYSTEM_PYTHON", "UV_TARGET", "UV_PREFIX", "UV_VENV_CLEAR",
                    "UV_VENV_ALLOW_EXISTING", "PYTHONPATH", "PYTHONHOME"):
            environment.pop(key, None)
        with self._lock:
            if job["_cancel"].is_set():
                raise RuntimeError("Environment creation cancelled")
            process = subprocess.Popen(command, cwd=str(Path(job["path"]).parent),
                env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, start_new_session=os.name == "posix")
            job["_process"] = process
        timed_out = threading.Event()

        def expire():
            timed_out.set()
            self._kill(process)

        timer = threading.Timer(900, expire)
        timer.daemon = True
        timer.start()
        output = ""
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            with process.stdout:
                while True:
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    text = decoder.decode(chunk)
                    output = (output + text)[-2000000:]
                    if log:
                        with self._lock:
                            job["log"] = (job["log"] + text)[-100000:]
            code = process.wait()
            if job["_cancel"].is_set():
                raise RuntimeError("Environment creation cancelled")
            if timed_out.is_set():
                raise RuntimeError("The command exceeded 15 minutes; creation stopped")
            if code:
                raise RuntimeError(output.strip()[-3000:] or f"Command exited with status {code}")
            return output
        finally:
            timer.cancel()
            self._kill(process)
            process.wait()
            self._update(job, _process=None)

    def _create(self, job, python, requirements, repositories, editable, identity):
        target = Path(job["path"])
        uv = [self.uv, "--no-config", "--color", "never", "--no-progress"]
        try:
            self._run(job, uv + ["venv", "--no-project", "--python", python,
                                "--prompt", job["name"], str(target)])
            info = _interpreter(str(target))
            self._update(job, status="installing")
            command = uv + ["pip", "install", "--python", info["python"]]
            for path in repositories:
                command.extend(["--editable", path] if editable else [path])
            command.extend(["ipykernel", *requirements])
            self._run(job, command)
            self._update(job, status="inspecting")
            output = self._run(job, [info["python"], "-I", "-c", PACKAGE_SCRIPT], log=False)
            result = _package_result(info, output)
            with self._lock:
                if job["_cancel"].is_set():
                    raise RuntimeError("Environment creation cancelled")
                entry = {key: result[key] for key in ("prefix", "python", "version", "label")}
                registered = [old for old in self._registered
                              if isinstance(old, dict) and old.get("prefix") != entry["prefix"]]
                registered.append(entry)
                atomic_write(self.registry_path, json.dumps(registered), mode=0o600)
                self._registered = registered
                job.update(status="ready", environment=result)
        except Exception as exc:
            error = str(exc)
            try:
                if not target.is_symlink() and (target.stat().st_dev, target.stat().st_ino) == identity:
                    shutil.rmtree(target)
            except FileNotFoundError:
                pass
            except OSError as cleanup:
                error += f"\nCould not remove incomplete environment {target}: {cleanup}"
            self._update(job, status="cancelled" if job["_cancel"].is_set() else "failed", error=error)

    def close(self):
        with self._lock:
            self._closed = True
            jobs = list(self.jobs.values())
            for job in jobs:
                self.cancel(job["id"], job["session"])
        deadline = time.monotonic() + 5
        for job in jobs:
            job["_thread"].join(timeout=max(0, deadline - time.monotonic()))
