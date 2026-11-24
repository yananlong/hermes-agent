"""Discover and link native Codex threads without importing their transcript.

Codex is the authority for thread identity and history. The Hermes row only stores
a durable pointer; opening a history page never resumes or changes the thread.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from persistence.conversation.sessions import NewSession
from persistence.core.identity import SessionId
from session_rules.session_ids import new_session_id


def _thread_id(value):
    if not isinstance(value, str):
        raise ValueError("A Codex thread ID is required")
    try:
        canonical = str(UUID(value))
    except ValueError:
        raise ValueError("Invalid Codex thread ID") from None
    if value != canonical:
        raise ValueError("Invalid Codex thread ID")
    return canonical


def _page(cursor, limit):
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096):
        raise ValueError("Invalid Codex page cursor")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Invalid Codex page size")
    return {"cursor": cursor, "limit": limit}


def resolve_codex_home(profile_home=None) -> Path:
    """Resolve the Codex store owned by the active profile without starting app-server.

    The account-wide ``~/.codex`` fallback is a launch-profile default only. A routed secondary
    profile must name its own store, otherwise thread IDs and auth state could cross profile scope.
    """
    from agent.secret_scope import (
        current_secret_scope_home, get_secret, is_multiplex_active, serves_routed_profile,
    )
    from hermes_constants import get_hermes_home, get_process_hermes_home, hermes_home_key

    active_home = Path(get_hermes_home()).expanduser()
    target_home = Path(profile_home).expanduser() if profile_home is not None else active_home
    if not target_home.is_absolute() or not active_home.is_absolute():
        raise ValueError("Cannot resolve Codex history without an absolute owning profile")
    try:
        target_key, active_key = hermes_home_key(target_home), hermes_home_key(active_home)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("Cannot resolve the owning profile for Codex history") from exc
    if target_key != active_key or not target_home.is_dir():
        raise ValueError("Codex history request is outside the active owning profile")

    routed = is_multiplex_active() or serves_routed_profile()
    if routed:
        scoped_home = current_secret_scope_home()
        if not scoped_home or hermes_home_key(scoped_home) != target_key:
            raise ValueError("Codex history has no secret scope for the owning profile")

    configured_codex_home = get_secret("CODEX_HOME")
    if isinstance(configured_codex_home, str) and configured_codex_home.strip():
        codex_home = Path(configured_codex_home).expanduser()
    else:
        try:
            is_launch_profile = target_key == hermes_home_key(Path(get_process_hermes_home()).expanduser())
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("Cannot resolve the launch profile for Codex history") from exc
        if routed and not is_launch_profile:
            raise ValueError("The owning profile must configure its own CODEX_HOME for Codex history")
        codex_home = Path.home() / ".codex"
    if not codex_home.is_absolute():
        raise ValueError("CODEX_HOME must resolve to an absolute directory")
    try:
        codex_home = codex_home.resolve(strict=True)
    except OSError as exc:
        raise ValueError("The owning profile's Codex home is unavailable") from exc
    if not codex_home.is_dir():
        raise ValueError("The owning profile's Codex home is unavailable")
    return codex_home


@contextmanager
def _codex_client(*, profile_home=None):
    from agent.transports.codex_app_server import CodexAppServerClient
    from hermes_constants import get_hermes_home, hermes_home_key
    from hermes_cli.codex_runtime_switch import get_configured_codex_binary
    from hermes_cli.config import load_config
    from tools.environments.local import served_profile_child_env

    active_home = Path(get_hermes_home()).expanduser()
    target_home = Path(profile_home).expanduser() if profile_home is not None else active_home
    if not target_home.is_absolute() or not active_home.is_absolute():
        raise ValueError("Cannot resolve Codex history without an absolute owning profile")
    try:
        target_key, active_key = hermes_home_key(target_home), hermes_home_key(active_home)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("Cannot resolve the owning profile for Codex history") from exc
    if target_key != active_key or not target_home.is_dir():
        raise ValueError("Codex history request is outside the active owning profile")

    codex_home = resolve_codex_home(target_home)

    # History reads need the profile's Codex auth/config files, but no Hermes provider key or
    # launch-profile dotenv residue. CODEX_HOME is selected from the active profile scope above,
    # then set explicitly after constructing the target-owned child environment.
    child_env = served_profile_child_env(target_home=target_home, inherit_credentials=False)
    child_env.pop("CODEX_HOME", None)
    with CodexAppServerClient(
        codex_bin=get_configured_codex_binary(load_config()), codex_home=str(codex_home),
        base_env=child_env, inherit_credentials=False, read_only=True,
        # A native history page can contain full tool payloads. Bound its single JSON line before
        # json.loads allocates it; the projector applies tighter per-trace display bounds afterward.
        max_response_line_bytes=32 * 1024 * 1024,
    ) as client:
        client.initialize(client_name="hermes", client_title="Hermes Agent")
        yield client


def list_codex_threads(cursor=None, limit=25):
    params = {**_page(cursor, limit), "sortKey": "updated_at", "sortDirection": "desc",
              "sourceKinds": ["cli", "vscode", "exec", "appServer", "subAgent", "subAgentReview",
                              "subAgentCompact", "subAgentThreadSpawn", "subAgentOther", "unknown"]}
    with _codex_client() as client:
        result = client.request("thread/list", params)
    from hermes_cli.codex_linked_history import _safe_native_text

    rows = []
    for row in result.get("data", []):
        try:
            thread_id = _thread_id(row.get("id"))
        except (AttributeError, ValueError):
            continue
        source = row.get("source")
        if not isinstance(source, str):
            source = None
        raw_title = row.get("name") or "Codex thread"
        title = _safe_native_text(raw_title[:512] if isinstance(raw_title, str) else "Codex thread")[:180]
        # Native previews contain raw assistant replies. There is no owning Hermes turn policy
        # available before a thread is linked, so discovery deliberately exposes no preview text.
        preview = ""
        cwd = row.get("cwd")
        if not isinstance(cwd, str) or len(cwd) > 4096:
            cwd = None
        updated_at = row.get("updatedAt")
        if type(updated_at) is not int or not 0 <= updated_at < 2**63:
            updated_at = None
        rows.append({"id": thread_id, "title": title or "Codex thread",
                     "cwd": cwd, "updated_at": updated_at,
                     "preview": preview, "source": source[:64] if source else None})
    next_cursor = result.get("nextCursor")
    if next_cursor is not None and (not isinstance(next_cursor, str) or len(next_cursor) > 4096):
        raise ValueError("Codex returned an invalid thread cursor")
    return {"threads": rows, "next_cursor": next_cursor}


def read_codex_history(thread_id, cursor=None, limit=50, sort_direction="asc", client=None):
    if sort_direction not in ("asc", "desc"):
        raise ValueError("Invalid Codex history direction")
    params = {**_page(cursor, limit), "threadId": _thread_id(thread_id),
              "sortDirection": sort_direction, "itemsView": "full"}
    if client is not None:
        result = client.request("thread/turns/list", params)
    else:
        with _codex_client() as active_client:
            result = active_client.request("thread/turns/list", params)
    return {"turns": result.get("data", []), "next_cursor": result.get("nextCursor")}


def link_codex_thread(thread_id, store, profile):
    """Create one profile-scoped pointer per native thread, atomically and idempotently."""
    from hermes_cli.codex_linked_history import _safe_native_text

    thread_id = _thread_id(thread_id)
    with _codex_client() as client:
        thread = client.request("thread/read", {"threadId": thread_id}).get("thread") or {}
    if thread.get("id") != thread_id:
        raise ValueError("Codex did not return the requested thread")
    if thread.get("ephemeral"):
        raise ValueError("An ephemeral Codex thread cannot be linked")
    session_id = new_session_id(hex_len=12)
    raw_title = thread.get("name")
    title = _safe_native_text(raw_title[:512] if isinstance(raw_title, str) else "Codex thread")[:180].strip()
    title = title or "Codex thread"
    if title == "Bot Chat":
        title = f"Bot Chat (Codex {thread_id[-6:]})"
    cwd = thread.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise ValueError("The Codex thread's working directory is unavailable")
    config = {"codex_thread_id": thread_id, "codex_linked_thread": True, "api_mode": "codex_app_server"}
    if thread.get("modelProvider") == "openai":
        config["provider"] = "openai"
    model = thread.get("model")
    if model is not None and not isinstance(model, str):
        raise ValueError("Codex returned an invalid model")
    spec = NewSession(session_id=SessionId(session_id), source="desktop", cwd=cwd,
                      model=model, model_config=config, profile_name=profile)
    linked_id, already_linked = store.sessions.link_codex_session(spec, title, thread_id)
    return {"session_id": linked_id, "already_linked": already_linked}
