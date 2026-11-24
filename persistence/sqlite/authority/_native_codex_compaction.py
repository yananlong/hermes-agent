"""Durable intents for Codex-native compaction and publication of its receipt."""
import json
import time
import uuid
from persistence.authority.vocabulary import admission_fingerprint
from persistence.core.errors import AuthorityRefusal


_NATIVE_CODEX_COMPACT_PREFIX = 'gateway.codex_native_compact.v1.'


def native_codex_compaction_key(target_session_id):
    """Stable slot for an unresolved native compact on a linked physical thread."""
    return _NATIVE_CODEX_COMPACT_PREFIX + admission_fingerprint(
        canonical_target=target_session_id, payload={'kind': 'intent'})


def require_native_codex_compaction_clear(conn, session_ids, *, request_identity=None):
    """Fence session writes/claims while a linked native compact awaits its Hermes receipt."""
    from ..conversation._store.local import POLICY_PREFIX

    targets = set()
    for session_id in session_ids or ():
        if not isinstance(session_id, str) or not session_id:
            continue
        targets.add(session_id)
        policy = conn.execute('SELECT value FROM state_meta WHERE key=?', (POLICY_PREFIX + session_id,)).fetchone()
        if policy is not None:
            try:
                receipt = json.loads(policy[0])
            except (TypeError, ValueError):
                raise AuthorityRefusal('storage_unavailable', field='session_id') from None
            target = (receipt.get('entry', {}).get('session_id') if isinstance(receipt, dict)
                      and isinstance(receipt.get('entry'), dict) else None)
            if isinstance(target, str) and target:
                targets.add(target)
    for session_id in targets:
        row = conn.execute('SELECT value FROM state_meta WHERE key=?',
                           (native_codex_compaction_key(session_id),)).fetchone()
        if row is None:
            continue
        try:
            marker = json.loads(row[0])
        except (TypeError, ValueError):
            raise _native_compaction_pending() from None
        if not isinstance(marker, dict) or marker.get('version') != 1:
            raise _native_compaction_pending()
        identity = request_identity or {}
        exact_retry = (
            identity.get('request_key') is not None
            and marker.get('request_key') == identity.get('request_key')
            and marker.get('request_digest') == identity.get('request_digest')
            and marker.get('target_session_id') == session_id
        )
        completed_recovery = _completed_native_compaction_matches(
            marker, identity, target_session_id=session_id, thread_id=marker.get('thread_id'))
        if not exact_retry and not completed_recovery:
            raise _native_compaction_pending()


def _completed_native_compaction_matches(marker, identity, *, target_session_id, thread_id):
    return (
        isinstance(marker, dict)
        and marker.get('version') == 1
        and marker.get('state') == 'completed'
        and isinstance(marker.get('outcome'), dict)
        and marker.get('target_session_id') == target_session_id
        and marker.get('thread_id') == thread_id
        and marker.get('request_digest') == identity.get('request_digest')
        and marker.get('request_session_id') == identity.get('request_session_id')
        and marker.get('principal_id') == identity.get('principal_id')
        and marker.get('expected_revision') == identity.get('expected_revision')
        and marker.get('expected_generation') == identity.get('expected_generation')
    )


def _native_compaction_pending():
    return AuthorityRefusal(
        'runtime_coordination_required', field='operation',
        detail=('A prior Codex compact may have been sent; retry is blocked. Verify history read-only, '
                'then use pending_native_codex_compaction/resolve_native_codex_compaction.'),
    )


def _rebind_completed_native_compaction(marker, *, identity):
    """Bind the saved confirmed outcome to the current authenticated retry receipt identity."""
    marker['request_key'] = identity['request_key']
    marker['request_digest'] = identity['request_digest']
    marker['request_session_id'] = identity['request_session_id']
    marker['principal_id'] = identity['principal_id']
    marker['request_id'] = identity['request_id']


def _completed_native_compaction_result(key, *, target_session_id, thread_id, identity, marker):
    return {
        'token': {'key': key, 'request_key': identity['request_key'],
                  'request_digest': identity['request_digest'], 'target_session_id': target_session_id,
                  'thread_id': thread_id},
        'outcome': marker['outcome'],
    }


