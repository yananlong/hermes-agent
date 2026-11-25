"""Append one Hermes turn to a linked Claude Code session through the native CLI.

A linked Claude row is a *pointer*: ``model_config.claude_session_id`` names a session whose
transcript lives in Claude Code's own store (``~/.claude/projects/<slug>/<uuid>.jsonl``, relocated
wholesale by ``CLAUDE_CONFIG_DIR``). Because that file is the single transcript, a writable linked
session must not paste a Hermes copy beside it — it appends to the *same* thread by running the
CLI's own resume path:

    claude -p "<prompt>" --resume <session-id> --output-format stream-json --verbose

``--verbose`` is required by the CLI beside ``stream-json`` in print mode; a build that rejects
``stream-json`` is retried once with ``--output-format json`` (see ``_run_once``). Nothing else is
added to the argv: no ``--session-id`` (that would START a session), no ``--fork-session`` (that
would COPY one), and no permission or tool-policy flag at all.

Three properties are asserted here rather than trusted:

* **No fork.** Every event stream echoes the session id the CLI is writing. A different id is
  reported as :data:`CLAUDE_SESSION_MISMATCH`; this transport never "adopts" the new id, so the
  native thread id is the same before and after a turn.
* **Auth is not a success.** A logged-out CLI prints ``"result": "Not logged in · Please run
  /login"`` and still exits 0. That is :data:`CLAUDE_AUTH_REQUIRED`, never an empty answer.
* **Permissions are the user's.** The argv carries no ``--permission-mode`` /
  ``--dangerously-skip-permissions`` / ``--allowed-tools``, so the CLI applies the policy the user
  configured; an approval a headless run cannot get surfaces as
  :data:`CLAUDE_PERMISSION_REQUIRED` instead of being auto-approved.

The turn's rows are shaped exactly like the read path's (``hermes_cli/claude_linked_history``:
``{"role", "content", "timestamp"}``, redacted and capped) so a live turn and a re-read of the
native store render the same. The caller shows them; nothing here writes a Hermes transcript.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from hermes_cli.foreign_linked_store import foreign_session_uuid

logger = logging.getLogger(__name__)

STREAM_JSON = "stream-json"
JSON_OUTPUT = "json"
_OUTPUT_FORMATS = (STREAM_JSON, JSON_OUTPUT)

# One Claude turn can edit files for minutes; the bound is ours, not the CLI's.
DEFAULT_TURN_TIMEOUT_SECONDS = 900.0

# Stable refusal codes: callers compare the string, never the exception type.
CLAUDE_MISSING_BINARY = "claude_missing_binary"
CLAUDE_SPAWN_FAILED = "claude_spawn_failed"
CLAUDE_AUTH_REQUIRED = "claude_auth_required"
CLAUDE_PERMISSION_REQUIRED = "claude_permission_required"
CLAUDE_SESSION_MISSING = "claude_session_missing"
CLAUDE_SESSION_MISMATCH = "claude_session_mismatch"
CLAUDE_TIMEOUT = "claude_timeout"
CLAUDE_CANCELLED = "claude_cancelled"
CLAUDE_EMPTY_RESULT = "claude_empty_result"
CLAUDE_INVALID_OUTPUT = "claude_invalid_output"
CLAUDE_EXIT_ERROR = "claude_exit_error"

# Markers a logged-out (or key-rejected) CLI reports in the result text / stderr, with exit 0.
_AUTH_MARKERS = (
    "not logged in", "please run /login", "please log in", "invalid api key",
    "oauth token has expired", "authentication_error", "failed to authenticate",
    "run `claude login`", "/login to authenticate",
)
# An approval the CLI would need a human for; headless we fail closed instead of granting it.
_PERMISSION_MARKERS = (
    "requires approval", "requires permission", "permission denied", "not permitted",
    "tool use was denied", "requested permissions to use", "rejected by permission",
)
# A resume of a session this store does not have. The CLI sometimes starts a fresh session here,
# which is exactly the fork the caller must hear about as a refusal.
_SESSION_MISSING_MARKERS = (
    "no conversation found with session id", "no conversation found", "no session found",
    "session not found", "could not find session",
)
# A build that predates an output format: retried once with the other one.
_FORMAT_UNSUPPORTED_MARKERS = (
    "unknown option", "unknown argument", "unrecognized option", "no such option",
    "invalid value", "invalid choice",
)

# One argv element holds the prompt; ARG_MAX allows far less once the environment is counted.
_MAX_ARGV_PROMPT_CHARS = 32 * 1024
_POLL_SECONDS = 0.05
_EXIT_WAIT_SECONDS = 10.0
_MAX_STDOUT_BYTES = 8 * 1024 * 1024
_MAX_EVENTS = 4096
_MAX_ROWS = 2048
_STDERR_TAIL_LINES = 40
_BRIEF_CHARS = 400


class ClaudeCliError(RuntimeError):
    """A typed refusal from the Claude resume transport; ``code`` is the stable wire value."""

    def __init__(self, code: str, detail: str, *, stderr_tail: str = "",
                 exit_code: int | None = None) -> None:
        self.code = code
        self.detail = _brief(detail)
        self.stderr_tail = _brief(stderr_tail, 800)
        self.exit_code = exit_code
        super().__init__(f"{code}: {self.detail}")


@dataclass
class ClaudeCliTurn:
    """One finished ``claude --resume`` turn and what it said about the native thread."""

    requested_session_id: str
    native_session_id: str | None = None
    rows: list[dict] = field(default_factory=list)
    final_text: str = ""
    model: str | None = None
    usage: dict | None = None
    output_format: str = STREAM_JSON
    argv: tuple[str, ...] = ()
    exit_code: int = 0
    stderr_tail: str = ""
    duration_ms: int = 0
    event_count: int = 0
    prompt_via_stdin: bool = False


class _FormatUnsupported(Exception):
    """The CLI rejected ``--output-format <format>`` before doing any work: retry the other one."""


def resolve_claude_binary(config: Mapping[str, Any] | None = None,
                          command: str | None = None) -> str:
    """Absolute path of the ``claude`` executable, or :data:`CLAUDE_MISSING_BINARY`.

    Mirrors ``hermes_cli.codex_runtime_switch.get_configured_codex_binary``: one argv element,
    never shell-parsed, with a config-level override (``model.claude_bin``) for gateway/service
    launches whose PATH lacks the CLI. Resolution order is explicit *command* → configured
    override → ``PATH`` → Claude Code's install prefixes (:func:`_install_prefix_binary`), so a
    test that shims PATH never reaches a machine-installed binary.
    """
    candidate = str(command or "").strip() or _configured_binary(config)
    if candidate:
        if os.sep in candidate or (os.altsep and os.altsep in candidate):
            path = os.path.abspath(os.path.expanduser(os.path.expandvars(candidate)))
            if not _is_executable(path):
                raise ClaudeCliError(
                    CLAUDE_MISSING_BINARY,
                    f"the configured Claude binary is not an executable file: {candidate}")
            return path
        found = _path_binary(candidate) or _install_prefix_binary(candidate)
        if found:
            return found
        raise ClaudeCliError(CLAUDE_MISSING_BINARY,
                             f"the configured Claude command was not found: {candidate}")
    found = _path_binary("claude") or _install_prefix_binary("claude")
    if found:
        return found
    raise ClaudeCliError(
        CLAUDE_MISSING_BINARY,
        "The 'claude' CLI is not installed. Install it with: "
        "npm install -g @anthropic-ai/claude-code")


def build_resume_argv(claude_bin: str, prompt: str, native_session_id: str, *,
                      output_format: str = STREAM_JSON) -> list[str]:
    """The exact argv for one resume turn: a list, never a shell string.

    The prompt rides after ``-p`` unless it starts with ``-``: ``-p`` takes an optional value, so a
    dash-leading prompt would be parsed as a flag and the CLI would wait on stdin. Callers check
    :func:`prompt_goes_to_stdin` and write the prompt there instead.
    """
    if output_format not in _OUTPUT_FORMATS:
        raise ValueError(f"unsupported Claude output format: {output_format!r}")
    argv = [claude_bin, "-p"]
    if not prompt_goes_to_stdin(prompt):
        argv.append(prompt)
    return [*argv, "--resume", native_session_id, "--output-format", output_format, "--verbose"]


def prompt_goes_to_stdin(prompt: str) -> bool:
    """True when the prompt must be piped to ``claude -p`` instead of passed as its value.

    Two cases: a dash-leading prompt (``-p`` takes an optional value, so it would be parsed as a
    flag) and one too long for a single argv element (a pasted 100 KB message would fail ``exec``
    with E2BIG instead of reaching the CLI).
    """
    return prompt.lstrip().startswith("-") or len(prompt) > _MAX_ARGV_PROMPT_CHARS


def run_claude_resume_turn(
    prompt: str,
    *,
    native_session_id: str,
    cwd: str | os.PathLike[str],
    claude_bin: str | None = None,
    config: Mapping[str, Any] | None = None,
    timeout: float | None = DEFAULT_TURN_TIMEOUT_SECONDS,
    cancel_event: Any = None,
    on_event: Callable[[dict], None] | None = None,
    env_overrides: Mapping[str, str] | None = None,
) -> ClaudeCliTurn:
    """Run one turn against the native thread *native_session_id* and return what it appended.

    *cwd* is the linked thread's own workspace: Claude records a session under the project dir its
    cwd encodes, so resuming from anywhere else can start a second project dir for one thread (see
    ``hermes_cli/claude_linked_resume.resolve_linked_claude_target``, which resolves it).

    *claude_bin* is an absolute path (or a name resolved through :func:`resolve_claude_binary`).
    *cancel_event* is any ``threading.Event``-shaped object; setting it kills the child's process
    group and raises :data:`CLAUDE_CANCELLED`. *timeout* bounds the whole turn
    (:data:`CLAUDE_TIMEOUT`). *env_overrides* is layered over the operator's environment — the
    native CLI owns its own auth, so Hermes adds nothing but what the caller resolves for it.

    Raises :class:`ClaudeCliError` for every failure mode a caller must branch on.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("a non-empty prompt is required")
    session_id = foreign_session_uuid(native_session_id)
    workdir = Path(cwd)
    if not workdir.is_absolute() or not workdir.is_dir():
        raise ClaudeCliError(CLAUDE_SPAWN_FAILED,
                             f"the linked thread's working directory is unavailable: {workdir}")
    binary = resolve_claude_binary(config, claude_bin)

    try:
        return _run_once(prompt, session_id=session_id, cwd=workdir, claude_bin=binary,
                         output_format=STREAM_JSON, timeout=timeout, cancel_event=cancel_event,
                         on_event=on_event, env_overrides=env_overrides)
    except _FormatUnsupported:
        try:
            return _run_once(prompt, session_id=session_id, cwd=workdir, claude_bin=binary,
                             output_format=JSON_OUTPUT, timeout=timeout, cancel_event=cancel_event,
                             on_event=on_event, env_overrides=env_overrides)
        except _FormatUnsupported:
            raise ClaudeCliError(
                CLAUDE_INVALID_OUTPUT,
                "This Claude Code build supports neither stream-json nor json output") from None


