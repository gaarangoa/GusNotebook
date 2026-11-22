"""Linux-container runner. No host execution fallback and no credentials in workers."""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid

from .project_files import ProjectError, encoded, read_file, write_file

BASE_IMAGE = "ghcr.io/astral-sh/uv:python3.12-bookworm-slim"
PYTHON = "/opt/project/environment/.venv/bin/python"

# This bootstrap, the Dockerfile and dependency declarations are broker-owned.
# stdout is the only output channel. /tmp is size-limited; there are no writable
# host mounts. A worker cannot write a receipt, approval, policy or report.
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

request = json.loads(Path('/analysis/request.json').read_text())
if request.get('wheel') and not os.environ.get('GUS_WHEEL_READY'):
    wheel = Path('/tmp') / request['wheel']['name']
    wheel.write_bytes(base64.b64decode(request['wheel']['data']))
    subprocess.run(['uv', 'venv', '--python', sys.executable, '/tmp/package-env'], check=True)
    # The clean package environment reuses only the immutable dependency layer,
    # never the analysis source tree. A venv's system-site-packages points to
    # base Python, not to the locked venv that created it.
    version = 'python' + str(sys.version_info.major) + '.' + str(sys.version_info.minor)
    site = Path('/tmp/package-env/lib') / version / 'site-packages'
    (site / 'locked_dependencies.pth').write_text(str(Path(sys.prefix) / 'lib' / version / 'site-packages') + '\n')
    subprocess.run(['uv', 'pip', 'install', '--offline', '--no-deps', '--python', '/tmp/package-env/bin/python', str(wheel)], check=True)
    os.execve('/tmp/package-env/bin/python', ['/tmp/package-env/bin/python', '/analysis/worker.py'],
              {**os.environ, 'GUS_WHEEL_READY': '1'})
environment = {'python': sys.version, 'platform': platform.platform(),
               'machine': platform.machine(), 'uv': subprocess.check_output(['uv', '--version'], text=True).strip(),
               'packages': sorted([{'name': d.metadata['Name'], 'version': d.version}
                                   for d in importlib.metadata.distributions()], key=lambda d: d['name'])}
log = Tail()
with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
    if not request.get('wheel'):
        sys.path.insert(0, '/analysis/code/src')
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
        shutil.copytree('/analysis/code', '/tmp/build/code')
        shutil.copy('/analysis/pyproject.toml', '/tmp/build/pyproject.toml')
        built = subprocess.run(['uv', 'build', '--wheel', '--python', sys.executable, '--no-build-isolation', '--offline', '--out-dir', '/tmp/dist'],
                               cwd='/tmp/build', capture_output=True, text=True, env={**os.environ, 'UV_NO_BUILD': '0'})
        log.write(built.stdout + built.stderr)
        if built.returncode:
            raise RuntimeError(built.stdout + built.stderr)
        wheel = next(Path('/tmp/dist').glob('*.whl'))
        wheel_result = {'name': wheel.name, 'data': base64.b64encode(wheel.read_bytes()).decode()}
print('GUS_RESULT:' + json.dumps({'result': result, 'environment': environment,
      'log': log.getvalue(), 'wheel': wheel_result, 'generality': generality}, allow_nan=False))
