"""Closing a terminal must not race its reader, input, or resize operations."""

import errno
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from gusnotebook import terminals


class TerminalIOTests(unittest.TestCase):
    def setUp(self):
        self.session = terminals.Session('race-test', Path.cwd(), command=['/bin/sh'])
        self.session._publish = Mock()
        self.fd, self.writer = os.pipe()
        os.set_blocking(self.fd, False)
        self.session.fd = self.fd
        self.session.alive = True

    def tearDown(self):
        self.session.close()
        os.close(self.writer)

    def test_close_between_select_and_read_never_reads_a_released_descriptor(self):
        replacement = None

        def ready(*_args):
            nonlocal replacement
            self.session.close()
            # open normally reuses the released integer. Never force reuse:
            # another thread may already own it by the time open returns.
            replacement = os.open(os.devnull, os.O_RDONLY)
            return [self.fd], [], []

        try:
            with patch.object(terminals.select, 'select', side_effect=ready), \
                    patch.object(terminals.os, 'read') as read:
                self.session._read_loop()
            read.assert_not_called()
            self.assertIsNone(self.session.fd)
            self.assertFalse(self.session.alive)
            os.fstat(replacement)  # cleanup must not close another fd's owner
            self.session._publish.assert_called_once()
        finally:
            if replacement is not None:
                os.close(replacement)

    def test_transient_read_errors_retry_and_eof_notifies_clients(self):
        listener = queue.Queue()
        self.session.attach(listener)
        with patch.object(terminals.select, 'select', side_effect=[
                    InterruptedError(errno.EINTR, 'interrupted')] + [([self.fd], [], [])] * 4), \
                patch.object(terminals.os, 'read', side_effect=[
                    InterruptedError(errno.EINTR, 'interrupted'),
                    BlockingIOError(errno.EAGAIN, 'try again'), b'output', b'']):
            self.session._read_loop()
        self.assertEqual(listener.get_nowait(), b'output')
        self.assertIn(b'exited', listener.get_nowait())
        self.assertIsNone(listener.get_nowait())
        self.assertIsNone(self.session.fd)
        with self.assertRaises(OSError):
            os.fstat(self.fd)

    def test_cleanup_error_is_contained_and_retried(self):
        temporary = Mock()
        temporary.cleanup.side_effect = [OSError(errno.ENOTEMPTY, 'shell is saving history'), None]
        self.session._shell_startup = temporary
        self.session.close()
        self.assertIs(self.session._shell_startup, temporary)
        self.session.close()
        self.assertIsNone(self.session._shell_startup)
        self.assertEqual(temporary.cleanup.call_count, 2)

    def test_partial_writes_and_backpressure_preserve_the_whole_input(self):
        accepted = bytearray()
        results = iter([BlockingIOError(errno.EAGAIN, 'full'), 2,
                        InterruptedError(errno.EINTR, 'interrupted'), 4])

        def write(_fd, data):
            result = next(results)
            if isinstance(result, Exception):
                raise result
            accepted.extend(data[:result])
            return result

        with patch.object(terminals.os, 'write', side_effect=write), \
                patch.object(terminals.select, 'select', return_value=([], [self.fd], [])):
            self.session.write('abcdef')
        self.assertEqual(accepted, b'abcdef')

    def test_backpressured_write_can_close_and_later_input_resize_are_noops(self):
        def writable(*_args):
            self.session.close()
            return [], [self.fd], []

        with patch.object(terminals.os, 'write', side_effect=BlockingIOError(errno.EAGAIN, 'full')) as write, \
                patch.object(terminals.select, 'select', side_effect=writable), \
                patch.object(terminals.fcntl, 'ioctl') as resize:
            self.session.write('waiting')
            self.session.write('late input')
            self.session.resize(40, 100)
            self.session.close()
        write.assert_called_once()
        resize.assert_not_called()


class RealTerminalLifecycleTests(unittest.TestCase):
    def test_close_while_real_pty_is_full_and_resizing_does_not_crash_or_hang(self):
        errors = []
        command = [sys.executable, '-c',
                   "import os, time, tty; tty.setraw(0); os.write(1,b'READY'); time.sleep(30)"]
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(terminals, 'SHELL_COMMAND', command), \
                patch('threading.excepthook', side_effect=lambda args: errors.append(args.exc_value)):
            session = terminals.Session('real-close', temporary, command=command).start()
            stop = threading.Event()
            writer = threading.Thread(target=lambda: session.write(b'x' * (1024 * 1024)), daemon=True)

            def resize():
                while not stop.wait(.005):
                    session.resize(40, 100)

            resizer = threading.Thread(target=resize, daemon=True)
            try:
                deadline = time.monotonic() + 5
                while b'READY' not in session.scrollback() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertIn(b'READY', session.scrollback())
                writer.start()
                resizer.start()
                time.sleep(.1)
                self.assertTrue(writer.is_alive(), 'Input should be waiting for the child to read')
                session.close()
                writer.join(timeout=2)
                session._reader.join(timeout=2)
                self.assertFalse(writer.is_alive())
                self.assertFalse(session._reader.is_alive())
                self.assertIsNone(session.fd)
                self.assertFalse(session.alive)
            finally:
                stop.set()
                session.close()
                if resizer.ident:
                    resizer.join(timeout=2)
                if writer.ident:
                    writer.join(timeout=2)
                session._reader.join(timeout=2)
                try:
                    os.waitpid(session.pid, 0)
                except ChildProcessError:
                    pass
            self.assertEqual(errors, [])

    def test_natural_exit_drains_output_and_releases_the_pty(self):
        command = [sys.executable, '-c', "print('FINAL_OUTPUT', flush=True)"]
        with tempfile.TemporaryDirectory() as temporary, patch.object(terminals, 'SHELL_COMMAND', command):
            session = terminals.Session('normal-exit', temporary, command=command).start()
            try:
                session._reader.join(timeout=5)
                self.assertFalse(session._reader.is_alive())
                self.assertIn(b'FINAL_OUTPUT', session.scrollback())
                self.assertIn(b'exited', session.scrollback())
                self.assertIsNone(session.fd)
            finally:
                session.close()
                try:
                    os.waitpid(session.pid, 0)
                except ChildProcessError:
                    pass


if __name__ == '__main__':
    unittest.main()