def _run_once(prompt: str, *, session_id: str, cwd: Path, claude_bin: str,
              output_format: str, timeout: float | None, cancel_event: Any,
              on_event: Callable[[dict], None] | None,
              env_overrides: Mapping[str, str] | None) -> ClaudeCliTurn:
    """One spawn + drain; raises :class:`_FormatUnsupported` when the flag itself was refused."""
    stdin_prompt = prompt_goes_to_stdin(prompt)
    argv = build_resume_argv(claude_bin, prompt, session_id, output_format=output_format)
    started = time.monotonic()
    proc = _spawn(argv, cwd=cwd, env_overrides=env_overrides, stdin_prompt=stdin_prompt)
    lines: queue.Queue = queue.Queue()
    stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
    raw = _RawStdout()
    try:
        _start_readers(proc, lines, stderr_tail)
        if stdin_prompt:
            _feed_stdin(proc, prompt, started=started, timeout=timeout, cancel_event=cancel_event)
        events = _drain(proc, lines, raw, started=started, timeout=timeout,
                        cancel_event=cancel_event, on_event=on_event)
        exit_code = _reap(proc)
    finally:
        _kill(proc)
    duration_ms = int((time.monotonic() - started) * 1000)
    stderr_text = "\n".join(stderr_tail)
    if not events and not raw.truncated and _format_rejected(stderr_text, exit_code):
        raise _FormatUnsupported(output_format)
    return _classify(argv=argv, session_id=session_id, events=events, raw=raw,
                     exit_code=exit_code, stderr_text=stderr_text, output_format=output_format,
                     duration_ms=duration_ms, stdin_prompt=stdin_prompt)


