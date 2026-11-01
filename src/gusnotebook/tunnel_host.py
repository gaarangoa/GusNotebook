"""Recover the hosted transport without stopping the workspace it exposes."""

import logging
import os
import signal
import subprocess
import threading
import time

from .tunnels import DevTunnels, TunnelError


logger = logging.getLogger(__name__)


def _stop(process):
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


class TunnelHost:
    """Own the CLI children, renew metadata, and retry failed hosting sessions."""

    def __init__(self, command, name, port, renew_interval=6 * 60 * 60,
                 retry_delay=5, max_retry_delay=60):
        self.name, self.port = name, port
        self.renew_interval = renew_interval
        self.retry_delay, self.max_retry_delay = retry_delay, max_retry_delay
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.cli = DevTunnels(command, runner=self._run_cli)
        self.children = set()
        self.process = None
        self.thread = None
        self.requested = False
        self.state, self.error = "starting", None
        self.renew_at = 0
        self.connected_at = 0

    def _run_cli(self, command, timeout=45, **_kwargs):
        # Management calls must also be cancellable during application shutdown.
        with self.lock:
            if self.stop.is_set():
                raise TunnelError("Tunnel host is stopping")
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            self.children.add(process)
        deadline = time.monotonic() + timeout
        try:
            while not self.stop.is_set():
                if time.monotonic() >= deadline:
                    raise TunnelError("Dev Tunnels did not respond in time")
                try:
                    output = process.communicate(timeout=.2)[0].decode("utf-8", errors="replace")
                    return subprocess.CompletedProcess(command, process.returncode, stdout=output, stderr=output)
                except subprocess.TimeoutExpired:
                    pass
            raise TunnelError("Tunnel host is stopping")
        finally:
            _stop(process)
            process.stdout.close()
            with self.lock:
                self.children.discard(process)

    def snapshot(self):
        with self.lock:
            return {"name": self.name, "state": self.state, "error": self.error}

    def _status(self, state, error=None):
        with self.lock:
            self.state, self.error = state, error

    def _launch(self, initial=False):
        if not initial:
            # prepare checks ownership/privacy and recreates a deleted tunnel.
            # A stale or competing host is retried, never forcibly displaced.
            self.name = self.cli.prepare(self.name, self.port)
        self.cli.renew(self.name, self.port)
        with self.lock:
            if self.stop.is_set():
                raise TunnelError("Tunnel host is stopping")
            self.process = self.cli.host(self.name)
            process = self.process
        process.wait_ready()
        self.connected_at = time.monotonic()
        self.renew_at = self.connected_at + self.renew_interval
        self._status("connected")

    def start(self):
        self._launch(initial=True)
        self.thread = threading.Thread(target=self._watch, name="gusnb-tunnel-host", daemon=True)
        self.thread.start()

    def request_renew(self):
        with self.lock:
            if self.stop.is_set():
                raise TunnelError("Tunnel host is stopping")
            if self.state not in {"starting", "reconnecting"}:
                self.requested = True
                self.state = "reconnecting"
                self.wake.set()
            return self.snapshot()

    def _release(self):
        with self.lock:
            process, self.process = self.process, None
        if process:
            process.close()

    def _report_error(self, error, state):
        message = str(error)
        if message != self.error:
            logger.warning(f"[tunnel] {message}\n[tunnel] Retrying automatically; the workspace is still running. "
                  "If sign-in expired, sign in again with the same GitHub/Microsoft account on the remote computer, "
                  f"then run PORT={self.port} gusnb tunnel renew.")
        self._status(state, message)

    def _watch(self):
        delay = self.retry_delay
        try:
            while not self.stop.is_set():
                with self.lock:
                    requested, self.requested = self.requested, False
                    process = self.process
                if requested or process is None or process.done.is_set() or process.expired.is_set():
                    stable = process and time.monotonic() - self.connected_at >= 60
                    if stable:
                        delay = self.retry_delay
                    self._status("reconnecting", self.error)
                    self._release()
                    logger.info("[tunnel] Reconnecting the tunnel; notebooks, kernels and terminals stay running.")
                    if process and not requested:
                        self.wake.wait(delay)
                        self.wake.clear()
                        if self.stop.is_set():
                            break
                        if not stable:
                            delay = min(delay * 2, self.max_retry_delay)
                    try:
                        self._launch()
                        logger.info("[tunnel] Reconnected: %s", self.name)
                    except (TunnelError, OSError) as error:
                        self._release()
                        if self.stop.is_set():
                            break
                        self._report_error(error, "retrying")
                        self.wake.wait(delay)
                        self.wake.clear()
                        delay = min(delay * 2, self.max_retry_delay)
                    continue
                if time.monotonic() - self.connected_at >= 60:
                    delay = self.retry_delay
                if time.monotonic() >= self.renew_at:
                    try:
                        self.cli.renew(self.name, self.port)
                        self.renew_at = time.monotonic() + self.renew_interval
                        self._status("connected")
                    except (TunnelError, OSError) as error:
                        if self.stop.is_set():
                            break
                        # A management outage must not interrupt a live transport.
                        self.renew_at = time.monotonic() + self.max_retry_delay
                        self._report_error(error, "connected")
                self.wake.wait(.25)
                self.wake.clear()
        finally:
            self._release()
            self._status("stopped")

    def close(self):
        with self.lock:
            self.stop.set()
            self.wake.set()
            process = self.process
            children = list(self.children)
        if process:
            process.close()
        for child in children:
            _stop(child)
        if self.thread:
            self.thread.join(timeout=10)
        self._status("stopped")
