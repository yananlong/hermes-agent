"""Client side of BYOT (bring your own terminal).

``hermes byot`` runs in an external terminal (e.g. iTerm on macOS with zsh),
spawns a command (default: the user's login shell) in a local PTY, shows it
locally as usual, and bridges the PTY stream to the Hermes web UI over the
``/api/byot/host`` WebSocket. The dashboard's Chat tab can then attach via
``/api/pty?byot=<id>`` and mirror/control this terminal instead of the
embedded one.

POSIX only (the local PTY mirror needs ``pty``/``termios``); the gateway side
accepts hosts from any platform.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import sys
import threading
import time

DEFAULT_DASHBOARD_URL = "http://127.0.0.1:8642"


def _ws_url(http_url: str, path: str, token: str) -> str:
    url = http_url.strip().rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url[len("https://") :]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://") :]
    elif not url.startswith("ws://") and not url.startswith("wss://"):
        url = "ws://" + url
    sep = "&" if "?" in url else "?"
    return f"{url}{path}{sep}token={token}"


class _PtyChild:
    """A command running in a fresh PTY (via ptyprocess, like pty_bridge)."""

    def __init__(self, command: list[str], cols: int, rows: int, env: dict):
        from ptyprocess import PtyProcess

        self._proc = PtyProcess.spawn(
            command, cwd=os.getcwd(), env=env, dimensions=(rows, cols)
        )

    @property
    def fd(self) -> int:
        return self._proc.fd

    def poll(self) -> int | None:
        if self._proc.isalive():
            return None
        return self._proc.exitstatus if self._proc.exitstatus is not None else 1

    @property
    def returncode(self) -> int | None:
        return self.poll()

    def terminate(self) -> None:
        try:
            self._proc.terminate(force=False)
        except Exception:
            pass

    def wait(self, timeout: float | None = None) -> int | None:
        deadline = None if timeout is None else time.time() + timeout
        while self._proc.isalive():
            if deadline is not None and time.time() >= deadline:
                return None
            time.sleep(0.05)
        return self.poll()

    def kill(self) -> None:
        try:
            self._proc.kill(signal.SIGKILL)
        except Exception:
            pass


def _spawn_pty(command: list[str], cols: int, rows: int, env: dict) -> _PtyChild:
    """Spawn *command* in a fresh PTY; return the child handle (master fd)."""
    return _PtyChild(command, cols, rows, env)


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    import fcntl
    import struct
    import termios

    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def run_byot_bridge(
    url: str = DEFAULT_DASHBOARD_URL,
    token: str = "",
    name: str = "",
    command: list[str] | None = None,
    out=None,
) -> int:
    """Bridge the current terminal to the dashboard. Returns an exit code."""
    out = out or sys.stdout
    token = token or os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN", "")
    if not token:
        print(
            "hermes byot: no dashboard session token. Pass --token <token> or set\n"
            "HERMES_DASHBOARD_SESSION_TOKEN (the value the dashboard/web server was\n"
            "started with) so the bridge can authenticate its WebSocket.",
            file=sys.stderr,
        )
        return 2
    if sys.platform.startswith("win"):
        print(
            "hermes byot: the local terminal bridge requires a POSIX PTY (macOS/Linux).",
            file=sys.stderr,
        )
        return 2

    from websockets.sync.client import connect

    if command:
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            command = None
    if not command:
        shell = os.environ.get("SHELL") or "/bin/sh"
        command = [shell]

    cols, rows = shutil.get_terminal_size(fallback=(80, 24))
    hello = {
        "type": "hello",
        "name": name or f"{socket.gethostname()}:{os.getpid()}",
        "shell": command[0],
        "cwd": os.getcwd(),
        "pid": os.getpid(),
        "cols": cols,
        "rows": rows,
    }

    ws_url = _ws_url(url, "/api/byot/host", token)
    print(f"hermes byot: connecting to {ws_url.split('?')[0]} ...", file=sys.stderr)
    try:
        ws = connect(ws_url, open_timeout=15)
    except Exception as exc:
        print(
            f"hermes byot: cannot reach the dashboard at {url}: {exc}", file=sys.stderr
        )
        return 2

    ws.send(json.dumps(hello))
    try:
        registered = json.loads(ws.recv(timeout=15))
        byot_id = registered.get("id") if isinstance(registered, dict) else None
    except Exception:
        byot_id = None
    if not byot_id:
        print(
            "hermes byot: dashboard did not accept the registration.", file=sys.stderr
        )
        ws.close()
        return 2
    print(
        f"hermes byot: attached as '{byot_id}' — in the Hermes web UI Chat tab, "
        f"attach the external terminal '{byot_id}' to mirror this terminal.\n"
        f"(Ctrl-C in the web UI detaches; the local terminal keeps running.)",
        file=sys.stderr,
    )

    env = dict(os.environ)
    child = _spawn_pty(command, cols, rows, env)
    master = child.fd

    # Local terminal raw mode so keystrokes pass through to the child.
    stdin_fd = sys.stdin.fileno() if sys.stdin.isatty() else None
    saved_termios = None
    if stdin_fd is not None:
        import termios
        import tty

        saved_termios = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)

    stop = threading.Event()
    ws_lock = threading.Lock()

    def send_control(control: dict) -> None:
        try:
            with ws_lock:
                ws.send(json.dumps(control))
        except Exception:
            stop.set()

    def pty_reader() -> None:
        """PTY output -> local terminal AND dashboard viewers."""
        while not stop.is_set():
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break  # child exited, PTY closed
            if not chunk:
                break
            try:
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            except Exception:
                pass
            try:
                with ws_lock:
                    ws.send(chunk)
            except Exception:
                pass  # dashboard gone — the local terminal keeps running
        stop.set()

    def stdin_reader() -> None:
        """Local keystrokes -> PTY."""
        if stdin_fd is None:
            return  # no local terminal (piped/CI): the dashboard drives input
        while not stop.is_set():
            try:
                chunk = os.read(stdin_fd, 4096)
            except OSError:
                break
            if not chunk:
                continue
            try:
                os.write(master, chunk)
            except OSError:
                break
        stop.set()

    def on_winchch(signum, frame) -> None:
        size = shutil.get_terminal_size(fallback=(80, 24))
        _set_winsize(master, size.columns, size.lines)
        send_control({"type": "resize", "cols": size.columns, "rows": size.lines})

    threads = [
        threading.Thread(target=pty_reader, daemon=True),
        threading.Thread(target=stdin_reader, daemon=True),
    ]
    for t in threads:
        t.start()
    old_winch = signal.signal(signal.SIGWINCH, on_winchch)

    exit_code = 0
    ws_open = True
    try:
        while not stop.is_set():
            if not ws_open:
                # Dashboard gone: keep the local terminal alive until the
                # child (the user's shell) exits on its own.
                if child.poll() is not None:
                    break
                time.sleep(0.2)
                continue
            try:
                msg = ws.recv(timeout=0.5)
            except TimeoutError:
                if child.poll() is not None:
                    break
                continue
            except Exception:
                ws_open = False
                print(
                    "hermes byot: dashboard disconnected; the local terminal keeps running.",
                    file=sys.stderr,
                )
                continue
            if isinstance(msg, bytes):
                try:
                    os.write(master, msg)
                except OSError:
                    break
            else:
                try:
                    control = json.loads(msg)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(control, dict) and control.get("type") == "resize":
                    cols = control.get("cols")
                    rows = control.get("rows")
                    if isinstance(cols, int) and isinstance(rows, int):
                        _set_winsize(master, cols, rows)
    finally:
        stop.set()
        signal.signal(signal.SIGWINCH, old_winch)
        if saved_termios is not None and stdin_fd is not None:
            import termios

            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved_termios)
        try:
            ws.close()
        except Exception:
            pass
        if child.poll() is None:
            try:
                child.terminate()
                child.wait(timeout=3)
            except Exception:
                child.kill()
        try:
            os.close(master)
        except OSError:
            pass
        exit_code = child.returncode or 0
    return exit_code
