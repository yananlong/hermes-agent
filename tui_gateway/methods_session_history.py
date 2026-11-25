"""Session history read handler shared by the gateway's profile-aware dispatch."""

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()

# Display names for the linked foreign agents, used in log and error messages.
_LINKED_AGENT_LABELS = {"codex": "Codex", "claude": "Claude", "mistral": "Mistral"}
# Where the user must change the model for each linked agent: a linked row's model belongs to the
# native tool's own thread, so Hermes can only report it, never override it.
_LINKED_AGENT_MODEL_TOOL = {"codex": "Codex", "claude": "Claude Code", "mistral": "Mistral Vibe"}

#: ``session.history`` / ``session.resume`` refusal for a row whose stored pointer cannot be read.
#: Distinct from ``-32603`` ("internal error"), which is what an uncaught read became: the store is
#: healthy and the server is fine — THIS row's configuration is unusable, and the user's fix is to
#: re-link the session. ``data.reason`` is the stable wire reason, as the 4090 refusals carry theirs.
LINKED_CONFIG_UNREADABLE_CODE = 4095
LINKED_CONFIG_UNREADABLE_REASON = "LINKED_CONFIG_UNREADABLE"


def _linked_config_unreadable_error(rid) -> dict:
    """The typed refusal for a linked row whose ``model_config`` cannot be read or validated."""
    return _err(
        rid, LINKED_CONFIG_UNREADABLE_CODE,
        "This session's linked-thread configuration cannot be read — re-link the session from the "
        "foreign session list",
        {"reason": LINKED_CONFIG_UNREADABLE_REASON},
    )


def _linked_foreign_target(store, key: str):
    """``(agent, native_id)`` when the row is a linked foreign thread pointer, else ``None``.

    Codex, Claude and Mistral linked rows all store a durable native identity in
    ``model_config``; the native tool owns the transcript and Hermes only points at it.
    """
    from hermes_cli.codex_linked_history import linked_thread_id
    from hermes_cli.claude_linked_history import linked_claude_session_id
    from hermes_cli.mistral_linked_history import linked_mistral_session_id
    if thread_id := linked_thread_id(store, key):
        return "codex", thread_id
    if native_id := linked_claude_session_id(store, key):
        return "claude", native_id
    if native_id := linked_mistral_session_id(store, key):
        return "mistral", native_id
    return None


def _read_linked_foreign_full(agent: str, native_id: str, *, profile_home=None) -> list[dict]:
    """Read the FULL native transcript of a linked Claude/Mistral session as display rows.

    ``read_claude_history``/``read_mistral_history`` return one paged dict (max 100 rows per
    page), so loop the cursor until it ends; fail closed on a broken cursor instead of spinning.
    The rows are already ``{role, content, ...}`` display dicts, the same shape Codex's
    ``read_display_history`` flattens to, so ``_history_to_messages`` consumes them directly.
    """
    if agent == "claude":
        from hermes_cli.claude_linked_threads import read_claude_history as read_page
    elif agent == "mistral":
        from hermes_cli.mistral_linked_threads import read_mistral_history as read_page
    else:
        raise ValueError(f"Unknown linked foreign agent: {agent}")
    rows, cursor, seen = [], None, set()
    while True:
        page = read_page(native_id, cursor=cursor, limit=100, profile_home=profile_home)
        messages = page.get("messages") if isinstance(page, dict) else None
        if not isinstance(messages, list):
            raise ValueError(f"Linked {agent} history returned an invalid page")
        rows.extend(row for row in messages if isinstance(row, dict))
        cursor = page.get("next_cursor")
        if cursor is None:
            return rows
        if not isinstance(cursor, str) or cursor in seen:
            raise ValueError(f"Linked {agent} history returned an invalid cursor")
        seen.add(cursor)


def _read_linked_display(agent: str, native_id: str, *, session_id: str, model=None,
                         profile_home=None, session_db=None, platform: str = "") -> list[dict]:
    """Native display rows for any linked foreign session, in the transcript row shape."""
    if agent == "codex":
        from gateway.session_linked_codex import read_display_history
        return read_display_history(
            native_id, session_id=session_id, model=model, profile_home=profile_home,
            session_db=session_db, platform=platform)
    return _read_linked_foreign_full(agent, native_id, profile_home=profile_home)


