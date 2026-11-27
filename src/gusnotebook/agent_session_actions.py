"""Native session mutations; no chat turns or model requests are started here."""

import json
import os
import selectors
import shutil
import signal
import subprocess
import time


class SessionActionError(ValueError):
    pass


def codex_request(root, method, params, *, timeout=20):
    """One local app-server request, following its initialize/initialized handshake.

    A short-lived stdio connection lets Codex own its indexes and rollout format.
    No daemon, open network port, or model session is needed. Output is bounded.
    """
    executable = shutil.which('codex')
    if not executable:
        raise SessionActionError('Install Codex CLI on this computer to manage its saved sessions.')
    if method not in {'thread/name/set', 'thread/delete'}:
        raise SessionActionError('Unsupported session action.')
    process = subprocess.Popen([executable, 'app-server'], cwd=root,
                               env={**os.environ, 'CODEX_HOME': str(root)},
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True)
    deadline = time.monotonic() + timeout
    buffer = bytearray()
    received = 0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)

            def send(value):
                data = (json.dumps(value) + '\n').encode()
                # FileIO is unbuffered; a write may be short.
                while data:
                    written = process.stdin.write(data)
                    if not written:
                        raise SessionActionError('Codex closed its session-management connection.')
                    data = data[written:]

            def receive(request_id):
                nonlocal received
                while True:
                    while b'\n' in buffer:
                        line, _, rest = buffer.partition(b'\n')
                        buffer[:] = rest
                        try:
                            response = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(response, dict) or response.get('id') != request_id:
                            continue
                        if 'error' in response:
                            details = response.get('error')
                            message = str(details.get('message', 'Session action failed.') if isinstance(details, dict) else details)
                            raise SessionActionError('Codex: ' + message[:600])
                        if 'result' in response:
                            return response['result']
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise SessionActionError('Codex took too long to respond. Refresh the session list before retrying.')
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        raise SessionActionError('Codex could not manage this session. Check or update the Codex CLI and retry.')
                    received += len(chunk)
                    if received > 2 * 1024 * 1024:
                        raise SessionActionError('Codex returned too much data while managing this session.')
                    buffer.extend(chunk)

            send({'id': 1, 'method': 'initialize', 'params': {
                'clientInfo': {'name': 'gusnotebook', 'title': 'GusNotebook', 'version': '0.1.0'}}})
            receive(1)
            send({'method': 'initialized', 'params': {}})
            send({'id': 2, 'method': method, 'params': params})
            return receive(2)
    except (BrokenPipeError, OSError) as exc:
        raise SessionActionError('Could not communicate with Codex. Check its CLI installation and retry.') from exc
    finally:
        process.stdin.close()
        process.stdout.close()
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