'''


class ContainerRunner:
    def __init__(self):
        self.docker = shutil.which("docker")
        self.processes = set()
        self.containers = set()
        self.guard = threading.Lock()
        self.closed = False

    def status(self):
        if not self.docker:
            return {"available": False, "message": "Install and start Docker on the workspace host to run restricted analyses."}
        try:
            self._local_engine()
            version = self.command([self.docker, "version", "--format", "{{.Server.Version}}"], timeout=8)
            return {"available": True, "message": "Docker " + version.strip()}
        except ProjectError as exc:
            if "local Docker" in str(exc):
                return {"available": False, "message": str(exc)}
            return {"available": False, "message": "Start Docker on the workspace host. Restricted runs never fall back to a host kernel."}
        except OSError:
            return {"available": False, "message": "Start Docker on the workspace host. Restricted runs never fall back to a host kernel."}

    def _local_engine(self):
        endpoint = os.environ.get("DOCKER_HOST") if not os.environ.get("DOCKER_CONTEXT") else None
        endpoint = endpoint or self.command([self.docker or "docker", "context", "inspect", "--format",
                                            "{{.Endpoints.docker.Host}}"], timeout=8).strip()
        if not endpoint.startswith("unix://"):
            raise ProjectError("Select a local Docker context on the workspace host; remote Docker bind mounts cannot verify approved file identity")

    def command(self, args, timeout=180, cancel=None, limit=10 * 1024 * 1024):
        if self.closed:
            raise ProjectError("Project runner is shutting down")
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        with self.guard:
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
                    process.kill()
                    break

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if self.closed or (cancel and cancel.is_set()) or time.monotonic() > deadline:
                    process.kill()
                    raise ProjectError("Operation cancelled" if self.closed or (cancel and cancel.is_set()) else "Operation timed out")
                time.sleep(.05)
            reader.join(timeout=3)
            output = b"".join(chunks).decode(errors="replace")
            if overflow.is_set():
                raise ProjectError("Worker output exceeded 10 MB; reduce result size")
            if process.returncode:
                raise ProjectError(output[-12000:] or "Container command failed")
            return output
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            reader.join(timeout=3)
            process.stdout.close()
            with self.guard:
                self.processes.discard(process)

    def prepare(self, directory, specification, lock=None, cancel=None):
        if not self.status()["available"]:
            raise ProjectError(self.status()["message"])
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        write_file(directory / "pyproject.toml", specification)
        if lock:
            write_file(directory / "uv.lock", lock)
        dockerfile = (f"FROM {BASE_IMAGE}\nWORKDIR /opt/project\n"
                      "ENV UV_PROJECT_ENVIRONMENT=/opt/project/environment/.venv UV_NO_BUILD=1 UV_LINK_MODE=copy\n"
                      "COPY pyproject.toml ./\n" + ("COPY uv.lock ./\n" if lock else "RUN uv lock\n") +
                      "RUN uv sync --locked --no-install-project\n")
        write_file(directory / "Dockerfile", dockerfile)
        image_file = directory / "image-id"
        self.command([self.docker, "build", "--iidfile", str(image_file), str(directory)], timeout=900, cancel=cancel)
        image = read_file(image_file).decode().strip()
        if not image.startswith("sha256:"):
            raise ProjectError("Docker did not return an immutable image ID")
        lock = self.command([self.docker, "run", "--rm", "--network=none", image,
                             "cat", "/opt/project/uv.lock"], cancel=cancel).encode()
        return {"image": image, "base": BASE_IMAGE}, lock

    def execute(self, image, snapshot, sources, cancel=None):
        self._local_engine()
        name = "gusnb-analysis-" + uuid.uuid4().hex
        args = [self.docker or "docker", "run", "--rm", "--name", name,
                "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--pids-limit=128", "--memory=2g", "--cpus=2", "--ulimit", "nofile=256:256",
                "--user", f"{os.getuid() or 65534}:{os.getgid() or 65534}",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m,mode=1777", "--workdir", "/tmp",
                "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
                "--env", "OPENBLAS_NUM_THREADS=1", "--env", "OMP_NUM_THREADS=1"]
        for proxy in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "FTP_PROXY"):
            args += ["--env", proxy + "=", "--env", proxy.lower() + "="]
        mounts = [(Path(snapshot), "/analysis")] + [(Path(s["snapshot"]), s["path"]) for s in sources]
        for source, target in mounts:
            if "," in str(source):
                raise ProjectError("Docker bind paths cannot contain commas")
            args += ["--mount", f"type=bind,source={source},target={target},readonly"]
        args += [image, PYTHON, "/analysis/worker.py"]
        with self.guard:
            self.containers.add(name)
        try:
            output = self.command(args, timeout=180, cancel=cancel)
            marker = "GUS_RESULT:"
            lines = [line[len(marker):] for line in output.splitlines() if line.startswith(marker)]
            if len(lines) != 1:
                raise ProjectError("Worker did not produce one complete result")
            return json.loads(lines[0])
        finally:
            # Also cleans up a worker when its CLI was killed for timeout/cancel.
            subprocess.run([self.docker or "docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
            with self.guard:
                self.containers.discard(name)

    def close(self):
        self.closed = True
        with self.guard:
            processes, containers = list(self.processes), list(self.containers)
        for process in processes:
            if process.poll() is None:
                process.kill()
        for name in containers:
            try:
                subprocess.run([self.docker, "rm", "-f", name], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
