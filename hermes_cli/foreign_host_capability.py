"""Host capability for linked foreign sessions (Codex / Claude Code / Mistral Vibe).

A linked session is a pointer row whose transcript lives in the native tool's own store **on the
gateway host**. A Desktop (Electron), Webapp or browser client may be connected to that gateway
over the network, so every question answered here is asked of the SERVING host and never of the
client's machine:

* does the native session store exist there, and can this gateway read it;
* does the native CLI resolve (``codex`` / ``claude`` / ``vibe``) — the token is the profile's
  configured ``model.*_bin`` when set, else the bare command from the profile's PATH;
* can a turn be APPENDED to a linked native thread from here (writable capability), and if not,
  why — the one-line reason a client renders instead of a broken affordance.

Served by ``session.foreign.status`` (``tui_gateway/methods_session_foreign.py``), profile-scoped
exactly like the sibling ``session.{codex,claude,mistral}.{list,history,link}`` RPCs. Clients that
predate the method, or gateways that predate the client, fall back to the read-only linked
behaviour — this module is additive, never required to open a transcript.

Read-only with respect to other tools' data: the store probe resolves the root, ``stat``s it and
reads at most one directory entry to prove enumerability. It never opens a transcript, never
writes, and never returns a raw host path — ``cli.path`` is ``~``-collapsed like nothing the
existing foreign RPCs return, and reasons are path-free sentences.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import socket
import subprocess
from pathlib import Path

from hermes_cli.linked_send_admission import AGENT_LABELS, agent_label, linked_send_refusal

AGENTS = ("codex", "claude", "mistral")

#: ``continue_status.reason_code`` values. The client keys its copy on these, so a new refusal
#: category is a contract change rather than a free-text guess about an English sentence.
REASON_STORE_MISSING = "store_missing"
REASON_STORE_UNREADABLE = "store_unreadable"
REASON_PROFILE_SCOPE = "profile_scope"
REASON_CLI_MISSING = "cli_missing"
REASON_BUILD_UNSUPPORTED = "build_unsupported"
REASON_SEND_READ_ONLY = "send_read_only"

#: The documented turn runner each linked adapter ships. Presence of THAT callable is the build
#: capability question: a transport module alone (argv building, output parsing) cannot append a
#: turn, so module presence alone would over-report.
_APPEND_RUNNERS = {
    "claude": ("hermes_cli.claude_linked_resume", "run_linked_claude_turn"),
    "mistral": ("hermes_cli.mistral_linked_resume", "run_linked_vibe_turn"),
}

#: ``(module, function)`` that resolves each agent's CLI the way its transport will spawn it.
_TRANSPORT_RESOLVERS = {
    "claude": ("agent.transports.claude_cli_resume", "resolve_claude_binary"),
    "mistral": ("agent.transports.vibe_cli_resume", "resolve_vibe_binary"),
}

_VERSION_PROBE_TIMEOUT = 3.0
_VERSION_MAX_CHARS = 80
_VERSION_SAFE = re.compile(r"[^A-Za-z0-9 ._+()\[\]/-]")


def host_label() -> str:
    """The serving host as a display label (same source as ``session.foreign.list.host``)."""
    try:
        return socket.gethostname() or "this gateway host"
    except OSError:  # pragma: no cover - gethostname failure is a broken container
        return "this gateway host"


def _resolve_store(agent: str, profile_home) -> Path:
    """The native session store root for *agent*, scoped to the owning profile."""
    if agent == "codex":
        from hermes_cli.codex_linked_threads import resolve_codex_home

        return resolve_codex_home(profile_home)
    if agent == "claude":
        from hermes_cli.claude_linked_threads import resolve_claude_projects

        return resolve_claude_projects(profile_home)
    from hermes_cli.mistral_linked_threads import resolve_vibe_session_root

    return resolve_vibe_session_root(profile_home)


def store_capability(agent: str, profile_home=None, *, host: str | None = None) -> dict:
    """Whether *agent*'s native store exists on this host and this gateway can enumerate it.

    ``available`` is "the directory exists"; ``readable`` additionally proves enumeration with a
    single ``scandir`` step, so a store owned by another user (or a mode-000 mount) reports the
    truth instead of failing later inside a list/history call. A profile-scope refusal is
    classified BEFORE the store lookup (``check_owning_profile`` repeats the scope rules every
    resolver applies) so a routed profile that never configured its own store reads as
    ``profile_scope`` rather than as a host without the tool.
    """
    label, host = agent_label(agent), host or host_label()
    from hermes_cli.foreign_linked_store import check_owning_profile

    try:
        check_owning_profile(profile_home)
    except (OSError, RuntimeError, ValueError):
        return {"available": False, "readable": False, "reason_code": REASON_PROFILE_SCOPE,
                "reason": f"{label}'s session store is not configured for this profile on {host}."}
    try:
        root = _resolve_store(agent, profile_home)
    except (OSError, RuntimeError, ValueError):
        # Resolvers raise path-free ValueErrors for a missing or relocated store, which is what
        # the client needs: no native store can be served from here.
        return {"available": False, "readable": False, "reason_code": REASON_STORE_MISSING,
                "reason": f"{label} has no session store on {host}."}
    try:
        with os.scandir(root) as entries:
            # One entry is enough to prove enumerability; a big store must not be walked here.
            next(entries, None)
    except OSError:
        return {"available": True, "readable": False, "reason_code": REASON_STORE_UNREADABLE,
                "reason": f"{label} session store on {host} is not readable by the gateway."}
    return {"available": True, "readable": True, "reason_code": None, "reason": None}


def _display_path(path: Path) -> str:
    """``~``-collapsed path: enough to recognize an install, never a home-directory disclosure."""
    try:
        return f"~/{path.relative_to(Path.home()).as_posix()}"
    except ValueError:
        return str(path)


def _command_display(token: str) -> str:
    """Display form of the spawn token: an absolute path collapses to its basename."""
    return Path(token).name if os.sep in token else token


def _probe_version(binary: str, timeout: float = _VERSION_PROBE_TIMEOUT) -> str | None:
    """Bounded ``--version`` for a resolved CLI: a short, path-free token, or ``None``.

    Opt-in only (``probe_version``): spawning three CLIs costs more than every other part of the
    probe combined, and a status surface that runs on mount must stay a stat/``which``.
    """
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True,
                              timeout=max(0.5, float(timeout)), check=False,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    for stream in (proc.stdout, proc.stderr):
        line = next((raw.strip() for raw in (stream or "").splitlines() if raw.strip()), "")
        if line:
            return _VERSION_SAFE.sub("", line)[:_VERSION_MAX_CHARS].strip() or None
    return None


def _model_bin_key(config, key: str) -> str:
    """``model.<key>`` when the profile pinned its CLI there, else ``""`` (configured override)."""
    model_cfg = config.get("model") if isinstance(config, dict) else None
    value = model_cfg.get(key) if isinstance(model_cfg, dict) else None
    return str(value or "").strip()


def _resolve_token(token: str, path_env: str | None) -> Path | None:
    """Resolve the argv[0] this gateway would spawn: an absolute file, or a PATH lookup."""
    if os.sep in token or (os.altsep and os.altsep in token):
        candidate = Path(token).expanduser()
        # A relative configured path resolves against whatever cwd the gateway happens to have,
        # which this probe cannot reproduce: report it unresolved rather than guess.
        if candidate.is_absolute() and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        return None
    found = shutil.which(token, path=path_env)
    return Path(found) if found else None


def _transport_binary(agent: str, config) -> Path | None:
    """The transport's own resolver result for *agent*, when this build ships one.

    Claude Code and Vibe each own the install-prefix fallbacks their spawn path uses
    (``resolve_claude_binary`` / ``resolve_vibe_binary``), so asking them keeps this probe from
    disagreeing with the process the gateway would actually start. Codex needs no such help:
    its token comes from :func:`get_configured_codex_binary` and is resolved here.
    """
    spec = _TRANSPORT_RESOLVERS.get(agent)
    if spec is None:
        return None
    module_name, getter_name = spec
    try:
        module = importlib.import_module(module_name)
        resolved = getattr(module, getter_name)(config if isinstance(config, dict) else {})
    except Exception:  # a transport mid-change, a typed "missing binary", or no install at all
        return None
    candidate = Path(str(resolved)).expanduser()
    return candidate if candidate.is_absolute() and candidate.is_file() else None


def cli_capability(agent: str, *, config=None, probe_version: bool = False,
                   host: str | None = None, env: dict | None = None) -> dict:
    """Whether *agent*'s CLI resolves on this host, and its display path / version when cheap.

    ``command``/``path`` never widen what a client already learns from the foreign RPCs: an
    absolute configured binary collapses to its basename, the resolved path is ``~``-collapsed,
    and ``version`` (opt-in) is reduced to a short token.
    """
    label, host = agent_label(agent), host or host_label()
    key = "codex_bin" if agent == "codex" else ("claude_bin" if agent == "claude" else "vibe_bin")
    configured = _model_bin_key(config, key)
    if agent == "codex":
        from hermes_cli.codex_runtime_switch import get_configured_codex_binary

        token = get_configured_codex_binary(config if isinstance(config, dict) else {})
    else:
        token = configured or ("claude" if agent == "claude" else "vibe")
    resolved = _transport_binary(agent, config)
    if resolved is None:
        resolved = _resolve_token(token, (env if env is not None else os.environ).get("PATH"))
    display = _command_display(token)
    if resolved is None:
        reason = f"{label} is not installed on {host}."
        if os.sep in token:
            reason = f"{label} is not runnable on {host}: its configured binary is missing."
        return {"available": False, "command": display, "path": None, "version": None,
                "source": "config" if configured else "path",
                "reason_code": REASON_CLI_MISSING, "reason": reason}
    return {"available": True, "command": display, "path": _display_path(resolved),
            "version": _probe_version(str(resolved)) if probe_version else None,
            "source": "config" if configured else "path", "reason_code": None, "reason": None}


def append_supported(agent: str) -> bool:
    """Does THIS gateway build have a code path that appends a turn to a linked *agent* thread?

    Codex always does: the app-server session grows the thread with ``turn/start`` +
    ``resume_thread_id`` (``agent/transports/codex_app_server_session.py``). Claude Code and Vibe
    are separate transports that a build may not ship, so their presence is probed by module —
    never by importing them, which would load a CLI transport just to answer a status question.
    """
    if agent == "codex":
        try:
            import inspect

            from agent.transports.codex_app_server_session import CodexAppServerSession

            return "resume_thread_id" in inspect.signature(CodexAppServerSession.__init__).parameters
        except Exception:  # pragma: no cover - an unimportable transport cannot append either
            return False
    spec = _APPEND_RUNNERS.get(agent)
    if spec is None:
        return False
    module_name, runner_name = spec
    try:
        return callable(getattr(importlib.import_module(module_name), runner_name, None))
    except Exception:  # pragma: no cover - an unimportable adapter cannot append either
        return False


def continue_capability(agent: str, *, store: dict, cli: dict) -> dict:
    """Whether a send from this host would append to the native thread, or why it would not.

    ``supported`` is the build question (the append path exists); ``writable`` is the end-to-end
    one this client acts on: the build can append, the store is readable here, the CLI resolves
    here, and the send admission (``hermes_cli/linked_send_admission.py``) accepts the row. The
    first failing condition supplies the reason, so the UI never has to guess.
    """
    label = agent_label(agent)
    if not append_supported(agent):
        return {"supported": False, "writable": False, "reason_code": REASON_BUILD_UNSUPPORTED,
                "reason": f"This gateway cannot continue (append to) {label} threads."}
    if not store["readable"]:
        return {"supported": True, "writable": False, "reason_code": store["reason_code"],
                "reason": store["reason"]}
    if not cli["available"]:
        return {"supported": True, "writable": False, "reason_code": cli["reason_code"],
                "reason": cli["reason"]}
    refusal = linked_send_refusal(agent)
    if refusal is not None:
        return {"supported": True, "writable": False, "reason_code": REASON_SEND_READ_ONLY,
                "reason": f"Sending is read-only in this Hermes build — it cannot append to "
                          f"linked {label} threads yet."}
    return {"supported": True, "writable": True, "reason_code": None, "reason": None}


def agent_capability(agent: str, *, profile_home=None, config=None, probe_version: bool = False,
                     host: str | None = None) -> dict:
    """One agent's ``session.foreign.status`` row (store + CLI + writable verdict)."""
    host = host or host_label()
    store = store_capability(agent, profile_home, host=host)
    cli = cli_capability(agent, config=config, probe_version=probe_version, host=host)
    return {"agent": agent, "label": AGENT_LABELS.get(agent, agent), "store": store, "cli": cli,
            "continue_status": continue_capability(agent, store=store, cli=cli)}


def host_capabilities(*, profile_home=None, config=None, probe_version: bool = False,
                      host: str | None = None) -> dict:
    """``session.foreign.status`` result: every linked agent evaluated on THIS host.

    Never raises for a per-agent problem — a host without Claude Code still answers, with that
    agent's reason filled in — so a client can render all three rows from one call. ``config``
    defaults to the profile's own ``config.yaml`` (the scope ``_profile_scoped`` bound).
    """
    host = host or host_label()
    if config is None:
        from hermes_cli.config import load_config

        try:
            config = load_config()
        except Exception:  # pragma: no cover - a broken config still reports stores/CLIs
            config = {}
    return {"host": host,
            "agents": [agent_capability(agent, profile_home=profile_home, config=config,
                                        probe_version=probe_version, host=host)
                       for agent in AGENTS]}
