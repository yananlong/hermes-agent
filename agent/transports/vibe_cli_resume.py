"""Append one Hermes turn to a native Mistral Vibe session — the write side of a linked thread.

A linked Mistral session is a *pointer* row: the native store owns the transcript. Unlike Codex
there is no persistent app-server protocol Hermes can hold open, so a writable turn is one headless
resume of the same native session:

    vibe -p "<prompt>" --resume <SESSION_ID> --workdir <cwd> --output json

``vibe --resume`` opens ``$VIBE_HOME/logs/session/unified/<SESSION_ID>/`` and appends this turn's
records to its ``journal/NNNN.jsonl`` log. Hermes therefore never creates a session, never forks one
and never writes its own copy of the turn:

* no-fork — the native id is an input, not an output: the caller passes it, ``--resume`` is the only
  session selector on the command line, and every message entry the run echoes back must carry that
  same ``sessionId`` (see :func:`parse_vibe_output` and the check in
  ``hermes_cli.mistral_linked_resume``). A run that answers a different session is a refusal, never
  a second thread.
* permissions — the argv this module builds can *only* resume under the policy the user configured
  in the native tool. ``--auto-approve`` / ``--yolo`` / ``--smart-approve`` / ``--approval-policy`` /
  ``--trust`` / ``--add-dir`` are rejected by :func:`build_resume_argv`; a tool call Vibe's own policy
  refuses surfaces as a ``Tool call denied: …`` warning instead of being approved from here.
* subprocess hygiene — argv list only (never ``shell=True``), an absolute binary path resolved like
  ``hermes_cli.codex_runtime_switch.get_configured_codex_binary``, an explicit timeout, the whole
  process group killed on timeout/cancel, stderr captured, and a missing or broken binary degraded to
  a typed :class:`VibeBinaryMissing` / :class:`VibeRunFailed` — never a crash and never a phantom
  session.

The store-level policy this module leaves to ``hermes_cli.mistral_linked_resume``: the per-thread
lock, the native lease probe, and the "did another writer touch the journal" verification.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "DEFAULT_OUTPUT_FORMAT",
    "DEFAULT_TIMEOUT_SECONDS",
    "FORBIDDEN_ARGS",
    "MAX_PROMPT_CHARS",
    "SUPPORTED_OUTPUT_FORMATS",
    "VibeApprovalRequired",
    "VibeBinaryMissing",
    "VibeEntry",
    "VibeOutputUnreadable",
    "VibeResumeError",
    "VibeRun",
    "VibeRunCancelled",
    "VibeRunFailed",
    "VibeRunTimeout",
    "VibeTurnOutput",
    "build_resume_argv",
    "check_vibe_binary",
    "configured_vibe_binary",
    "parse_vibe_output",
    "raise_for_vibe_failure",
    "resolve_vibe_binary",
    "run_vibe_resume",
    "vibe_denials",
]

#: One agentic Vibe turn can run for a while; the caller always passes an explicit bound.
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_OUTPUT_FORMAT = "json"
#: ``json`` emits the turn's entries as one document, ``streaming`` as newline-delimited ones;
#: :func:`parse_vibe_output` accepts either shape for both.
SUPPORTED_OUTPUT_FORMATS = ("json", "streaming")
#: A prompt rides in argv (``-p``) with ``stdin=DEVNULL`` and no fallback path, so the binding limit
#: is the kernel's PER-ARGUMENT cap, not ``ARG_MAX``: Linux refuses any single argv string longer
#: than ``MAX_ARG_STRLEN`` (128 KiB) with E2BIG however much total room is left, and the child's
#: environment plus the rest of this argv come out of the same budget. 96 KiB keeps a 32 KiB margin
#: inside that hard limit; the old 384 KiB passed this check and then died in ``execvp``.
MAX_PROMPT_CHARS = 96 * 1024

_POLL_SLICE_SECONDS = 0.2
_TERM_GRACE_SECONDS = 5.0
_STDERR_TAIL_CHARS = 2_000
_VERSION_PROBE_TIMEOUT = 10.0

_DENIED_RE = re.compile(r"tool call denied", re.IGNORECASE)
_APPROVAL_REQUIRED_RE = re.compile(r"needs approval|approval .*cannot", re.IGNORECASE)

#: Flags a linked resume must never carry: they either pick a different session, or widen the
#: approval/permission policy past the one the user configured for the native tool.
FORBIDDEN_ARGS = frozenset({
    "-c", "--continue", "--setup", "--check-upgrade", "-v", "--version",
    "--auto-approve", "--yolo", "--smart-approve", "--approval-policy",
    "--trust", "--add-dir", "--worktree",
})


# --------------------------------------------------------------------------------------------------
# typed refusals
# --------------------------------------------------------------------------------------------------
class VibeResumeError(RuntimeError):
    """Base for every typed refusal from the Vibe resume transport.

    ``stderr`` carries the captured (capped, secret-redacted) child stderr so a surface can show
    what the native tool said without re-running anything.
    """

    def __init__(self, message: str, *, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


class VibeBinaryMissing(VibeResumeError):
    """The Vibe CLI could not be located or executed: a refusal, never a fresh session."""


class VibeRunFailed(VibeResumeError):
    """The CLI ran and exited non-zero; ``returncode`` says how."""

    def __init__(self, message: str, *, stderr: str = "", returncode: int | None = None) -> None:
        super().__init__(message, stderr=stderr)
        self.returncode = returncode


class VibeApprovalRequired(VibeRunFailed):
    """A headless run hit an approval the native policy would prompt for.

    Programmatic Vibe denies what it cannot ask about; Hermes must not answer for the user, so the
    turn fails here with a message that says how to unblock it.
    """


class VibeRunTimeout(VibeResumeError):
    """The turn exceeded the caller's timeout and the process group was killed."""


