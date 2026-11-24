"""Stored session workspace mutation handlers."""

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method


def _find_live_target(target: str) -> tuple[str, dict | None]:
    with _sessions_lock:
        return next(
            ((sid, session) for sid, session in list(_sessions.items())
             if session.get("session_key") == target),
            ("", None),
        )


def _move_linked_workspace_once(rid, target: str, target_cwd: str, home, live: dict | None, db):
    from tui_gateway.session_workdir import (
        LinkedCodexWorkspaceBusy, LinkedCodexWorkspaceError, LinkedCodexWorkspacePersistError,
    )

    try:
        moved = _move_linked_codex_workspace(
            db, target, target_cwd, home, live_session=live,
        )
    except LinkedCodexWorkspaceBusy as exc:
        return None, _err(rid, 4009, str(exc))
    except LinkedCodexWorkspacePersistError as exc:
        return None, _err(rid, 5007, str(exc))
    except (LinkedCodexWorkspaceError, ValueError) as exc:
        return None, _err(rid, 4017, str(exc))
    return moved, None


def _move_linked_workspace(rid, target: str, target_cwd: str, home, live_sid: str, live: dict | None, db):
    if live is None:
        moved, refusal = _move_linked_workspace_once(rid, target, target_cwd, home, None, db)
    else:
        lock = live.get("history_lock")
        if lock is None:
            return _err(rid, 5000, "session lock unavailable")
        with lock:
            if live.get("running"):
                return _err(rid, 4009, "session busy")
            moved, refusal = _move_linked_workspace_once(rid, target, target_cwd, home, live, db)
    if refusal:
        return refusal
    if live is not None:
        _emit("session.info", live_sid, _cwd_info(live, moved["cwd"], branch=moved["branch"]))
    return _ok(rid, moved)


def _move_regular_workspace(rid, target: str, target_cwd: str, live_sid: str,
                            live: dict | None, db, branch: str, root: str):
    if not db.sessions.get(target):
        if live is None:
            return _err(rid, 4007, "session not found")
    else:
        try:
            db.sessions.update_cwd(target, target_cwd, branch, root, replace_git_meta=True)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.exception("session.workspace.move: stored row update failed")
            return _err(rid, 5007, f"move failed: {exc}")
    if live is not None:
        try:
            _set_session_cwd(live, target_cwd)
        except ValueError as exc:
            return _err(rid, 4017, str(exc))
        _emit("session.info", live_sid, _cwd_info(live, target_cwd, branch=branch))
    return _ok(rid, {"cwd": target_cwd, "branch": branch, "git_repo_root": root})


@method("session.workspace.move")
def _(rid, params: dict) -> dict:
    """Re-home a stored session's workspace, using native same-thread resume for linked rows."""
    if not (target := _str_param(params, "session_key")):
        return _err(rid, 4007, "session_key required")
    if not (raw := _str_param(params, "cwd")):
        return _err(rid, 4016, "cwd required")
    from hermes_constants import translate_cwd_for_wsl_backend

    live_sid, live = _find_live_target(target)
    home = live.get("profile_home") if live is not None else _profile_home(params.get("profile"))
    try:
        target_cwd = _workspace_cwd(home, translate_cwd_for_wsl_backend(raw))
    except ValueError:
        return _err(rid, 4017, f"working directory does not exist: {raw}")
    branch, root = git_probe.branch(target_cwd), git_probe.common_repo_root(target_cwd)
    with _profile_db(params, writer=True) as db:
        if db is None:
            return _db_unavailable_error(rid, code=5007)
        row = db.sessions.get(target)
        if row:
            # A linked row's working directory is part of the native thread, so moving it is a
            # native operation. Codex can re-home its thread in place (session_linked_codex), which
            # is why it keeps its own path here; Claude Code and Vibe carry the cwd in their own
            # transcript/meta store, so a Hermes-side move would only make the row disagree with
            # the thread the next send resumes.
            from hermes_cli.foreign_linked_store import linked_foreign_kind, linked_foreign_label

            if (kind := linked_foreign_kind(db, target)) is not None:
                if kind != "codex":
                    return _err(
                        rid, 4008,
                        "Moving the workspace of a linked "
                        f"{linked_foreign_label(kind)} thread is not supported — its working "
                        f"directory belongs to {linked_foreign_label(kind)}")
                return _move_linked_workspace(rid, target, target_cwd, home, live_sid, live, db)
        return _move_regular_workspace(rid, target, target_cwd, live_sid, live, db, branch, root)


def register(server) -> None:
    bind_module(globals(), server, skip=("_",))