def resume_completed_native_codex_compaction(holder, *, epoch, target_session_id, thread_id, identity, snapshot):
    """Resume and, when needed, rebind a completed result to a same-operation fresh request ID.

    Only the same profile-scoped database, authorized principal, logical session, payload digest,
    and revision/generation fences can adopt the confirmed outcome. An uncertain ``started`` marker
    remains blocked and never reaches the native compact call.
    """
    from ._runtime_picks import local_snapshot
    from ._runtime import _epoch, _json
    from ..conversation._store.sessions import _parse_model_config

    key = native_codex_compaction_key(target_session_id)

    def write(conn):
        _epoch(conn, epoch)
        owner = conn.execute('SELECT runtime_revision,runtime_generation FROM sessions WHERE id=?',
                             (identity['request_session_id'],)).fetchone()
        if owner is None:
            raise AuthorityRefusal('not_found')
        if owner['runtime_revision'] != identity['expected_revision']:
            raise AuthorityRefusal('revision_conflict')
        if owner['runtime_generation'] != identity['expected_generation']:
            raise AuthorityRefusal('stale_generation')
        if local_snapshot(holder, conn, identity['request_session_id']) != snapshot:
            raise AuthorityRefusal('revision_conflict')
        linked = conn.execute('SELECT model_config FROM sessions WHERE id=?', (target_session_id,)).fetchone()
        config = _parse_model_config(linked[0]) if linked is not None else {}
        if (config.get('codex_linked_thread') is not True or config.get('codex_thread_id') != thread_id):
            raise AuthorityRefusal('revision_conflict', field='native_compaction_intent')
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
        if row is None:
            return None
        try:
            marker = json.loads(row[0])
        except (TypeError, ValueError):
            raise _native_compaction_pending() from None
        if not _completed_native_compaction_matches(
                marker, identity, target_session_id=target_session_id, thread_id=thread_id):
            raise _native_compaction_pending()
        if (marker.get('request_key') != identity['request_key']
                or marker.get('request_id') != identity['request_id']):
            _rebind_completed_native_compaction(marker, identity=identity)
            marker['rebound_at'] = time.time()
            conn.execute('UPDATE state_meta SET value=? WHERE key=?', (_json(marker), key))
        return _completed_native_compaction_result(
            key, target_session_id=target_session_id, thread_id=thread_id, identity=identity, marker=marker)

    return holder._execute_write(write)


def pending_native_codex_compaction(holder, target_session_id):
    """Read the bounded recovery record for an operator investigating an uncertain compact."""
    key = native_codex_compaction_key(target_session_id)
    with holder._read_ctx() as conn:
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
    if row is None:
        return None
    try:
        marker = json.loads(row[0])
    except (TypeError, ValueError):
        raise _native_compaction_pending() from None
    if not isinstance(marker, dict) or marker.get('version') != 1:
        raise _native_compaction_pending()
    return dict(marker)