class VibeRunCancelled(VibeResumeError):
    """The caller cancelled the turn and the process group was killed."""


class VibeOutputUnreadable(VibeResumeError):
    """The CLI exited zero but printed no parsable message entry."""


# --------------------------------------------------------------------------------------------------
# binary resolution
# --------------------------------------------------------------------------------------------------
def configured_vibe_binary(config: Mapping[str, Any] | None) -> str:
    """``model.vibe_bin`` (one argv element, never shell-parsed) or bare ``vibe``.

    Mirrors ``hermes_cli.codex_runtime_switch.get_configured_codex_binary``: gateway/service
    processes often run with a minimal PATH that lacks the CLI, so the store owner can pin it.
    """
    model_cfg = config.get("model") if isinstance(config, Mapping) else None
    value = model_cfg.get("vibe_bin") if isinstance(model_cfg, Mapping) else None
    return str(value or "").strip() or "vibe"


def resolve_vibe_binary(config: Mapping[str, Any] | None = None, *,
                        which=shutil.which) -> str:
    """Absolute path of the Vibe CLI to spawn, or a typed :class:`VibeBinaryMissing`.

    A configured value must be absolute: a relative path would resolve against whichever cwd the
    Hermes process happens to have. A bare name is looked up on PATH and canonicalized, so argv
    always carries an absolute binary path.
    """
    name = configured_vibe_binary(config)
    expanded = os.path.expanduser(name)
    looks_like_path = os.path.isabs(expanded) or os.sep in expanded or (
        os.altsep is not None and os.altsep in expanded)
    if looks_like_path:
        candidate = Path(expanded)
        if not candidate.is_absolute():
            raise VibeBinaryMissing(
                f"model.vibe_bin must be an absolute path, got {name!r}")
        if not candidate.is_file():
            raise VibeBinaryMissing(
                f"The configured Vibe CLI was not found at {str(candidate)!r}. "
                "Fix model.vibe_bin or install it with: uv tool install mistral-vibe")
        if not os.access(candidate, os.X_OK):
            raise VibeBinaryMissing(f"The configured Vibe CLI at {str(candidate)!r} is not executable")
        return str(candidate)
    found = which(name)
    if not found:
        raise VibeBinaryMissing(
            "The Vibe CLI was not found on PATH. Install it with: uv tool install mistral-vibe "
            "or set model.vibe_bin to its absolute path")
    resolved = Path(found)
    if not resolved.is_absolute():
        resolved = resolved.resolve()
    if not os.access(resolved, os.X_OK):
        raise VibeBinaryMissing(f"Found {name!r} on PATH but {str(resolved)!r} is not executable")
    return str(resolved)


def check_vibe_binary(binary: str | None = None, *, timeout: float = _VERSION_PROBE_TIMEOUT,
                      config: Mapping[str, Any] | None = None) -> tuple[bool, str]:
    """Best-effort ``vibe --version`` probe → ``(ok, version_or_message)`` for surfaces/status."""
    try:
        resolved = binary or resolve_vibe_binary(config)
    except VibeBinaryMissing as exc:
        return False, str(exc)
    try:
        proc = subprocess.run(
            [resolved, "--version"], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, check=False,
            **_hide_flags(),
        )
    except FileNotFoundError:
        return False, f"Vibe CLI not found at {resolved!r}"
    except subprocess.TimeoutExpired:
        return False, "vibe --version timed out"
    except OSError as exc:
        return False, f"vibe --version failed: {exc}"
    if proc.returncode != 0:
        return False, f"vibe --version exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}"
    version = (proc.stdout or "").strip().splitlines()
    return True, (version[0].strip() if version else "")


