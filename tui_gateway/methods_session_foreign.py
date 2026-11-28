"""Desktop foreign-history browsing, scoped to the serving backend and profile."""

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method
_profile_scoped = _registry.profile_scoped


@method("session.foreign.list")
def _foreign_list(rid, params):
    from hermes_cli.foreign_sessions_browser import list_foreign_sessions
    try:
        return _ok(rid, list_foreign_sessions(params.get("source"), params.get("offset", 0), params.get("limit", 25)))
    except ValueError as exc:
        return _err(rid, -32602, str(exc))
    except OSError:
        return _err(rid, -32000, "Could not read session folders on this backend")


def _foreign_history_request(rid, params, importing):
    from hermes_cli.foreign_sessions_browser import import_browser_session, preview_foreign_session
    try:
        with _profile_db(params, writer=importing) as db, _persistence_for(db) as persistence:
            if db is None:
                return _db_unavailable_error(rid, code=-32000)
            result = (import_browser_session(params.get("id"), db, _response_profile_name(params.get("profile")),
                                             persistence=persistence)
                      if importing else preview_foreign_session(params.get("id"), db, persistence=persistence))
            return _ok(rid, result)
    except ValueError as exc:
        return _err(rid, -32602, str(exc))
    except OSError:
        return _err(rid, -32000, "Could not read this session on the backend")


@method("session.foreign.preview")
def _foreign_preview(rid, params):
    return _foreign_history_request(rid, params, False)


@method("session.foreign.import")
@_profile_scoped
def _foreign_import(rid, params):
    return _foreign_history_request(rid, params, True)


@method("session.foreign.status")
@_profile_scoped
def _foreign_status(rid, params):
    """Per-agent host capability: native store, CLI and writable-continue verdict on THIS host.

    A Desktop/Webapp client may be connected over the network, so every field answers for the
    serving backend — the client never stats its own machine for a linked thread. Profile-scoped
    like the sibling session.{codex,claude,mistral}.* RPCs: the store, the configured CLI and the
    send admission all resolve inside the requested profile's scope. Additive by design: a client
    that predates this method simply keeps its read-only linked behaviour.
    """
    from hermes_cli.foreign_host_capability import host_capabilities
    probe_version = params.get("probe_version") is True
    return _ok(rid, host_capabilities(profile_home=_profile_home(params.get("profile")),
                                      probe_version=probe_version))


@method("session.codex.list")
@_profile_scoped
def _codex_list(rid, params):
    from agent.transports.codex_app_server import CodexAppServerError
    from hermes_cli.codex_linked_threads import list_codex_threads
    try:
        return _ok(rid, list_codex_threads(params.get("cursor"), params.get("limit") or 25))
    except ValueError as exc:
        return _err(rid, -32602, str(exc))
    except (CodexAppServerError, OSError, TimeoutError):
        return _err(rid, -32000, "Could not list Codex threads")


@method("session.codex.history")
@_profile_scoped
def _codex_history(rid, params):
    from agent.transports.codex_app_server import CodexAppServerError
    from hermes_cli.codex_linked_history import display_native_turn_page
    from hermes_cli.codex_linked_threads import _codex_client, _thread_id
    try:
        session_id, raw_thread_id = params.get("session_id"), params.get("thread_id")
        limit = params.get("limit") if params.get("limit") is not None else 50
        if not isinstance(session_id, str) or not session_id or type(limit) is not int or not 1 <= limit <= 100:
            return _err(rid, -32602, "A linked session and a page limit from 1 to 100 are required")
        thread_id = _thread_id(raw_thread_id)
        with _profile_db(params) as db:
            if db is None:
                return _db_unavailable_error(rid, code=-32000)
            from hermes_cli.codex_linked_history import linked_thread_id
            if linked_thread_id(db, session_id) != thread_id:
                return _err(rid, -32000, "No linked Codex history is available for this session")
            owner_row = db.sessions.get(session_id)
            profile_home = _profile_home(params.get("profile"))
            with _codex_client(profile_home=profile_home) as client:
                result = display_native_turn_page(
                    thread_id, session_id=session_id, model=owner_row.model if owner_row is not None else None,
                    cursor=params.get("cursor"), limit=limit, client=client, session_db=db,
                    platform=owner_row.source if owner_row else "")
        return _ok(rid, result)
    except ValueError as exc:
        return _err(rid, -32602, str(exc))
    except (CodexAppServerError, OSError, TimeoutError):
        return _err(rid, -32000, "Could not read linked Codex history")