def resolve_native_codex_compaction(holder, *, epoch, target_session_id, thread_id,
                                    request_key, request_digest, verified_status):
    """Operator recovery after independent read-only verification of the native thread.

    ``verified_status`` is ``"compacted"`` to save a replayable outcome, or ``"not_compacted"``
    to release the slot for a retry. This function cannot verify Codex state itself; callers must
    only use it after checking the linked thread read-only and matching this exact pending record.
    """
    if verified_status not in {'compacted', 'not_compacted'}:
        raise AuthorityRefusal('invalid_params', field='verified_status')
    from ._runtime import _epoch, _json
    from ..conversation._store.sessions import _parse_model_config
    key = native_codex_compaction_key(target_session_id)

    def write(conn):
        _epoch(conn, epoch)
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
        if row is None:
            raise AuthorityRefusal('not_found', field='native_compaction_intent')
        try:
            marker = json.loads(row[0])
        except (TypeError, ValueError):
            raise _native_compaction_pending() from None
        if (marker.get('state') != 'started' or marker.get('target_session_id') != target_session_id
                or marker.get('thread_id') != thread_id or marker.get('request_key') != request_key
                or marker.get('request_digest') != request_digest):
            raise AuthorityRefusal('revision_conflict', field='native_compaction_intent')
        linked = conn.execute('SELECT model_config FROM sessions WHERE id=?', (target_session_id,)).fetchone()
        config = _parse_model_config(linked[0]) if linked is not None else {}
        if (config.get('codex_linked_thread') is not True or config.get('codex_thread_id') != thread_id):
            raise AuthorityRefusal('revision_conflict', field='native_compaction_intent',
                                    detail='the linked Codex thread changed during recovery')
        if verified_status == 'not_compacted':
            conn.execute('DELETE FROM state_meta WHERE key=?', (key,))
            return 'cleared'
        count = marker.get('before_messages')
        if type(count) is not int or count < 0:
            count = 0
        marker['state'] = 'completed'
        marker['outcome'] = {
            'status': 'compressed',
            'summary': {
                'noop': False, 'aborted': False, 'refused_would_grow': False, 'fallback_used': False,
                'headline': 'Codex compaction verified during recovery',
                'token_line': 'Codex compaction was verified; Hermes transcript rows were not rewritten.',
                'note': 'Codex remains the history authority for this thread.',
            },
            'before_messages': count, 'after_messages': count, 'removed': 0,
        }
        marker['recovered_at'] = time.time()
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (_json(marker), key))
        return 'completed'

    return holder._execute_write(write)


def begin_native_codex_compaction(holder, *, epoch, target_session_id, thread_id, identity, snapshot):
    """Durably reserve the at-most-once native compact slot before its external side effect."""
    from ._runtime_picks import local_snapshot
    from ._runtime import _epoch, _json
    from ..conversation._store.sessions import _parse_model_config

    key = native_codex_compaction_key(target_session_id)

    def write(conn):
        _epoch(conn, epoch)
        owner_row = conn.execute('SELECT runtime_revision,runtime_generation FROM sessions WHERE id=?',
                                 (identity['request_session_id'],)).fetchone()
        if owner_row is None:
            raise AuthorityRefusal('not_found')
        if owner_row['runtime_revision'] != identity['expected_revision']:
            raise AuthorityRefusal('revision_conflict')
        if owner_row['runtime_generation'] != identity['expected_generation']:
            raise AuthorityRefusal('stale_generation')
        row = conn.execute('SELECT model_config FROM sessions WHERE id=?', (target_session_id,)).fetchone()
        if row is None:
            raise AuthorityRefusal('not_found')
        if local_snapshot(holder, conn, identity['request_session_id']) != snapshot:
            raise AuthorityRefusal('revision_conflict')
        config = _parse_model_config(row['model_config'])
        if config.get('codex_linked_thread') is not True or config.get('codex_thread_id') != thread_id:
            raise AuthorityRefusal('unsupported_operation', field='operation',
                                    detail='the linked Codex thread changed before compaction')
        existing = conn.execute('SELECT value FROM state_meta WHERE key=?', (key,)).fetchone()
        if existing is not None:
            try:
                marker = json.loads(existing[0])
            except (TypeError, ValueError):
                raise _native_compaction_pending() from None
            if _completed_native_compaction_matches(
                    marker, identity, target_session_id=target_session_id, thread_id=thread_id):
                if (marker.get('request_key') != identity['request_key']
                        or marker.get('request_id') != identity['request_id']):
                    _rebind_completed_native_compaction(marker, identity=identity)
                    marker['rebound_at'] = time.time()
                    conn.execute('UPDATE state_meta SET value=? WHERE key=?', (_json(marker), key))
                return _completed_native_compaction_result(
                    key, target_session_id=target_session_id, thread_id=thread_id,
                    identity=identity, marker=marker)
            raise _native_compaction_pending()
        marker = {
            'version': 1,
            'state': 'started',
            'target_session_id': target_session_id,
            'thread_id': thread_id,
            'request_key': identity['request_key'],
            'request_digest': identity['request_digest'],
            'request_session_id': identity['request_session_id'],
            'principal_id': identity.get('principal_id'),
            'request_id': identity.get('request_id'),
            'expected_revision': identity['expected_revision'],
            'expected_generation': identity['expected_generation'],
            'before_messages': identity.get('before_messages', 0),
            'started_at': time.time(),
        }
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)', (key, _json(marker)))
        return {'token': {'key': key, 'request_key': identity['request_key'],
                          'request_digest': identity['request_digest'],
                          'target_session_id': target_session_id, 'thread_id': thread_id},
                'outcome': None}

    return holder._execute_write(write)