# --------------------------------------------------------------------------------------------------
# argv
# --------------------------------------------------------------------------------------------------
def build_resume_argv(*, binary: str, prompt: str, session_id: str, workdir: str | Path,
                      output: str = DEFAULT_OUTPUT_FORMAT,
                      extra_args: Sequence[str] = ()) -> list[str]:
    """The exact argv a linked turn spawns: ``vibe -p <prompt> --resume <id> --workdir <cwd> --output``.

    ``-p`` is the headless one-shot, ``--resume`` names the *existing* native session, ``--workdir``
    is the thread's own cwd (a resumed Vibe session keeps working where it was), and the output
    format selects the shape :func:`parse_vibe_output` reads. Nothing here can create a session or
    loosen a permission: relative/path-less binary names and any flag in :data:`FORBIDDEN_ARGS` are
    refused, so a caller cannot smuggle in ``--auto-approve`` or a second ``--resume``.
    """
    if not isinstance(binary, str) or not binary.strip():
        raise ValueError("A resolved Vibe binary path is required")
    if not Path(os.path.expanduser(binary)).is_absolute():
        raise ValueError("The Vibe binary must be an absolute path")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("A prompt is required to resume a Vibe session")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(
            f"The prompt is longer than the {MAX_PROMPT_CHARS} character headless argv budget")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("The native Vibe session ID is required to resume it")
    if "\x00" in session_id or "\n" in session_id:
        raise ValueError("Invalid native Vibe session ID")
    workdir_path = Path(os.path.expanduser(str(workdir)))
    if not workdir_path.is_absolute():
        raise ValueError("The Vibe thread's working directory must be absolute")
    if not workdir_path.is_dir():
        raise ValueError("The Vibe thread's working directory is unavailable")
    if output not in SUPPORTED_OUTPUT_FORMATS:
        raise ValueError(f"Unsupported Vibe output format {output!r}")
    extras = [str(arg) for arg in extra_args]
    for arg in extras:
        flag = arg.split("=", 1)[0]
        if flag in FORBIDDEN_ARGS:
            raise ValueError(f"{flag} must not be passed to a linked Vibe resume")
    return [
        binary,
        "-p", prompt,
        "--resume", session_id,
        "--workdir", str(workdir_path),
        "--output", output,
        *extras,
    ]


# --------------------------------------------------------------------------------------------------
# subprocess
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class VibeRun:
    """One finished headless ``vibe`` invocation, with everything a caller needs to judge it."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    pid: int | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_vibe_resume(*, binary: str, prompt: str, session_id: str, workdir: str | Path,
                    output: str = DEFAULT_OUTPUT_FORMAT,
                    timeout: float = DEFAULT_TIMEOUT_SECONDS,
                    env: Mapping[str, str] | None = None,
                    cancel_event: Any = None,
                    extra_args: Sequence[str] = ()) -> VibeRun:
    """Spawn the resume turn and wait for it, killing the whole process group on timeout/cancel.

    Returns the :class:`VibeRun` for *any* exit code — :func:`raise_for_vibe_failure` turns a
    non-zero one into a typed refusal. Raises :class:`VibeBinaryMissing` when the binary cannot be
    executed at all (a spawn failure must never look like a session), :class:`VibeRunTimeout` /
    :class:`VibeRunCancelled` after killing the child and draining its pipes.
    """
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("An explicit positive timeout is required for a Vibe turn")
    argv = build_resume_argv(binary=binary, prompt=prompt, session_id=session_id,
                             workdir=workdir, output=output, extra_args=extra_args)
    spawn_kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "cwd": str(Path(os.path.expanduser(str(workdir)))),
        "env": dict(env) if env is not None else None,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    spawn_kwargs.update(_spawn_isolation_kwargs())
    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, **spawn_kwargs)  # argv list only: never shell=True
    except FileNotFoundError as exc:
        raise VibeBinaryMissing(
            f"The Vibe CLI at {binary!r} could not be executed") from exc
    except OSError as exc:
        raise VibeResumeError(f"Could not start the Vibe CLI at {binary!r}: {exc}") from exc

    stdout, stderr, timed_out, cancelled = _wait_for(proc, timeout, cancel_event)
    duration = time.monotonic() - started
    if cancelled:
        raise VibeRunCancelled("The linked Vibe turn was cancelled",
                               stderr=_stderr_tail(stderr))
    if timed_out:
        raise VibeRunTimeout(
            f"Vibe did not finish within {timeout:g}s; the turn was stopped",
            stderr=_stderr_tail(stderr))
    return VibeRun(argv=tuple(argv), returncode=int(proc.returncode or 0),
                   stdout=stdout or "", stderr=stderr or "", duration_s=duration,
                   pid=proc.pid)


def _wait_for(proc: subprocess.Popen, timeout: float, cancel_event: Any):
    """Poll ``communicate`` in slices so cancellation is honored while the child runs."""
    deadline = time.monotonic() + float(timeout)
    while True:
        if cancel_event is not None and getattr(cancel_event, "is_set", None) is not None \
                and cancel_event.is_set():
            return (*_terminate(proc), False, True)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return (*_terminate(proc), True, False)
        try:
            stdout, stderr = proc.communicate(timeout=min(_POLL_SLICE_SECONDS, remaining))
            return stdout, stderr, False, False
        except subprocess.TimeoutExpired:
            continue


def _terminate(proc: subprocess.Popen) -> tuple[str, str]:
    """SIGTERM the child's process group, escalate to SIGKILL, and reap it."""
    if proc.poll() is not None:
        stdout, stderr = proc.communicate()
        return stdout or "", stderr or ""
    _signal_group(proc, getattr(signal, "SIGTERM", signal.SIGINT))
    try:
        return proc.communicate(timeout=_TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_group(proc, getattr(signal, "SIGKILL", signal.SIGTERM))
        try:
            return proc.communicate(timeout=_TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
            return "", ""


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    with suppress(ProcessLookupError, PermissionError, OSError, AttributeError):
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), sig)
        elif sig == getattr(signal, "SIGKILL", None):  # pragma: no cover - windows
            proc.kill()
        else:  # pragma: no cover - windows
            proc.terminate()