class _RawStdout:
    """Bounded copy of stdout: the fallback parse needs it, memory does not need all of it."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.size = 0
        self.truncated = False

    def add(self, line: str) -> None:
        self.size += len(line)
        if self.size > _MAX_STDOUT_BYTES:
            self.truncated = True
            return
        self.parts.append(line)

    @property
    def text(self) -> str:
        return "".join(self.parts)


def _spawn(argv: list[str], *, cwd: Path, env_overrides: Mapping[str, str] | None,
           stdin_prompt: bool) -> subprocess.Popen:
    """Start the CLI with pipes, a hidden window on Windows and its own group on POSIX."""
    try:
        return subprocess.Popen(
            argv, stdin=subprocess.PIPE if stdin_prompt else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(cwd),
            env=_native_cli_env(env_overrides), text=True, encoding="utf-8", errors="replace",
            bufsize=1, shell=False, **_group_kwargs())
    except FileNotFoundError as exc:
        raise ClaudeCliError(CLAUDE_MISSING_BINARY,
                             f"the Claude binary disappeared before the turn started: {argv[0]}") from exc
    except OSError as exc:
        raise ClaudeCliError(CLAUDE_SPAWN_FAILED, f"could not start the Claude CLI: {exc}") from exc


def _group_kwargs() -> dict[str, Any]:
    """Hidden console on Windows; its own process group on POSIX so a timeout kills the tree."""
    from hermes_cli._subprocess_compat import IS_WINDOWS, windows_hide_flags

    if IS_WINDOWS:
        return {"creationflags": windows_hide_flags()}
    # detached-spawn: ok — awaited turn; its timeout kills the whole group (kill_process_tree)
    return {"process_group": 0}


def _native_cli_env(overrides: Mapping[str, str] | None) -> dict[str, str]:
    """The child's environment: the SERVED profile's own, plus what the caller resolved for it.

    Claude Code authenticates as the user (``CLAUDE_CONFIG_DIR``, ``~/.claude`` credentials,
    ``ANTHROPIC_*``), so Hermes adds no credential of its own: the base is the profile-scoped child
    env, and the one overlay is the store the caller resolved through the profile secret scope.

    ``os.environ`` is the LAUNCH profile's — under ``gateway.multiplex_profiles`` that is the DEFAULT
    profile's key, so an unbound read here signs a routed turn's child in as the wrong profile, and a
    scoped miss must fail closed rather than fall back to it (``served_profile_child_env``).
    """
    from tools.environments.local import served_profile_child_env

    env = served_profile_child_env(inherit_credentials=True)
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        env[str(key)] = str(value)
    return env


def _start_readers(proc: subprocess.Popen, lines: queue.Queue, stderr_tail: deque[str]) -> None:
    """Drain both pipes on context-bound daemon threads (a silent child must not block us)."""
    from agent.memory_provider import spawn_context_thread

    def read_stdout() -> None:
        try:
            for line in proc.stdout or ():
                lines.put(("line", line))
        except (OSError, ValueError) as exc:
            lines.put(("error", str(exc)))
        finally:
            lines.put(("eof", None))

    def read_stderr() -> None:
        try:
            for line in proc.stderr or ():
                if line.strip():
                    stderr_tail.append(line.rstrip())
        except (OSError, ValueError):
            return

    spawn_context_thread(read_stdout, name="claude-resume-stdout").start()
    spawn_context_thread(read_stderr, name="claude-resume-stderr").start()


def _feed_stdin(proc: subprocess.Popen, prompt: str, *, started: float, timeout: float | None,
                cancel_event: Any) -> None:
    """Write the prompt to ``claude -p``'s stdin under the turn's deadline and cancellation.

    A pipe write blocks in the kernel until the child drains it, so a child that never reads its
    stdin would hold the turn past its own timeout before :func:`_drain` ever ran. The write
    therefore runs on a context-bound thread while the caller polls the same deadline/cancel checks
    the drain uses; on expiry the child's group is killed (which breaks the pipe and ends the write)
    and the typed refusal is raised. A closed pipe is the child's own error, as before.
    """
    from agent.memory_provider import spawn_context_thread

    written = threading.Event()
    payload = prompt if prompt.endswith("\n") else prompt + "\n"
    stream = proc.stdin
    assert stream is not None  # _spawn opens a stdin pipe whenever the prompt rides on it

    def write() -> None:
        try:
            stream.write(payload)
            stream.flush()
        except (OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(OSError, ValueError):
                stream.close()
            written.set()

    spawn_context_thread(write, name="claude-resume-stdin").start()
    deadline = None if timeout is None else started + timeout
    while not written.is_set():
        if cancel_event is not None and cancel_event.is_set():
            _kill(proc)
            raise ClaudeCliError(CLAUDE_CANCELLED, "the linked Claude turn was cancelled")
        wait = _POLL_SECONDS
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill(proc)
                raise ClaudeCliError(
                    CLAUDE_TIMEOUT, f"the linked Claude turn exceeded {timeout:g}s")
            wait = min(wait, remaining)
        written.wait(wait)


def _drain(proc: subprocess.Popen, lines: queue.Queue, raw: _RawStdout, *, started: float,
           timeout: float | None, cancel_event: Any,
           on_event: Callable[[dict], None] | None) -> list[dict]:
    """Collect parsed events until the child closes stdout; cancel and timeout kill the group."""
    deadline = None if timeout is None else started + timeout
    events: list[dict] = []
    while True:
        if cancel_event is not None and cancel_event.is_set():
            _kill(proc)
            raise ClaudeCliError(CLAUDE_CANCELLED, "the linked Claude turn was cancelled")
        wait = _POLL_SECONDS
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill(proc)
                raise ClaudeCliError(
                    CLAUDE_TIMEOUT, f"the linked Claude turn exceeded {timeout:g}s")
            wait = min(wait, remaining)
        try:
            kind, payload = lines.get(timeout=wait)
        except queue.Empty:
            continue
        if kind == "eof":
            return events
        if kind == "error":
            raise ClaudeCliError(CLAUDE_EXIT_ERROR,
                                 f"reading the Claude CLI's output failed: {payload}")
        raw.add(payload)
        event = _parse_event(payload)
        if event is None:
            continue
        if len(events) < _MAX_EVENTS:
            events.append(event)
        if on_event is not None:
            _notify(on_event, event)


def _notify(on_event: Callable[[dict], None], event: dict) -> None:
    """Hand one event to the caller's display callback; its failure is logged, never fatal.

    The native turn is already being appended by the child, so a rendering bug must not abort the
    read half and strand the caller's view of the turn. This is the deliberate catch-all boundary,
    and the traceback goes to the log.
    """
    try:
        on_event(event)
    except Exception:  # health: allow BLE001 -- a display callback must not abandon a native turn
        logger.warning("linked Claude turn event callback failed", exc_info=True)


def _reap(proc: subprocess.Popen) -> int:
    """Wait for the exited child; a child that will not die after EOF is killed."""
    try:
        return proc.wait(timeout=_EXIT_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill(proc)
        raise ClaudeCliError(CLAUDE_TIMEOUT, "the Claude CLI kept running after closing its output") from None


def _kill(proc: subprocess.Popen) -> None:
    """Kill the child's whole process group and reap it; a finished child is a no-op."""
    if proc.poll() is not None:
        return
    from agent.deadline import kill_process_tree

    kill_process_tree(proc.pid)
    try:
        proc.wait(timeout=_EXIT_WAIT_SECONDS)
    except (subprocess.TimeoutExpired, OSError):
        proc.kill()


