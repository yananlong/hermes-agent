"""Bridge a local POSIX terminal to the dashboard's BYOT WebSocket."""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import socket
import sys
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI
from websockets.sync.client import connect

from agent.memory_provider import spawn_context_thread
from hermes_cli.web_server_session_token import configured_session_token
from hermes_constants import get_hermes_home
from tools.environments.local import build_subprocess_env, served_profile_child_env

DEFAULT_DASHBOARD_URL = "http://127.0.0.1:9119"
_log = logging.getLogger(__name__)


def _ws_url(http_url: str, path: str, token: str) -> str:
    url = http_url.strip()
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(parts.scheme)
    if not scheme or not parts.netloc or parts.fragment or parts.username or parts.password:
        raise ValueError("dashboard URL must be an HTTP(S) or WebSocket URL without credentials or a fragment")
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "token"]
    query.append(("token", token))
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + path, urlencode(query), ""))


class _PtyChild:
    """One command in a fresh local PTY."""

    def __init__(self, command: list[str], cols: int, rows: int, env: dict):
        from ptyprocess import PtyProcess
        self._proc = PtyProcess.spawn(command, cwd=os.getcwd(), env=env, dimensions=(rows, cols))

    @property
    def fd(self) -> int:
        return self._proc.fd

    def poll(self) -> int | None:
        if self._proc.isalive():
            return None
        return self._proc.exitstatus if self._proc.exitstatus is not None else 1

    def terminate(self) -> None:
        self._proc.terminate(force=False)

    def wait(self, timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        while self._proc.isalive():
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.05)
        return self.poll()

    def kill(self) -> None:
        self._proc.kill(getattr(signal, "SIGKILL", signal.SIGTERM))


def _spawn_pty(command: list[str], cols: int, rows: int, env: dict) -> _PtyChild:
    return _PtyChild(command, cols, rows, env)


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    import fcntl
    import struct
    import termios
    if not (1 <= cols <= 65535 and 1 <= rows <= 65535):
        return
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        _log.debug("PTY resize failed", exc_info=True)


def _register(url: str, token: str, hello: dict):
    try:
        ws_url = _ws_url(url, "/api/byot/host", token)
        print("hermes byot: connecting to the dashboard ...", file=sys.stderr)
        ws = connect(ws_url, open_timeout=15)
    except (ValueError, OSError, TimeoutError, InvalidURI, InvalidHandshake) as exc:
        # An exception may contain a URL bearing the credential.
        print(f"hermes byot: dashboard connection failed ({type(exc).__name__}).", file=sys.stderr)
        return None
    try:
        ws.send(json.dumps(hello))
        registered = json.loads(ws.recv(timeout=15))
        byot_id = registered.get("id") if isinstance(registered, dict) else None
        if not isinstance(byot_id, str) or not byot_id:
            raise ValueError("missing registration id")
    except (ConnectionClosed, OSError, TimeoutError, ValueError, TypeError):
        print("hermes byot: dashboard did not accept the registration.", file=sys.stderr)
        ws.close()
        return None
    return ws, byot_id


