"""IPython kernel management — one kernel per open notebook.

Each notebook gets its own Kernel (its own process and namespace), keyed by
path in a KernelPool. A Kernel can run on any interpreter: we override the
native kernelspec's argv[0], which jupyter_client would otherwise resolve to
this app's own sys.executable.
"""

import os
import queue
import sys
import threading
import time

from jupyter_client.manager import KernelManager

from . import bus

# Silent bootstrap run at kernel start: inline plots when matplotlib exists.
_BOOTSTRAP = (
    "try:\n"
    "    get_ipython().run_line_magic('matplotlib', 'inline')\n"
    "except Exception:\n"
    "    pass\n"
)


class Kernel:
    """A single long-lived IPython kernel with serialized execution.

    `python` selects the interpreter (and therefore the venv); `key` is the
    notebook this kernel belongs to, echoed on every status event so the right
    tab updates.
    """

    # How often streaming output is pushed to listeners, at most. Roughly a
    # screen refresh: fast enough that a progress bar still looks live, slow
    # enough that a tight print loop can't turn into one event per line. See
    # execute() for why this exists.
    STREAM_INTERVAL = 0.1

    def __init__(self, cwd, python=None, key=None):
        self.cwd = str(cwd)
        self.python = str(python or sys.executable)
        self.key = key
        self._km = None
        self._kc = None
        self._lock = threading.Lock()
        self.status = "stopped"
        self.execution_count = 0

    # --- lifecycle ---

    def _set_status(self, status):
        self.status = status
        bus.publish("kernel_status", status=status, notebook=self.key,
                    python=self.python)

    def _spec_for(self, km):
        """The native kernelspec with argv[0] pinned to our interpreter."""
        spec = km.kernel_spec
        argv = list(spec.argv)
        if argv and os.path.basename(argv[0]).startswith("python"):
            argv[0] = self.python
        else:
            argv = [self.python, "-m", "ipykernel_launcher",
                    "-f", "{connection_file}"]
        spec.argv = argv
        return spec

    def start(self):
        with self._lock:
            if self._km is not None:
                return
            self._set_status("starting")
            km = KernelManager(kernel_name="python3")
            km._kernel_spec = self._spec_for(km)
            km.start_kernel(cwd=self.cwd)
            kc = km.client()
            kc.start_channels()
            try:
                kc.wait_for_ready(timeout=60)
            except RuntimeError:
                kc.stop_channels()
                km.shutdown_kernel(now=True)
                self._km = self._kc = None
                self._set_status("dead")
                raise
            self._km, self._kc = km, kc
            self._set_status("idle")
        self._execute_silent(_BOOTSTRAP)

    def shutdown(self):
        with self._lock:
            if self._km is None:
                return
            try:
                self._kc.stop_channels()
                self._km.shutdown_kernel(now=True)
            except Exception:
                pass
            self._km = self._kc = None
            self.execution_count = 0
            self._set_status("stopped")

    def restart(self, python=None):
        """Restart, optionally switching to a different interpreter."""
        self.shutdown()
        if python:
            self.python = str(python)
        self.start()

    def interrupt(self):
        with self._lock:
            if self._km is not None:
                self._km.interrupt_kernel()

    def is_alive(self):
        return self._km is not None and self._km.is_alive()

    # --- execution ---

    def _drain_shell(self, kc, msg_id, timeout=1.0):
        """Pull the shell reply for msg_id so it doesn't leak into the next run."""
        try:
            while True:
                reply = kc.get_shell_msg(timeout=timeout)
                if reply["parent_header"].get("msg_id") == msg_id:
                    return reply
        except queue.Empty:
            return None

    def _execute_silent(self, code):
        if self._kc is None:
            return
        msg_id = self._kc.execute(code, store_history=False, silent=True)
        deadline = 15
        while True:
            try:
                msg = self._kc.get_iopub_msg(timeout=deadline)
            except queue.Empty:
                break
            if msg["parent_header"].get("msg_id") != msg_id:
                continue
            if msg["msg_type"] == "status" and msg["content"]["execution_state"] == "idle":
                break
        self._drain_shell(self._kc, msg_id)

    def execute(self, code, on_output=None, timeout=None):
        """Run code to completion. Returns (execution_count, [nbformat outputs]).

        on_output(outputs) is called with the full accumulated output list as it
        changes, for live streaming. Only one execute runs at a time.

        Calls are **coalesced** to STREAM_INTERVAL: a loop printing a line at a
        time produces a message per line, and forwarding each one meant an event
        per line, every event carrying the whole buffer so far — quadratic in
        both bytes sent and parsing done, which is what made a chatty cell hang
        the page. Text is still accumulated on every message; only the
        *notification* is rate-limited, so nothing is lost, it just arrives in
        batches. A screen refresh's worth of latency on a progress bar is
        imperceptible; the stall it replaces was not.

        Anything that isn't a stream (a result, an image, an error, a clear)
        notifies immediately: those are single discrete events the user is
        waiting to see, not a firehose.
        """
        if not self.is_alive():
            self.start()

        with self._lock:
            kc = self._kc
            if kc is None:
                raise RuntimeError("kernel is not running")

            outputs = []
            exec_count = None
            self._set_status("busy")
            msg_id = kc.execute(code, store_history=True)

            last_sent = [0.0]
            pending = [False]

            def notify(force=False):
                """Push the current outputs, at most every STREAM_INTERVAL."""
                if not on_output:
                    return
                now = time.monotonic()
                if not force and now - last_sent[0] < self.STREAM_INTERVAL:
                    pending[0] = True       # flushed by the next tick or at the end
                    return
                last_sent[0] = now
                pending[0] = False
                on_output([dict(o) for o in outputs])

            def emit(out, force=True):
                outputs.append(out)
                notify(force=force)

            try:
                while True:
                    try:
                        msg = kc.get_iopub_msg(timeout=timeout or 0.5)
                    except queue.Empty:
                        if not self.is_alive():
                            emit({
                                "output_type": "error",
                                "ename": "KernelDied",
                                "evalue": "the kernel stopped responding",
                                "traceback": [],
                            })
                            break
                        # A gap in output is exactly when a throttled update is
                        # owed: a cell that prints a burst then computes for a
                        # minute would otherwise sit showing stale text.
                        if pending[0]:
                            notify(force=True)
                        continue

                    if msg["parent_header"].get("msg_id") != msg_id:
                        continue

                    mtype, content = msg["msg_type"], msg["content"]

                    if mtype == "status":
                        if content["execution_state"] == "idle":
                            break
                    elif mtype == "execute_input":
                        exec_count = content.get("execution_count")
                    elif mtype == "stream":
                        # Coalesce consecutive writes to the same stream. Both
                        # paths are rate-limited, not just the append: code that
                        # alternates stdout and stderr starts a new output every
                        # message, and forcing on each would defeat the throttle
                        # for exactly the chattiest case.
                        if outputs and outputs[-1].get("output_type") == "stream" \
                                and outputs[-1].get("name") == content["name"]:
                            outputs[-1]["text"] += content["text"]
                            notify()
                        else:
                            emit({
                                "output_type": "stream",
                                "name": content["name"],
                                "text": content["text"],
                            }, force=False)
                    elif mtype == "execute_result":
                        exec_count = content.get("execution_count", exec_count)
                        emit({
                            "output_type": "execute_result",
                            "data": content.get("data", {}),
                            "metadata": content.get("metadata", {}),
                            "execution_count": exec_count,
                        })
                    elif mtype == "display_data":
                        emit({
                            "output_type": "display_data",
                            "data": content.get("data", {}),
                            "metadata": content.get("metadata", {}),
                        })
                    elif mtype == "error":
                        emit({
                            "output_type": "error",
                            "ename": content.get("ename", "Error"),
                            "evalue": content.get("evalue", ""),
                            "traceback": content.get("traceback", []),
                        })
                    elif mtype == "clear_output":
                        del outputs[:]
                        notify()
            finally:
                # Whatever the throttle held back, send now. Without this the
                # last batch of a run is dropped and the cell shows output that
                # stops short of what it actually printed.
                if pending[0]:
                    notify(force=True)
                self._drain_shell(kc, msg_id)
                self._set_status("idle" if self.is_alive() else "dead")

            if exec_count is not None:
                self.execution_count = exec_count
            return exec_count, outputs