def _parse_event(line: str) -> dict | None:
    """One stdout line as a JSON event; anything unparseable is skipped (warnings share stdout)."""
    text = line.strip()
    if not text or not text.startswith("{"):
        return None
    try:
        event = json.loads(text)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def _classify(*, argv: list[str], session_id: str, events: list[dict], raw: _RawStdout,
              exit_code: int, stderr_text: str, output_format: str, duration_ms: int,
              stdin_prompt: bool) -> ClaudeCliTurn:
    """Turn the collected events into a result, or raise the typed refusal they describe.

    Identity and auth are decided first: a logged-out or forked run is refused before its (empty
    or missing) answer is mistaken for a successful turn.
    """
    rows, final_text, model, usage = _stream_rows(events)
    result_event = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if output_format == JSON_OUTPUT or not events:
        # `--output-format json` is one document, not a stream: read it whole so a document that
        # also carries `messages` projects rows instead of collapsing into its summary line.
        rows, final_text, model, usage, result_event = _json_output(raw)
    reported = _reported_session_id(events, result_event)
    result_text = str(result_event.get("result") or "") if result_event else ""

    _refuse(f"{result_text}\n{stderr_text}", reported, session_id, exit_code, stderr_text,
            result_event, events)
    if not _has_answer(rows, final_text):
        if raw.text.strip() and not events:
            raise ClaudeCliError(
                CLAUDE_INVALID_OUTPUT,
                "the Claude CLI produced output this transport cannot parse",
                stderr_tail=stderr_text, exit_code=exit_code)
        raise ClaudeCliError(
            CLAUDE_EMPTY_RESULT,
            "the Claude CLI exited 0 without an answer (no assistant message in its output)",
            stderr_tail=stderr_text, exit_code=exit_code)
    return ClaudeCliTurn(
        requested_session_id=session_id, native_session_id=reported or session_id, rows=rows,
        final_text=final_text or _last_assistant_text(rows), model=model, usage=usage,
        output_format=output_format, argv=tuple(argv), exit_code=exit_code,
        stderr_tail=_brief(stderr_text, 800), duration_ms=duration_ms,
        event_count=len(events), prompt_via_stdin=stdin_prompt)


