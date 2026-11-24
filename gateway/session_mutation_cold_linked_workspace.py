"""Canonical workspace changes for a linked Codex row before Hermes adopts it.

An imported/linked row has no local execution policy until ``session.resume`` adopts it. A
workspace pick on that row updates its durable cwd so that adoption starts in the selected
directory; it must not invent a policy or a Hermes session merely to make the mutation fit the
ordinary local-policy path.
"""

import os
from gateway.session_persistence import authority_persistence, owner_rule
from persistence.authority.runtime import ColdLinkedWorkspaceRequest
from persistence.core.errors import AuthorityRefusal


def _unopened_workspace_target(server, payload, profile_home):
    if server._cwd_is_remote(profile_home):
        raise AuthorityRefusal('unsupported_operation', field='workspace',
                                detail='linked Codex workspaces must be local to the gateway')
    from hermes_constants import translate_cwd_for_wsl_backend
    candidate = os.path.abspath(os.path.expanduser(translate_cwd_for_wsl_backend(payload['cwd'])))
    if not os.path.isdir(candidate):
        raise AuthorityRefusal('invalid_params', field='payload.cwd')
    try:
        return server._linked_codex_local_cwd(candidate, profile_home)
    except server.LinkedCodexWorkspaceError as exc:
        raise AuthorityRefusal('invalid_params', field='payload.cwd') from exc


def commit_unopened_linked_workspace(authority, request: ColdLinkedWorkspaceRequest, history):
    """Move an unopened linked row using the native thread writer lease and a canonical receipt.

    The durable ``codex_pending_cwd`` intent prevents local legacy adoption from racing the native
    resume. If Codex changes cwd but SQLite cannot commit, an exact retry reads native cwd under
    the same per-thread lock and finishes the original receipt. If Codex has moved to a third
    directory, the intent stays pending: restore the native thread to its recorded original or
    requested directory and retry this exact mutation. A different request cannot replace that
    intent, and there is no separate mutation-status endpoint that can safely resolve it.
    """
    from agent.transports.codex_app_server_session import CodexThreadResumeError
    from gateway.session_linked_codex import linked_thread_id
    from tui_gateway import server

    store = authority_persistence(authority)
    sid = request.session_id
    thread_id = linked_thread_id(store, sid)
    if not thread_id:
        raise AuthorityRefusal('revision_conflict', field='workspace', detail='linked thread identity changed')
    profile_home = store.scope.profile_dir

    def advance(**kwargs):
        authority._require_admission_open()
        try:
            return store.runtime.mutate_cold_linked_workspace(
                authority.epoch, request, thread_id=thread_id, history=history, owner=owner_rule(), **kwargs)
        except OSError as exc:
            raise AuthorityRefusal('storage_unavailable', field='workspace',
                                   detail='workspace change needs recovery; retry this request') from exc

    receipt = advance()
    if 'replay' in receipt:
        return receipt['replay']
    target = _unopened_workspace_target(server, {'cwd': request.cwd}, profile_home)

    def commit_intent(intent, effective):
        if os.path.realpath(effective) != os.path.realpath(target):
            raise AuthorityRefusal('revision_conflict', field='workspace',
                                   detail='Codex reported a different working directory')
        return advance(target_cwd=target, intent=intent, branch=server.git_probe.branch(target) or None,
                       repo_root=server.git_probe.common_repo_root(target) or None)['replay']

    def effective_local_cwd(native):
        effective = getattr(native, 'effective_cwd', None)
        if not server._is_absolute_host_path(effective):
            raise AuthorityRefusal('storage_unavailable', field='workspace',
                                    detail='Codex did not report a local absolute workspace')
        return server._linked_codex_local_cwd(effective, profile_home)

    try:
        with server._linked_codex_workspace_operation_lock(store, thread_id):
            if linked_thread_id(store, sid) != thread_id:
                raise AuthorityRefusal('revision_conflict', field='workspace',
                                        detail='linked thread identity changed')
            claim = advance(target_cwd=target)
            if 'replay' in claim:
                return claim['replay']
            intent, recovering = claim['intent'], claim['recovery']
            if recovering:
                with server._linked_codex_resume(thread_id, target, profile_home,
                                                 override=False, accept_native=True) as native:
                    effective = effective_local_cwd(native)
                    if os.path.realpath(effective) == os.path.realpath(target):
                        return commit_intent(intent, effective)
                    if os.path.realpath(effective) != os.path.realpath(intent['from']):
                        raise AuthorityRefusal('revision_conflict', field='workspace',
                            detail=('Codex moved elsewhere. Restore its original or requested folder and retry this '
                                    'exact move; choose a new folder only after it completes.'))
                server._linked_codex_clear_intent(store, sid, intent, intent['from'])
                claim = advance(target_cwd=target)
                if 'replay' in claim:
                    return claim['replay']
                intent, recovering = claim['intent'], claim['recovery']
                if recovering:
                    raise AuthorityRefusal('revision_conflict', field='workspace',
                                            detail='workspace recovery could not be restarted')
            try:
                with server._linked_codex_resume(thread_id, target, profile_home, override=True) as native:
                    return commit_intent(intent, effective_local_cwd(native))
            except CodexThreadResumeError as exc:
                if 'active writer' in str(exc).lower():
                    server._linked_codex_clear_intent(store, sid, intent, intent['from'])
                    raise AuthorityRefusal('session_busy', field='workspace',
                        detail='Close this thread in the other Codex client and retry') from exc
                raise AuthorityRefusal('storage_unavailable', field='workspace',
                    detail='Codex workspace change needs recovery; retry this request') from exc
    except CodexThreadResumeError as exc:
        if 'active writer' in str(exc).lower():
            raise AuthorityRefusal('session_busy', field='workspace',
                detail='Close this thread in the other Codex client and retry') from exc
        raise AuthorityRefusal('storage_unavailable', field='workspace',
            detail='Codex workspace recovery is unavailable; retry this request') from exc
    except server.LinkedCodexWorkspaceBusy as exc:
        raise AuthorityRefusal('session_busy', field='workspace', detail=str(exc)) from exc
    except server.LinkedCodexWorkspaceError as exc:
        raise AuthorityRefusal('storage_unavailable', field='workspace', detail=str(exc)) from exc
