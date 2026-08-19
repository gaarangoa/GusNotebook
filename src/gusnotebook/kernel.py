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
from pathlib import Path

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
        self._run_state_lock = threading.Lock()
        self._interrupt_requested = threading.Event()
        self._prepared_run = None
        self._run_prepared = False
        self._kernel_cell_busy = False
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

    def _venv_env(self):
        """Isolated environment for the kernel's venv.

        When self.python is inside a venv, strip out everything that would let
        a shell command escape to the system Python: remove PYTHONPATH and
        PYTHONHOME (which can redirect imports outside the venv), and rebuild
        PATH with only the venv's bin/ plus non-Python system dirs — so `pip`,
        `python` etc. always resolve to the venv and nothing else.
        """
        bin_dir = Path(self.python).parent
        venv_dir = bin_dir.parent
        if not (venv_dir / "pyvenv.cfg").exists():
            return {}

        # Rebuild PATH: venv bin first, then every entry that isn't another
        # Python bin dir (site-packages parent, /usr/bin with a python, etc.).
        # We keep non-Python system dirs so tools like git, curl, etc. still work.
        clean_path_entries = [str(bin_dir)]
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            if not entry or entry == str(bin_dir):
                continue
            p = Path(entry)
            # Drop any bin that contains a `python` or `python3` executable —
            # those are other Python installations that would shadow the venv.
            if (p / "python").exists() or (p / "python3").exists():
                continue
            clean_path_entries.append(entry)

        env = {
            "PATH": os.pathsep.join(clean_path_entries),
            "VIRTUAL_ENV": str(venv_dir),
        }
        # These would let imports escape the venv entirely.
        env["PYTHONPATH"] = ""
        env["PYTHONHOME"] = ""
        return env

    def start(self):
        with self._lock:
            if self._km is not None:
                return
            self._set_status("starting")
            km = KernelManager(kernel_name="python3")
            km._kernel_spec = self._spec_for(km)
            # Pass the full env with the venv's bin/ at the front of PATH so
            # !pip, !python etc. in cells resolve to the kernel's own venv.
            env = {**os.environ, **self._venv_env()}
            km.start_kernel(cwd=self.cwd, env=env)
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

    def prepare_execution(self, run_id=None):
        """Mark a browser run as pending before kernel startup begins.

        The HTTP Stop request can overtake the Run request while the latter is
        starting a kernel.  Preparing under the app's run-control lock gives
        ``interrupt`` an execution to target throughout that window.
        """
        with self._run_state_lock:
            self._interrupt_requested.clear()
            self._prepared_run = run_id
            self._run_prepared = True
            self._kernel_cell_busy = False

    def cancel_prepared_execution(self, run_id=None):
        """Forget a prepared run which was cancelled before it reached IPython."""
        with self._run_state_lock:
            if run_id is None or self._prepared_run == run_id:
                self._interrupt_requested.clear()
                self._prepared_run = None
                self._run_prepared = False
                self._kernel_cell_busy = False

    def interrupt(self, run_id=None):
        """Interrupt the active execution without waiting for its run lock.

        ``execute`` deliberately holds ``_lock`` until a cell is done so two
        runs cannot consume each other's messages. Acquiring it here made Stop
        wait behind the very execution it was meant to interrupt. KernelManager
        sends SIGINT to the kernel process group, so it is both safe and
        necessary to take this path concurrently with ``execute``.
        """
        with self._run_state_lock:
            # A token prevents a delayed Stop response from interrupting a
            # different cell which happened to start in the meantime.
            if run_id is not None and self._run_prepared \
                    and self._prepared_run != run_id:
                return False
            self._interrupt_requested.set()
            # Before the target cell reports `busy`, SIGINT can land on an idle
            # kernel (or on startup/bootstrap) and be consumed before the code
            # begins. The execute loop re-delivers it on that cell's busy event.
            km = self._km if self._kernel_cell_busy else None
        if km is not None:
            km.interrupt_kernel()
        return True

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
        # API callers prepare before entering this method so Stop can cover the
        # kernel-start window. Direct callers still get a clean execution state.
        with self._run_state_lock:
            if not self._run_prepared:
                self._interrupt_requested.clear()
                self._prepared_run = None
                self._run_prepared = True
        if not self.is_alive():
            self.start()

        with self._lock:
            kc = self._kc
            if kc is None:
                raise RuntimeError("kernel is not running")

            outputs = []
            exec_count = None

            # Stop arrived during kernel startup. Do not send the cell merely
            # to interrupt it a moment later; report the cancellation directly.
            if self._interrupt_requested.is_set():
                outputs.append({
                    "output_type": "error",
                    "ename": "KeyboardInterrupt",
                    "evalue": "execution stopped before the kernel was ready",
                    "traceback": [],
                })
                with self._run_state_lock:
                    self._interrupt_requested.clear()
                    self._prepared_run = None
                    self._run_prepared = False
                    self._kernel_cell_busy = False
                self._set_status("idle" if self.is_alive() else "dead")
                # Status first so the UI has left its temporary "stopping"
                # state by the time it displays the cancellation.
                if on_output:
                    on_output([dict(o) for o in outputs])
                return None, outputs

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
                        state = content["execution_state"]
                        if state == "busy":
                            with self._run_state_lock:
                                self._kernel_cell_busy = True
                                interrupt_now = self._interrupt_requested.is_set()
                                km = self._km
                            if interrupt_now and km is not None:
                                km.interrupt_kernel()
                        elif state == "idle":
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
                with self._run_state_lock:
                    self._interrupt_requested.clear()
                    self._prepared_run = None
                    self._run_prepared = False
                    self._kernel_cell_busy = False
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