def _has_answer(rows: list[dict], final_text: str) -> bool:
    """True when the turn produced something a user can read."""
    return bool(final_text.strip()) or any(row["role"] == "assistant" for row in rows)


def _refuse(combined: str, reported: str | None, session_id: str, exit_code: int,
            stderr_text: str, result_event: dict | None, events: list[dict]) -> None:
    """Raise the first matching typed refusal; identity and auth precede generic exit failures."""
    lowered = combined.lower()
    if any(marker in lowered for marker in _AUTH_MARKERS):
        raise ClaudeCliError(
            CLAUDE_AUTH_REQUIRED,
            "Claude Code is not logged in (or its key was rejected); run `claude /login` and retry",
            stderr_tail=stderr_text, exit_code=exit_code)
    if reported is not None and reported != session_id:
        raise ClaudeCliError(
            CLAUDE_SESSION_MISMATCH,
            f"Claude resumed {reported} instead of {session_id}: the turn did not land on the "
            "linked thread", stderr_tail=stderr_text, exit_code=exit_code)
    if any(marker in lowered for marker in _SESSION_MISSING_MARKERS):
        raise ClaudeCliError(
            CLAUDE_SESSION_MISSING,
            f"Claude Code has no session {session_id} in this store",
            stderr_tail=stderr_text, exit_code=exit_code)
    if _needs_permission(combined, events, result_event):
        raise ClaudeCliError(
            CLAUDE_PERMISSION_REQUIRED,
            "the turn needed an approval Claude Code could not ask for; approve the tool once in "
            "the native CLI (or widen its own permission config) and retry",
            stderr_tail=stderr_text, exit_code=exit_code)
    if result_event is not None and result_event.get("is_error") is True:
        raise ClaudeCliError(
            CLAUDE_EXIT_ERROR,
            f"Claude reported {result_event.get('subtype') or 'an error'}: "
            f"{result_event.get('result') or 'no detail'}",
            stderr_tail=stderr_text, exit_code=exit_code)
    if exit_code != 0:
        raise ClaudeCliError(CLAUDE_EXIT_ERROR,
                             f"the Claude CLI exited {exit_code}",
                             stderr_tail=stderr_text, exit_code=exit_code)