def _spawn_isolation_kwargs() -> dict[str, Any]:
    """Own process group on POSIX (so a kill takes the tree) / hidden console on Windows."""
    if os.name == "posix":
        return {"start_new_session": True}
    return {"creationflags": _hide_flags().get("creationflags", 0)}  # pragma: no cover - windows


def _hide_flags() -> dict[str, Any]:
    try:
        from hermes_cli._subprocess_compat import windows_hide_flags
        return {"creationflags": windows_hide_flags()}
    except Exception:  # pragma: no cover - optional nicety
        return {}


def _stderr_tail(stderr: str | None) -> str:
    """Capped, redacted child stderr: safe to put in an error a surface may render."""
    if not stderr:
        return ""
    text = stderr.strip()
    if not text:
        return ""
    try:
        from agent.redact import redact_sensitive_text
        text = redact_sensitive_text(text, force=True)
    except Exception:  # health: allow BLE001 -- redaction failures must fail closed
        return "[stderr hidden because redaction failed]"
    if len(text) > _STDERR_TAIL_CHARS:
        text = "…" + text[-_STDERR_TAIL_CHARS:]
    return text


def vibe_denials(stderr: str | None) -> list[str]:
    """``Tool call denied: …`` lines the native policy emitted, capped and redacted.

    Programmatic Vibe never prompts: a call its policy would ask about is denied and reported. That
    is the correct outcome for a linked turn (Hermes must not approve on the user's behalf), but the
    user still needs to see it.
    """
    if not stderr:
        return []
    tail = _stderr_tail(stderr)
    return [line.strip() for line in tail.splitlines() if _DENIED_RE.search(line)][:20]


def raise_for_vibe_failure(run: VibeRun, *, session_id: str) -> VibeRun:
    """Raise the typed failure for a non-zero exit; return ``run`` unchanged when it succeeded."""
    if run.ok:
        return run
    tail = _stderr_tail(run.stderr)
    detail = f" ({tail})" if tail else ""
    if _APPROVAL_REQUIRED_RE.search(run.stderr or ""):
        raise VibeApprovalRequired(
            "Vibe needs an interactive approval for this turn, which a headless resume cannot "
            f"answer; run it in the Vibe CLI or adjust the thread's own approval policy{detail}",
            stderr=tail, returncode=run.returncode)
    raise VibeRunFailed(
        f"The Vibe CLI exited {run.returncode} without appending this turn to session "
        f"{session_id!r}{detail}", stderr=tail, returncode=run.returncode)


# --------------------------------------------------------------------------------------------------
# output parsing
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class VibeEntry:
    """One native message entry, normalized from a Vibe ``--output json``/``streaming`` record."""

    role: str
    text: str
    session_id: str | None = None
    turn_id: str | None = None
    created_at: float | int | None = None
    entry_id: str | None = None