def complete_native_codex_compaction(holder, *, epoch, token, outcome):
    """Record Codex's confirmation before attempting Hermes' receipt transaction."""
    from ._runtime import _epoch, _json

    if not isinstance(outcome, dict) or outcome.get('status') != 'compressed':
        raise AuthorityRefusal('invalid_params')

    def write(conn):
        _epoch(conn, epoch)
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (token['key'],)).fetchone()
        if row is None:
            raise _native_compaction_pending()
        try:
            marker = json.loads(row[0])
        except (TypeError, ValueError):
            raise _native_compaction_pending() from None
        expected = {
            'request_key': token['request_key'], 'request_digest': token['request_digest'],
            'target_session_id': token['target_session_id'], 'thread_id': token['thread_id'],
        }
        if any(marker.get(name) != value for name, value in expected.items()):
            raise _native_compaction_pending()
        if marker.get('state') == 'completed':
            if marker.get('outcome') != outcome:
                raise _native_compaction_pending()
            return
        if marker.get('state') != 'started':
            raise _native_compaction_pending()
        marker['state'] = 'completed'
        marker['outcome'] = outcome
        marker['completed_at'] = time.time()
        conn.execute('UPDATE state_meta SET value=? WHERE key=?', (_json(marker), token['key']))

    return holder._execute_write(write)


def clear_unsubmitted_native_codex_compaction(holder, *, epoch, token):
    """Release only a request known not to have reached Codex (the local lock-skip path)."""
    from ._runtime import _epoch

    def write(conn):
        _epoch(conn, epoch)
        row = conn.execute('SELECT value FROM state_meta WHERE key=?', (token['key'],)).fetchone()
        if row is None:
            raise _native_compaction_pending()
        try:
            marker = json.loads(row[0])
        except (TypeError, ValueError):
            raise _native_compaction_pending() from None
        expected = {
            'request_key': token['request_key'], 'request_digest': token['request_digest'],
            'target_session_id': token['target_session_id'], 'thread_id': token['thread_id'],
        }
        if (marker.get('state') != 'started'
                or any(marker.get(name) != value for name, value in expected.items())):
            raise _native_compaction_pending()
        conn.execute('DELETE FROM state_meta WHERE key=?', (token['key'],))

    return holder._execute_write(write)


def _consume_native_codex_compaction_intent(conn, prepared, target_session_id):
    from ._runtime import _json

    token = prepared.get('native_compaction_intent')
    if not isinstance(token, dict):
        raise _native_compaction_pending()
    row = conn.execute('SELECT value FROM state_meta WHERE key=?', (token.get('key'),)).fetchone()
    if row is None:
        raise _native_compaction_pending()
    try:
        marker = json.loads(row[0])
    except (TypeError, ValueError):
        raise _native_compaction_pending() from None
    expected = {
        'request_key': token.get('request_key'), 'request_digest': token.get('request_digest'),
        'target_session_id': target_session_id, 'thread_id': prepared.get('native_thread_id'),
    }
    if (marker.get('state') != 'completed' or not isinstance(marker.get('outcome'), dict)
            or any(marker.get(name) != value for name, value in expected.items())):
        raise _native_compaction_pending()
    if marker['outcome'] != prepared.get('native_compaction_outcome'):
        raise _native_compaction_pending()
    from ..conversation._store.sessions import _parse_model_config
    row = conn.execute('SELECT model_config FROM sessions WHERE id=?', (target_session_id,)).fetchone()
    config = _parse_model_config(row[0]) if row is not None else {}
    if config.get('codex_linked_thread') is not True or config.get('codex_thread_id') != token.get('thread_id'):
        raise AuthorityRefusal('revision_conflict', field='operation',
                                detail='the linked Codex thread changed before receipt commit')
    conn.execute('DELETE FROM state_meta WHERE key=?', (token['key'],))
