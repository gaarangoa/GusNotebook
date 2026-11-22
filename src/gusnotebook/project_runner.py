"""Project-local uv environments and bounded, cancellable Python subprocesses.

Virtual environments isolate dependencies, not filesystem or network access.
Only trusted analysis code should be run with this host user's permissions.
"""

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time

from .environments import uv_binary
from .project_files import ProjectError, digest, read_file, write_file

WORKER = r'''
import base64, contextlib, importlib, importlib.metadata, io, json, os, platform, subprocess, sys
from pathlib import Path
import pandas as pd

class Tail(io.StringIO):
    def write(self, text):
        super().write(text)
        if self.tell() > 32768:
            value = self.getvalue()[-16384:]
            self.seek(0); self.truncate(); super().write(value)
        return len(text)

snapshot = Path(sys.argv[1]).resolve()
work = Path(sys.argv[2]).resolve()
uv = os.environ['GUS_PROJECT_UV']
request = json.loads((snapshot / 'request.json').read_text())
if request.get('wheel') and not os.environ.get('GUS_WHEEL_READY'):
    wheel = work / request['wheel']['name']
    wheel.write_bytes(base64.b64decode(request['wheel']['data']))
    subprocess.run([uv, 'venv', '--python', sys.executable, str(work / 'package-env')], check=True)
    # The clean package environment reuses the locked dependency environment,
    # never the analysis source tree. A venv's system-site-packages points to
    # base Python, not to the locked venv that created it.
    version = 'python' + str(sys.version_info.major) + '.' + str(sys.version_info.minor)
    site = work / 'package-env/lib' / version / 'site-packages'
    (site / 'locked_dependencies.pth').write_text(str(Path(sys.prefix) / 'lib' / version / 'site-packages') + '\n')
    subprocess.run([uv, 'pip', 'install', '--offline', '--no-deps', '--python', str(work / 'package-env/bin/python'), str(wheel)], check=True)
    os.execve(str(work / 'package-env/bin/python'), [str(work / 'package-env/bin/python'), str(snapshot / 'worker.py'), str(snapshot), str(work)],
              {**os.environ, 'GUS_WHEEL_READY': '1'})
environment = {'python': sys.version, 'platform': platform.platform(),
               'machine': platform.machine(), 'uv': subprocess.check_output([uv, '--version'], text=True).strip(),
               'packages': sorted([{'name': d.metadata['Name'], 'version': d.version}
                                   for d in importlib.metadata.distributions()], key=lambda d: d['name'])}
log = Tail()
with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
    if not request.get('wheel'):
        sys.path.insert(0, str(snapshot / 'code/src'))
    data = {s['id']: (pd.read_csv(s['path']) if s['suffix'] == '.csv' else pd.read_parquet(s['path']))
            for s in request['sources']}
    analyze = importlib.import_module('project_analysis.' + request['analysis_id']).analyze
    result = analyze(data, request['parameters'])
    # DataFrame contract checks use new values and a different row count. These
    # are evidence of basic portability, not proof of scientific validity.
    generality = None
    if request.get('check_generality'):
        import numpy as np
        rng = np.random.default_rng(42)
        synthetic = {}
        for key, frame in data.items():
            synthetic[key] = pd.DataFrame({column: (rng.normal(size=11) if pd.api.types.is_numeric_dtype(dtype)
                else ['synthetic_' + str(i % 3) for i in range(11)]) for column, dtype in frame.dtypes.items()})
        other = analyze(synthetic, request['parameters'])
        json.dumps(other, allow_nan=False)
        generality = {'passed': True, 'fixture': 'independent seeded synthetic values, 11 rows',
                      'scope': 'same declared column schema'}
    wheel_result = None
    if request.get('build'):
        import shutil
        shutil.copytree(snapshot / 'code', work / 'build/code')
        shutil.copy(snapshot / 'pyproject.toml', work / 'build/pyproject.toml')
        built = subprocess.run([uv, 'build', '--wheel', '--python', sys.executable, '--no-build-isolation', '--offline', '--out-dir', str(work / 'dist')],
                               cwd=str(work / 'build'), capture_output=True, text=True, env={**os.environ, 'UV_NO_BUILD': '0'})
        log.write(built.stdout + built.stderr)
        if built.returncode:
            raise RuntimeError(built.stdout + built.stderr)
        wheel = next(Path(str(work / 'dist')).glob('*.whl'))
        wheel_result = {'name': wheel.name, 'data': base64.b64encode(wheel.read_bytes()).decode()}
print('GUS_RESULT:' + json.dumps({'result': result, 'environment': environment,
      'log': log.getvalue(), 'wheel': wheel_result, 'generality': generality}, allow_nan=False))
'''

