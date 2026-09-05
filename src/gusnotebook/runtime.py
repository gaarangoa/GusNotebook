"""Resources owned by one application, with explicit startup and shutdown."""

import threading
import time

from . import bus, notebook, paths, preview, sessions, terminals, textfile
from .history import History
from .environments import EnvironmentManager
from .kernel import KernelPool


class Runtime:
    def __init__(self):
        self.bus = bus.Bus()
        self.notebooks = notebook.Registry()
        self.texts = textfile.TextRegistry()
        self.previews = preview.PreviewPool()
        self.kernels = KernelPool()
        self.terms = terminals.SessionPool()
        self.store = sessions.SessionStore()
        self.exec_locks = {}
        self.exec_locks_guard = threading.RLock()
        self.focuses = {}
        self.focus_guard = threading.Lock()
        self.markup_focuses = {}
        self.markup_focus_serial = 0
        self.prompts = {}
        self.run_control_lock = threading.Lock()
        self.cancelled_runs = {}
        self.running_cells = {}
        self.settings_memory = {}
        self.history = History(paths.state("history"))
        self.environments = EnvironmentManager(paths.state("environments.json"))
        self.stop = threading.Event()
        self.watcher = None
        self.workers = set()
        self.workers_lock = threading.Lock()

    def start(self):
        if self.watcher is None:
            self.watcher = notebook.watch(self.notebooks, stop=self.stop,
                                         publish=self.bus.publish)

    def close(self):
        self.stop.set()
        self.environments.close()
        if self.watcher:
            self.watcher.join(timeout=2)
        # Interrupt first: shutdown takes the execution lock and must not wait
        # behind an unbounded user computation.
        for key in list(self.kernels.info()):
            kernel = self.kernels.peek(key)
            if kernel:
                kernel.interrupt()
        self.terms.close_all()
        self.previews.close_all()
        self.kernels.shutdown_all()
        deadline = time.monotonic() + 3
        with self.workers_lock:
            workers = list(self.workers)
        for worker in workers:
            worker.join(timeout=max(0, deadline - time.monotonic()))
        self.history.close()
