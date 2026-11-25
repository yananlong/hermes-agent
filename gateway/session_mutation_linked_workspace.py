"""Commit a linked Codex workspace change under both owners' writer fences."""

import json
import os

from persistence.core.errors import AuthorityRefusal


def commit_linked_workspace(store, sid, request_id, principal_id, prepared, publish):
    """Change the native cwd and commit its policy and retry receipt atomically in Hermes.

    An intent remains durable if the native resume succeeds but the owner transaction fails.
    A retry reads Codex's cwd before deciding whether an override still needs to be applied.
    """
    from agent.transports.codex_app_server_session import CodexThreadResumeError
    from gateway.session_linked_codex import linked_thread_id
    from tui_gateway import server

    thread_id = linked_thread_id(store, sid)
    if not thread_id:
        raise AuthorityRefusal('revision_conflict', field='workspace', detail='linked thread identity changed')
    target = prepared['policy']['cwd']
    terminal = json.loads(prepared['snapshot']['receipt']['policy']['terminal_json'])
    if str(terminal.get('TERMINAL_ENV') or 'local').lower() == 'ssh':
        raise AuthorityRefusal('unsupported_operation', field='workspace',
                                detail='linked Codex workspaces must be local to the gateway')
    profile_home = str(store.scope.profile_dir)

    def same_cwd(left, right):
        return isinstance(left, str) and os.path.realpath(left) == os.path.realpath(right)

    def commit(intent, native):
        if not same_cwd(native.effective_cwd, target):
            raise AuthorityRefusal('revision_conflict', field='workspace',
                                    detail='Codex reported a different working directory')
        outcome = publish({**prepared, 'linked_codex_workspace_intent': intent})
        row = store.sessions.get(sid)
        if (not same_cwd(row.cwd if row is not None else None, target) or linked_thread_id(store, sid) != thread_id
                or store.sessions.get_model_config_value(sid, 'codex_pending_cwd') is not None
                or not store.runtime.has_mutation_receipt(principal_id, sid, request_id)):
            raise AuthorityRefusal('storage_unavailable', field='workspace',
                                    detail='workspace commit could not be verified; retry this request')
        return outcome

    try:
        with server._linked_codex_workspace_operation_lock(store, thread_id):
            if linked_thread_id(store, sid) != thread_id:
                raise AuthorityRefusal('revision_conflict', field='workspace', detail='linked thread identity changed')
            intent = server._linked_codex_pending_intent(store, sid)
            if intent is not None:
                if (intent['thread_id'] != thread_id
                        or intent.get('principal_id', principal_id) != principal_id):
                    raise AuthorityRefusal('revision_conflict', field='workspace',
                                            detail='another workspace change needs recovery first')
                # The prior operation may have changed Codex but failed before the receipt transaction.
                with server._linked_codex_resume(thread_id, target, profile_home,
                                                 override=False, accept_native=True) as native:
                    if same_cwd(native.effective_cwd, target):
                        return commit(intent, native)
                    if not same_cwd(native.effective_cwd, intent['from']):
                        raise AuthorityRefusal('revision_conflict', field='workspace',
                            detail=f'Codex currently uses {native.effective_cwd}; select that folder to synchronize first')
                    server._linked_codex_clear_intent(store, sid, intent, intent['from'])

            row = store.sessions.get(sid)
            original = row.cwd if row is not None else None
            if not isinstance(original, str) or not os.path.isabs(original):
                raise AuthorityRefusal('storage_unavailable', field='workspace', detail='linked workspace is unavailable')
            intent = {'thread_id': thread_id, 'from': original, 'to': target,
                      'request_id': request_id, 'principal_id': principal_id}
            if not store.sessions.claim_linked_codex_workspace_change(sid, intent):
                raise AuthorityRefusal('revision_conflict', field='workspace', detail='workspace request changed')
            try:
                with server._linked_codex_resume(thread_id, target, profile_home, override=True) as native:
                    return commit(intent, native)
            except CodexThreadResumeError as exc:
                if 'active writer' in str(exc).lower():
                    server._linked_codex_clear_intent(store, sid, intent, original)
                    raise AuthorityRefusal('session_busy', field='workspace',
                                            detail='Close this thread in the other Codex client and retry') from exc
                raise AuthorityRefusal('storage_unavailable', field='workspace',
                                        detail='Codex workspace change needs recovery; retry this request') from exc
    except CodexThreadResumeError as exc:
        # Recovery also resumes the thread; preserve its pending marker if another client owns it.
        if 'active writer' in str(exc).lower():
            raise AuthorityRefusal('session_busy', field='workspace',
                                    detail='Close this thread in the other Codex client and retry') from exc
        raise AuthorityRefusal('storage_unavailable', field='workspace',
                                detail='Codex workspace recovery is unavailable; retry this request') from exc
    except server.LinkedCodexWorkspaceBusy as exc:
        raise AuthorityRefusal('session_busy', field='workspace', detail=str(exc)) from exc
    except server.LinkedCodexWorkspaceError as exc:
        raise AuthorityRefusal('storage_unavailable', field='workspace', detail=str(exc)) from exc