@dataclass(frozen=True)
class VibeTurnOutput:
    """Everything one headless run reported about its turn, in Hermes-shaped rows."""

    entries: tuple[VibeEntry, ...] = ()
    session_ids: tuple[str, ...] = ()
    turn_id: str | None = None
    shape: str = "unknown"  # "json" | "ndjson"

    def rows(self) -> list[dict]:
        """Hermes-shaped ``{"role", "content"}`` rows for the entries the run reported."""
        return [{"role": entry.role, "content": entry.text} for entry in self.entries]

    @property
    def assistant_text(self) -> str:
        return "\n\n".join(e.text for e in self.entries if e.role == "assistant" and e.text)


_ENTRY_ROLES = ("user", "assistant")


def parse_vibe_output(stdout: str) -> VibeTurnOutput:
    """Parse ``vibe -p … --output json|streaming`` stdout into message entries.

    Both formats carry the same entry objects (``sessionId``/``turnId``/``role``/``content[].text``/
    ``createdAt``); ``json`` prints them as one document, ``streaming`` as newline-delimited events.
    The walk is intentionally shape-tolerant — a top-level array, an envelope object with
    ``messages``/``entries``/``history``, a ``projection_delta``-style ``delta[].entry`` list or a
    bare NDJSON stream all yield the same entries — because Vibe's headless envelope is not part of
    its public contract, while the entry fields are.
    """
    if not isinstance(stdout, str) or not stdout.strip():
        raise VibeOutputUnreadable("Vibe printed no output")
    shape = "json"
    documents: list[Any] = []
    try:
        documents.append(json.loads(stdout))
    except ValueError:
        shape = "ndjson"
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                documents.append(json.loads(line))
            except ValueError:
                continue  # a streaming frame that is not JSON is skipped, never fatal
        if not documents:
            raise VibeOutputUnreadable("Vibe printed no parsable JSON entry") from None

    entries: list[VibeEntry] = []
    session_ids: list[str] = []
    seen: set[tuple] = set()
    for document in documents:
        _walk(document, entries, session_ids, seen)
    if not entries:
        raise VibeOutputUnreadable("Vibe returned no message entries")
    if all(entry.created_at is not None for entry in entries):
        entries.sort(key=lambda entry: entry.created_at)
    turn_ids = [entry.turn_id for entry in entries if entry.turn_id]
    return VibeTurnOutput(entries=tuple(entries), session_ids=tuple(dict.fromkeys(session_ids)),
                          turn_id=turn_ids[-1] if turn_ids else None, shape=shape)


def _walk(node: Any, entries: list[VibeEntry], session_ids: list[str],
          seen: set[tuple]) -> None:
    """Collect message entries (and every ``sessionId``) from a decoded Vibe document."""
    if isinstance(node, list):
        for item in node:
            _walk(item, entries, session_ids, seen)
        return
    if not isinstance(node, dict):
        return
    session_id = node.get("sessionId")
    if isinstance(session_id, str) and session_id and session_id not in session_ids:
        session_ids.append(session_id)
    entry = _as_entry(node)
    if entry is not None:
        key = _entry_key(entry)
        if key not in seen:
            seen.add(key)
            entries.append(entry)
    for value in node.values():
        if isinstance(value, (dict, list)):
            _walk(value, entries, session_ids, seen)


def _as_entry(node: Mapping[str, Any]) -> VibeEntry | None:
    role = node.get("role")
    if role not in _ENTRY_ROLES:
        return None
    if "content" not in node and node.get("type") != "message":
        return None
    text = _entry_text(node.get("content"))
    if not text.strip():
        return None
    return VibeEntry(
        role=role,
        text=text,
        session_id=node.get("sessionId") if isinstance(node.get("sessionId"), str) else None,
        turn_id=node.get("turnId") if isinstance(node.get("turnId"), str) else None,
        created_at=_as_timestamp(node.get("createdAt")),
        entry_id=node.get("id") if isinstance(node.get("id"), str) else None,
    )


def _entry_text(content: Any) -> str:
    """Flatten a Vibe ``content`` field: a plain string, or blocks whose text parts we keep."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        if block.get("type") not in (None, "text"):
            continue  # images/attachments never become transcript text
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def _as_timestamp(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _entry_key(entry: VibeEntry) -> tuple:
    if entry.entry_id:
        return ("id", entry.entry_id)
    return ("body", entry.role, entry.turn_id, entry.created_at, entry.text)
