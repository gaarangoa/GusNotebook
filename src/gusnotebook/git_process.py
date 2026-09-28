"""Cancellable subprocesses shared by Git operations and authentication."""

import os
import re
import signal
import subprocess
import threading
import time


class GitError(RuntimeError):
    pass


def redact(text):
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", str(text))
    text = re.sub(r"(https?://)[^/\s@]+@", r"\1[credentials]@", text)
    return re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b", "[token]", text)


def stop(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
        except ProcessLookupError:
            pass


class Commands:
    def __init__(self):
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.children = set()

    def run(self, args, *, env, cwd=None, data=None, cancel=None, timeout=30, output=None):
        with self.lock:
            if self.closed.is_set() or (cancel and cancel.is_set()):
                raise GitError("Canceled")
            process = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            self.children.add(process)
        deadline = time.monotonic() + timeout
        readers = []
        chunks = {"stdout": [], "stderr": []}
        oversized = threading.Event()
        def read(stream, key):
            size = 0
            while True:
                chunk = stream.read1(4096)
                if not chunk:
                    return
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    oversized.set()
                else:
                    chunks[key].append(chunk)
                if output:
                    output(chunk.decode("utf-8", "replace"))
        try:
            if data:
                process.stdin.write(data.encode())
            process.stdin.close()
            for key in chunks:
                thread = threading.Thread(target=read, args=(getattr(process, key), key), daemon=True)
                thread.start()
                readers.append(thread)
            while process.poll() is None:
                if self.closed.wait(.05) or (cancel and cancel.is_set()):
                    raise GitError("Canceled")
                if oversized.is_set():
                    raise GitError("Command output is too large for the Git panel. Use the terminal for this operation.")
                if time.monotonic() > deadline:
                    raise GitError("Command timed out. Check authentication and your connection, then retry.")
            for thread in readers:
                thread.join(2)
            if oversized.is_set():
                raise GitError("Command output is too large for the Git panel. Use the terminal for this operation.")
            return subprocess.CompletedProcess(args, process.returncode,
                **{key: b"".join(value).decode("utf-8", "replace") for key, value in chunks.items()})
        finally:
            stop(process)
            for thread in readers:
                thread.join(2)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
            with self.lock:
                self.children.discard(process)

    def close(self):
        self.closed.set()
        with self.lock:
            children = list(self.children)
        for child in children:
            stop(child)