@method("session.codex.link")
@_profile_scoped
def _codex_link(rid, params):
    from agent.transports.codex_app_server import CodexAppServerError
    from hermes_cli.codex_linked_threads import link_codex_thread
    try:
        with _profile_db(params, writer=True) as db:
            if db is None:
                return _db_unavailable_error(rid, code=-32000)
            return _ok(rid, link_codex_thread(params.get("thread_id"), db,
                                             _response_profile_name(params.get("profile"))))
    except ValueError as exc:
        return _err(rid, -32602, str(exc))
    except (CodexAppServerError, OSError, TimeoutError):
        return _err(rid, -32000, "Could not link the Codex thread")


def _linked_foreign_list(rid, params, agent):
    from hermes_cli import claude_linked_threads, mistral_linked_threads
    module = {"claude": claude_linked_threads, "mistral": mistral_linked_threads}[agent]
    try:
        lister = module.list_claude_threads if agent == "claude" else module.list_mistral_threads
        return _ok(rid, lister(params.get("cursor"), params.get("limit") or 25,
                               profile_home=_profile_home(params.get("profile"))))
    except ValueError as exc:
        return _err(rid, -32602, str(exc))
    except (OSError, TimeoutError):
        return _err(rid, -32000, f"Could not list {agent} sessions")


def _linked_foreign_history(rid, params, agent):
    from hermes_cli import claude_linked_threads, claude_linked_history
    from hermes_cli import mistral_linked_threads, mistral_linked_history
    threads = {"claude": claude_linked_threads, "mistral": mistral_linked_threads}[agent]
    history = {"claude": claude_linked_history, "mistral": mistral_linked_history}[agent]
    try:
        session_id = params.get("session_id")
        raw_native_id = params.get("session_id_native") or params.get("thread_id")
        limit = params.get("limit") if params.get("limit") is not None else 50
        if not isinstance(session_id, str) or not session_id or type(limit) is not int or not 1 <= limit <= 100:
            return _err(rid, -32602, "A linked session and a page limit from 1 to 100 are required")
        profile_home = _profile_home(params.get("profile"))
        with _profile_db(params) as db:
            if db is None:
                return _db_unavailable_error(rid, code=-32000)
            getter = history.linked_claude_session_id if agent == "claude" else history.linked_mistral_session_id
            native_id = getter(db, session_id)
            if native_id is None:
                return _err(rid, -32000, f"No linked {agent} history is available for this session")
            if raw_native_id is not None and str(raw_native_id) != native_id:
                return _err(rid, -32000, f"No linked {agent} history is available for this session")
            reader = threads.read_claude_history if agent == "claude" else threads.read_mistral_history
            result = reader(native_id, cursor=params.get("cursor"), limit=limit, profile_home=profile_home)
        return _ok(rid, result)
    except ValueError as exc:
        return _err(rid, -32602, str(exc))
    except (OSError, TimeoutError):
        return _err(rid, -32000, f"Could not read linked {agent} history")


def _linked_foreign_link(rid, params, agent):
    from hermes_cli import claude_linked_threads, mistral_linked_threads
    module = {"claude": claude_linked_threads, "mistral": mistral_linked_threads}[agent]
    try:
        with _profile_db(params, writer=True) as db:
            if db is None:
                return _db_unavailable_error(rid, code=-32000)
            linker = module.link_claude_thread if agent == "claude" else module.link_mistral_thread
            return _ok(rid, linker(params.get("session_id") or params.get("thread_id"), db,
                                   _response_profile_name(params.get("profile")),
                                   profile_home=_profile_home(params.get("profile"))))
    except ValueError as exc:
        return _err(rid, -32602, str(exc))
    except (OSError, TimeoutError):
        return _err(rid, -32000, f"Could not link the {agent} session")


@method("session.claude.list")
@_profile_scoped
def _claude_list(rid, params):
    return _linked_foreign_list(rid, params, "claude")


@method("session.claude.history")
@_profile_scoped
def _claude_history(rid, params):
    return _linked_foreign_history(rid, params, "claude")


@method("session.claude.link")
@_profile_scoped
def _claude_link(rid, params):
    return _linked_foreign_link(rid, params, "claude")


@method("session.mistral.list")
@_profile_scoped
def _mistral_list(rid, params):
    return _linked_foreign_list(rid, params, "mistral")


@method("session.mistral.history")
@_profile_scoped
def _mistral_history(rid, params):
    return _linked_foreign_history(rid, params, "mistral")


@method("session.mistral.link")
@_profile_scoped
def _mistral_link(rid, params):
    return _linked_foreign_link(rid, params, "mistral")


def register(server):
    bind_module(globals(), server)
