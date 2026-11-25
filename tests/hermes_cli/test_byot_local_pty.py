"""Real local PTY behavior for the BYOT client."""
import io
import os
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from hermes_cli.byot_bridge import _Relay, _spawn_pty


@pytest.mark.platforms("posix")
def test_ctrl_c_from_viewer_interrupts_pty_child():
    ready = threading.Event()
    received = bytearray()
    deadline = time.monotonic() + 10

    class Socket:
        sent = False
        def send(self, data):
            if isinstance(data, bytes):
                received.extend(data)
                if b"READY" in received:
                    ready.set()
        def recv(self, timeout):
            assert time.monotonic() < deadline, "PTY did not finish"
            if not self.sent and ready.wait(timeout=timeout):
                self.sent = True
                return b"\x03"
            raise TimeoutError()
        def close(self):
            pass

    program = "import sys; print('READY', flush=True); exec(\"try:\\n input()\\nexcept KeyboardInterrupt:\\n print('INTERRUPTED', flush=True)\")"
    child = _spawn_pty([sys.executable, "-u", "-c", program], 80, 24, dict(os.environ))
    result = _Relay(Socket(), child, None, SimpleNamespace(buffer=io.BytesIO())).run()
    assert result == 0
    assert b"INTERRUPTED" in received


@pytest.mark.platforms("posix")
def test_dashboard_disconnect_does_not_end_local_shell(capsys):
    from websockets.exceptions import ConnectionClosed

    ready, disconnected = threading.Event(), threading.Event()
    output = bytearray()
    deadline = time.monotonic() + 10

    class Socket:
        def send(self, data):
            if isinstance(data, bytes):
                output.extend(data)
                if b"READY" in output:
                    ready.set()
        def recv(self, timeout):
            assert time.monotonic() < deadline, "PTY did not finish"
            if ready.wait(timeout=timeout):
                disconnected.set()
                raise ConnectionClosed(None, None)
            raise TimeoutError()
        def close(self):
            pass

    program = "print('READY', flush=True); input(); print('STILL RUNNING', flush=True)"
    child = _spawn_pty([sys.executable, "-u", "-c", program], 80, 24, dict(os.environ))
    def local_input():
        if disconnected.wait(timeout=5):
            os.write(child.fd, b"continue\n")
    writer = threading.Thread(target=local_input, daemon=True)
    writer.start()
    local_output = io.BytesIO()
    result = _Relay(Socket(), child, None, SimpleNamespace(buffer=local_output)).run()
    writer.join(timeout=5)
    assert result == 0
    assert b"STILL RUNNING" in local_output.getvalue()
    assert "local terminal keeps running" in capsys.readouterr().err