class _Relay:
    """Run local input and output independently of the dashboard connection."""

    def __init__(self, ws, child: _PtyChild, stdin_fd: int | None, out):
        self.ws, self.child, self.stdin_fd, self.out = ws, child, stdin_fd, out
        self.stop = threading.Event()
        self.disconnected = threading.Event()
        self.ws_lock = threading.Lock()

    def send(self, message: str | bytes) -> None:
        if self.disconnected.is_set():
            return
        try:
            with self.ws_lock:
                self.ws.send(message)
        except (ConnectionClosed, OSError):
            self.disconnected.set()

    def read_pty(self) -> None:
        while not self.stop.is_set():
            try:
                chunk = os.read(self.child.fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            try:
                self.out.buffer.write(chunk)
                self.out.buffer.flush()
            except (OSError, ValueError):
                _log.debug("Local terminal output unavailable", exc_info=True)
            self.send(chunk)
        self.stop.set()

    def read_stdin(self) -> None:
        if self.stdin_fd is None:
            return
        while not self.stop.is_set():
            try:
                chunk = os.read(self.stdin_fd, 4096)
                if not chunk:
                    return
                os.write(self.child.fd, chunk)
            except OSError:
                return

    def resize(self, signum, frame) -> None:
        size = shutil.get_terminal_size(fallback=(80, 24))
        _set_winsize(self.child.fd, size.columns, size.lines)
        self.send(json.dumps({"type": "resize", "cols": size.columns, "rows": size.lines}))

    def receive(self) -> None:
        while not self.stop.is_set():
            if self.disconnected.is_set():
                if self.child.poll() is not None:
                    return
                self.stop.wait(0.2)
                continue
            try:
                message = self.ws.recv(timeout=0.5)
            except TimeoutError:
                if self.child.poll() is not None:
                    return
                continue
            except (ConnectionClosed, OSError):
                self.disconnected.set()
                print("hermes byot: dashboard disconnected; the local terminal keeps running.", file=sys.stderr)
                continue
            if isinstance(message, bytes):
                try:
                    os.write(self.child.fd, message)
                except OSError:
                    return
            else:
                self.control(message)

    def control(self, message: str) -> None:
        try:
            control = json.loads(message)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(control, dict) or control.get("type") != "resize":
            return
        cols, rows = control.get("cols"), control.get("rows")
        if type(cols) is int and type(rows) is int:
            _set_winsize(self.child.fd, cols, rows)

    def run(self) -> int:
        saved_termios = None
        if self.stdin_fd is not None:
            import termios
            import tty
            saved_termios = termios.tcgetattr(self.stdin_fd)
            tty.setraw(self.stdin_fd)
        old_winch = signal.signal(signal.SIGWINCH, self.resize)
        output_thread = spawn_context_thread(self.read_pty, name="byot-output")
        output_thread.start()
        spawn_context_thread(self.read_stdin, name="byot-input").start()
        try:
            self.receive()
        finally:
            if self.child.poll() is not None:
                output_thread.join(timeout=1)
            self.stop.set()
            signal.signal(signal.SIGWINCH, old_winch)
            if saved_termios is not None:
                import termios
                termios.tcsetattr(self.stdin_fd, termios.TCSADRAIN, saved_termios)
            self.ws.close()
            if self.child.poll() is None:
                self.child.terminate()
                if self.child.wait(timeout=3) is None:
                    self.child.kill()
                    self.child.wait(timeout=3)
            try:
                os.close(self.child.fd)
            except OSError:
                _log.debug("PTY already closed", exc_info=True)
        return self.child.poll() or 0


def run_byot_bridge(url: str = DEFAULT_DASHBOARD_URL, token: str = "", name: str = "",
                    command: list[str] | None = None, out=None) -> int:
    """Bridge the current terminal to the dashboard; return the child exit code."""
    token = token or configured_session_token()
    if not token:
        print("hermes byot: no dashboard session token. Pass --token or set "
              "HERMES_DASHBOARD_SESSION_TOKEN.", file=sys.stderr)
        return 2
    if sys.platform.startswith("win"):
        print("hermes byot: the local terminal bridge requires a POSIX PTY (macOS/Linux).", file=sys.stderr)
        return 2
    command = list(command or [])
    if command[:1] == ["--"]:
        command = command[1:]
    command = command or [os.environ.get("SHELL") or "/bin/sh"]
    cols, rows = shutil.get_terminal_size(fallback=(80, 24))
    hello = {"type": "hello", "name": name or f"{socket.gethostname()}:{os.getpid()}",
             "shell": command[0], "cwd": os.getcwd(), "pid": os.getpid(), "cols": cols, "rows": rows}
    registration = _register(url, token, hello)
    if registration is None:
        return 2
    ws, byot_id = registration
    print(f"hermes byot: attached as '{byot_id}'. Attach it in the web Chat terminal selector.\n"
          "Use ‘Use embedded terminal’ in Chat to detach. Ctrl+C is sent to the terminal unless used to copy a selection.", file=sys.stderr)
    env = build_subprocess_env(base=served_profile_child_env(target_home=get_hermes_home()), scrub_secrets=True)
    env.pop("HERMES_DASHBOARD_SESSION_TOKEN", None)
    try:
        child = _spawn_pty(command, cols, rows, env)
    except (OSError, ValueError) as exc:
        ws.close()
        print(f"hermes byot: could not start terminal ({type(exc).__name__}).", file=sys.stderr)
        return 2
    stdin_fd = sys.stdin.fileno() if sys.stdin.isatty() else None
    return _Relay(ws, child, stdin_fd, out or sys.stdout).run()