def _needs_permission(combined: str, events: list[dict], result_event: dict | None) -> bool:
    """An approval the headless run could not get: denial list, or a permission marker."""
    denials = (result_event or {}).get("permission_denials")
    if isinstance(denials, list) and denials:
        return True
    if any(marker in combined.lower() for marker in _PERMISSION_MARKERS):
        return True
    return any(event.get("subtype") == "permission_requested" for event in events)


def _stream_rows(events: Iterable[dict]) -> tuple[list[dict], str, str | None, dict | None]:
    """Project assistant/user events into display rows plus the last text, model and usage."""
    rows: list[dict] = []
    model: str | None = None
    usage: dict | None = None
    final_text = ""
    for event in events:
        etype = event.get("type")
        if etype == "result":
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else usage
            final_text = str(event.get("result") or "") or final_text
            continue
        row = _message_row(event)
        if row is None:
            continue
        rows.append(row)
        message = event.get("message")
        if model is None and isinstance(message, dict) and isinstance(message.get("model"), str):
            model = message["model"]
        if len(rows) >= _MAX_ROWS:
            break
    return rows, final_text, model, usage


def _message_row(event: dict) -> dict | None:
    """One stream event as a display row, or None for anything the read path would also skip.

    The projection is ``hermes_cli.foreign_sessions._message_turn`` — the same function the native
    history reader calls — so a live row and a re-read row have identical text.
    """
    if event.get("type") not in ("assistant", "user"):
        return None
    if event.get("isSidechain") or event.get("isMeta") or event.get("parent_tool_use_id"):
        return None
    from hermes_cli.foreign_linked_store import safe_native_text
    from hermes_cli.foreign_sessions import _message_turn

    turn = _message_turn(event.get("message"))
    if turn is None:
        return None
    role, text = turn
    return {"role": role, "content": safe_native_text(text), "timestamp": _epoch(event.get("timestamp"))}