class UvRunner:
    def __init__(self):
        self.uv = uv_binary()
        self.processes = set()
        self.guard = threading.Lock()
        self.closed = False

    def status(self):
        if not self.uv:
            return {"available": False, "message": "Install uv on the workspace host to prepare project environments."}
        try:
            return {"available": True, "message": self.command([self.uv, "--version"], timeout=8).strip()}
        except (ProjectError, OSError) as exc:
            return {"available": False, "message": str(exc)}

    @staticmethod
    def _stop(process):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def command(self, args, timeout=180, cancel=None, limit=10 * 1024 * 1024, cwd=None, env=None):
        # Do not pass app/agent credentials to analysis children. Host filesystem
        # access remains possible; this is deliberately not a security sandbox.
        child_env = {key: value for key, value in os.environ.items()
                     if key in {"PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR",
                                "SSL_CERT_FILE", "SSL_CERT_DIR", "UV_CACHE_DIR"}}
        child_env.update(env or {})
        with self.guard:
            if self.closed:
                raise ProjectError("Project runner is shutting down")
            process = subprocess.Popen(args, cwd=cwd, env=child_env, start_new_session=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.processes.add(process)
        chunks, count, overflow = [], [0], threading.Event()

        def drain():
            while True:
                data = process.stdout.read(8192)
                if not data:
                    break
                count[0] += len(data)
                if count[0] <= limit:
                    chunks.append(data)
                else:
                    overflow.set()
                    self._stop(process)
                    break

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None or reader.is_alive():
                if overflow.is_set():
                    raise ProjectError("Worker output exceeded 10 MB; reduce result size")
                if self.closed or (cancel and cancel.is_set()) or time.monotonic() > deadline:
                    raise ProjectError("Operation cancelled" if self.closed or (cancel and cancel.is_set()) else "Operation timed out")
                time.sleep(.05)
            output = b"".join(chunks).decode(errors="replace")
            if process.returncode:
                raise ProjectError(output[-12000:] or "Project command failed")
            return output
        finally:
            # The process group also includes Python/uv children created by a run.
            self._stop(process)
            process.wait()
            reader.join(timeout=3)
            process.stdout.close()
            with self.guard:
                self.processes.discard(process)

    def prepare(self, directory, specification, lock=None, cancel=None):
        status = self.status()
        if not status["available"]:
            raise ProjectError(status["message"])
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        compact = (directory / "code/pyproject.toml").is_file()
        definitions = directory / "code" if compact else directory
        write_file(definitions / "pyproject.toml", specification)
        if lock:
            write_file(definitions / "uv.lock", lock)
        venv = directory / ("data/environment/.venv" if compact else "environment/.venv")
        env = {"UV_PROJECT_ENVIRONMENT": str(venv), "UV_NO_BUILD": "1"}
        if not lock:
            self.command([self.uv, "lock", "--python", "3.12"], cwd=definitions, env=env, timeout=900, cancel=cancel)
        self.command([self.uv, "sync", "--locked", "--no-install-project", "--python", "3.12"],
                     cwd=definitions, env=env, timeout=900, cancel=cancel)
        python = str(venv / "bin/python")
        version = self.command([python, "--version"]).strip()
        return {"runner": "uv", "path": str(venv), "python": python, "python_version": version,
                "definitions": str(definitions), "uv": status["message"]}, read_file(definitions / "uv.lock")

    def execute(self, environment, snapshot, sources, cancel=None):
        if environment.get("runner") != "uv":
            raise ProjectError("Prepare the uv environment before running this project. Older container runs cannot be replayed with uv.")
        root = Path(environment.get("definitions") or Path(environment["path"]).parent.parent)
        if digest(read_file(root / "uv.lock")) != environment["lock_sha256"]:
            raise ProjectError("Prepared lockfile changed; restore it before replaying")
        # Reconcile additions/removals made from the terminal with the saved lock.
        self.command([self.uv, "sync", "--locked", "--no-install-project", "--python", environment["python"]],
                     cwd=root, env={"UV_PROJECT_ENVIRONMENT": environment["path"], "UV_NO_BUILD": "1"},
                     timeout=900, cancel=cancel)
        if self.command([environment["python"], "--version"]).strip() != environment["python_version"]:
            raise ProjectError("Python version changed; prepare the environment again")
        with tempfile.TemporaryDirectory(prefix="gusnb-analysis-") as temporary:
            output = self.command([environment["python"], str(Path(snapshot) / "worker.py"), str(snapshot), temporary],
                cwd=temporary, cancel=cancel, env={"GUS_PROJECT_UV": self.uv, "PYTHONDONTWRITEBYTECODE": "1",
                "VIRTUAL_ENV": environment["path"], "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"})
        marker = "GUS_RESULT:"
        lines = [line[len(marker):] for line in output.splitlines() if line.startswith(marker)]
        if len(lines) != 1:
            raise ProjectError("Worker did not produce one complete result")
        return json.loads(lines[0])

    def close(self):
        with self.guard:
            self.closed = True
            for process in self.processes:
                self._stop(process)