def read_resume_history(resume, profile_scope) -> tuple:
    """Read resume projections and retire queue residue left by the previous process."""
    db, target = resume.persistence, resume.target
    db.sessions.retire_undrained_queue_rows(target)
    if resume.omit_messages:
        return resume.child_history(repair=True), []
    linked = _linked_foreign_target(db, target)
    if linked is not None:
        # A linked native thread owns model context; its safe projection owns display history.
        agent, native_id = linked
        owner_row = db.sessions.get(target)
        try:
            with profile_scope({"profile_home": resume.profile_home}):
                display = _read_linked_display(
                    agent, native_id, session_id=target,
                    model=owner_row.model if owner_row is not None else None,
                    profile_home=resume.profile_home, session_db=db,
                    platform=owner_row.source if owner_row else "")
        except Exception:
            logger.warning("Unable to load linked %s history for resume",
                           _LINKED_AGENT_LABELS[agent], exc_info=True)
            stored = db.messages.read_resume_conversations(target)
            return [m.as_openai_message() for m in stored.model], [
                m.as_openai_message() for m in stored.display]
        return resume.child_history(repair=True), display
    stored = db.messages.read_resume_conversations(target)
    return [message.as_openai_message() for message in stored.model], [
        message.as_openai_message() for message in stored.display]


def _session_linked_foreign_agent(session: dict) -> str | None:
    """Which linked foreign agent (codex/claude/mistral) owns this session's row, if any."""
    key = session.get("session_key")
    if not key:
        return None
    with _session_db(session) as db:
        if db is None:
            raise RuntimeError("session database is unavailable")
        linked = _linked_foreign_target(db, key)
        return linked[0] if linked is not None else None


def _session_is_codex_linked(session: dict) -> bool:
    # Any linked foreign thread (Codex, Claude, Mistral) is a pointer row: its transcript lives in
    # the native tool's store. Appending is a capability of the HOST (``session.foreign.status``),
    # not of this predicate — a linked row is still a linked row when a send is possible.
    return _session_linked_foreign_agent(session) is not None


def _refuse_linked_model_switch(session: dict) -> None:
    agent = _session_linked_foreign_agent(session)
    if agent is None:
        return
    noun = "thread" if agent == "codex" else "session"
    raise ValueError(
        f"Change the model in {_LINKED_AGENT_MODEL_TOOL[agent]} before opening this linked {noun}")


def _linked_compression_result(rid, session: dict, agent, usage: dict) -> dict:
    from agent.conversation_compression import finalize_context_engine_compression_notification
    owner_session_id = _submit_row_target_key(session)
    with _session_db(session) as db:
        linked = _linked_foreign_target(db, owner_session_id)
        linked_agent, native_id = linked if linked is not None else ("codex", None)
        label = _LINKED_AGENT_LABELS[linked_agent]
        owner_row = db.sessions.get(owner_session_id)
        try:
            with _session_profile_runtime_scope(session):
                display = _read_linked_display(
                    linked_agent, native_id, session_id=owner_session_id,
                    model=owner_row.model if owner_row is not None else None,
                    profile_home=session.get("profile_home"),
                    session_db=db, platform=owner_row.source if owner_row else "")
            rendered = _history_to_messages(display, profile_home=session.get("profile_home"))
        except Exception:
            logger.warning("%s compacted but its history could not be refreshed", label, exc_info=True)
            rendered = None
    finalize_context_engine_compression_notification(agent, committed=True)
    noun = "thread" if linked_agent == "codex" else "session"
    return _ok(rid, {"status": "compressed", "message": f"{label} {noun} compressed.",
                     "usage": usage, "info": _session_info(agent, session), "messages": rendered})


def _session_history_result(rid, session: dict) -> dict:
    history = list(session.get("history", []))
    # Address the live writer's tip after compression, so resumed clients also see
    # rows written since the rotation.
    row_session = _submit_row_target_key(session)
    if row_session:
        with _session_db(session) as db:
            if db is not None:
                try:
                    linked = _linked_foreign_target(db, row_session)
                except ValueError:
                    # An unreadable pointer is a typed refusal, never a -32603 internal error.
                    logger.warning("Linked session row %s has an unreadable configuration", row_session)
                    return _linked_config_unreadable_error(rid)
                if linked is not None:
                    agent, native_id = linked
                    label = _LINKED_AGENT_LABELS[agent]
                    owner_row = db.sessions.get(row_session)
                    try:
                        with _session_profile_runtime_scope(session):
                            history = _read_linked_display(
                                agent, native_id, session_id=row_session,
                                model=owner_row.model if owner_row is not None else None,
                                profile_home=session.get("profile_home"), session_db=db,
                                platform=owner_row.source if owner_row else "")
                    except Exception:
                        logger.warning("Unable to load linked %s history", label, exc_info=True)
                        return _err(rid, 5000, f"Unable to load linked {label} history")
                    return _ok(rid, {"count": len(history), "messages": _history_to_messages(
                        history, profile_home=session.get("profile_home"))})
                with contextlib.suppress(Exception):
                    from persistence.conversation.messages import ConversationRequest
                    options = ConversationRequest(include_lineage=True, include_row_ids=True)
                    history = [row.as_openai_message() for row in db.messages.read_conversation(row_session, options)]
    return _ok(rid, {"count": len(history), "messages": _history_to_messages(
        history, profile_home=session.get("profile_home"))})


def register(server):
    bind_module(globals(), server)