def _json_output(raw: _RawStdout) -> tuple[list[dict], str, str | None, dict | None, dict | None]:
    """Parse a whole ``--output-format json`` document (one object, no event stream).

    A document that is not JSON at all yields nothing here: the caller's ``_refuse`` reports the
    exit code or auth failure behind it, and an unparseable answer on a clean exit becomes
    :data:`CLAUDE_INVALID_OUTPUT` there.
    """
    text = raw.text.strip()
    if not text.startswith("{"):
        return [], "", None, None, None
    try:
        document = json.loads(text)
    except ValueError:
        return [], "", None, None, None
    if not isinstance(document, dict):
        return [], "", None, None, None
    rows = [row for row in (_message_row(e) for e in document.get("messages") or []) if row]
    result = str(document.get("result") or "")
    if result and not rows:
        from hermes_cli.foreign_linked_store import safe_native_text
        rows = [{"role": "assistant", "content": safe_native_text(result), "timestamp": None}]
    usage = document.get("usage") if isinstance(document.get("usage"), dict) else None
    model = document.get("model") if isinstance(document.get("model"), str) else None
    return rows, result, model, usage, document


def _reported_session_id(events: Iterable[dict], result_event: dict | None) -> str | None:
    """The session id the CLI says it wrote, newest event first (the result is authoritative)."""
    ordered = list(events)[::-1]
    if result_event is not None:
        ordered.insert(0, result_event)
    for event in ordered:
        if isinstance(event.get("session_id"), str):
            return event["session_id"]
    return None


def _last_assistant_text(rows: Iterable[dict]) -> str:
    """The last assistant row's text — what a caller shows when the CLI echoed no result."""
    return next((row["content"] for row in reversed(list(rows)) if row["role"] == "assistant"), "")


def _format_rejected(stderr_text: str, exit_code: int) -> bool:
    """True when a nonzero exit is the CLI refusing the flag, not a failed turn.

    Argument parsing fails before the CLI touches the session, so the retry in
    :func:`run_claude_resume_turn` cannot double-append. Both halves must look like the refusal:
    an unknown-option word, and the flag itself named on the error line.
    """
    if exit_code == 0:
        return False
    lowered = stderr_text.lower()
    if "output-format" not in lowered and "stream-json" not in lowered:
        return False
    return any(marker in lowered for marker in _FORMAT_UNSUPPORTED_MARKERS)


def _configured_binary(config: Mapping[str, Any] | None) -> str:
    """``model.claude_bin`` from the caller's config, or "" (one argv element, never parsed)."""
    model_cfg = config.get("model") if isinstance(config, Mapping) else None
    value = model_cfg.get("claude_bin") if isinstance(model_cfg, Mapping) else None
    return str(value or "").strip()


def _path_binary(name: str) -> str | None:
    """An executable on PATH for *name*, as an absolute path."""
    import shutil

    hit = shutil.which(name)
    return os.path.abspath(hit) if hit else None


def _install_prefix_binary(name: str) -> str | None:
    """An executable in Claude Code's known install prefixes (a GUI/service PATH misses them)."""
    from agent.anthropic_adapter import find_claude_code_cli

    return find_claude_code_cli(name)


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _epoch(value: Any) -> float | None:
    """Epoch seconds from an event's ISO-8601 timestamp; None when absent or unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()  # 3.11+ reads the trailing "Z"
    except ValueError:
        return None


def _brief(value: Any, limit: int = _BRIEF_CHARS) -> str:
    """One bounded line of diagnostic text: whitespace collapsed, inedible characters dropped."""
    text = " ".join(str(value or "").split())
    return "".join(ch for ch in text if ch.isprintable())[:limit]