class KernelPool:
    """One Kernel per notebook path, created on demand."""

    def __init__(self, default_python=None):
        self.default_python = str(default_python or sys.executable)
        self._kernels = {}
        self._lock = threading.Lock()

    def get(self, key, cwd, python=None):
        """The kernel for `key`, started lazily on first execute."""
        key = str(key)
        with self._lock:
            k = self._kernels.get(key)
            if k is None:
                k = Kernel(cwd=cwd, python=python or self.default_python, key=key)
                self._kernels[key] = k
            return k

    def peek(self, key):
        """The kernel for `key` if one exists — never creates one."""
        return self._kernels.get(str(key))

    def status(self, key):
        k = self.peek(key)
        return k.status if k else "stopped"

    def drop(self, key):
        """Shut down and forget one notebook's kernel (on tab close)."""
        with self._lock:
            k = self._kernels.pop(str(key), None)
        if k:
            k.shutdown()
        return k is not None

    def rename(self, old, new):
        """Follow a notebook that changed path, keeping its live namespace."""
        with self._lock:
            k = self._kernels.pop(str(old), None)
            if k is None:
                return False
            k.key = str(new)
            self._kernels[str(new)] = k
            return True

    def shutdown_all(self):
        with self._lock:
            kernels, self._kernels = list(self._kernels.values()), {}
        for k in kernels:
            k.shutdown()

    def info(self):
        return {
            key: {
                "status": k.status,
                "python": k.python,
                "alive": k.is_alive(),
                "execution_count": k.execution_count,
            }
            for key, k in self._kernels.items()
        }
