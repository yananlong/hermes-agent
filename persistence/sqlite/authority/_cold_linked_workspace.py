"""Receipted workspace moves for an unopened native Codex pointer."""

import os

from persistence.authority.runtime import mutation_receipt_key, parse_mutation
from persistence.core.errors import AuthorityRefusal

from ..conversation._store.local import POLICY_PREFIX
from ..conversation._store.local_migration import LEGACY_PREFIX
from ..conversation._store.sessions import _own_profile_name, _parse_model_config
from ._mutation import authorize_write
from ._runtime import _epoch, _json, _text, replay_mutation_receipt
from ._runtime_guards import require_idle


def _unopened_row(holder, conn, request, thread_id):
    sid = request.session_id
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if row is None:
        raise AuthorityRefusal("not_found")
    config = _parse_model_config(row["model_config"])
    if config.get("codex_linked_thread") is not True or config.get("codex_thread_id") != thread_id:
        raise AuthorityRefusal("revision_conflict", field="workspace", detail="linked thread identity changed")
    if row["profile_name"] not in (None, "", _own_profile_name(holder) or "default"):
        raise AuthorityRefusal("profile_mismatch")
    if row["runtime_revision"] != request.expected_revision:
        raise AuthorityRefusal("revision_conflict")
    if row["runtime_generation"] != request.expected_generation:
        raise AuthorityRefusal("stale_generation")
    if (row["session_key"] or row["chat_id"] or row["origin_json"]
            or conn.execute("SELECT 1 FROM state_meta WHERE key IN (?,?)",
                            (POLICY_PREFIX + sid, LEGACY_PREFIX + sid)).fetchone()):
        raise AuthorityRefusal("runtime_coordination_required")
    require_idle(holder, conn, [sid])
    return row, config


def _claim(conn, request, row, config, thread_id, target_cwd):
    pending = config.get("codex_pending_cwd")
    intent = {
        "thread_id": thread_id,
        "from": row["cwd"],
        "to": target_cwd,
        "request_id": request.request_id,
        "principal_id": request.principal_id,
        "expected_revision": request.expected_revision,
        "expected_generation": request.expected_generation,
    }
    if pending is not None:
        if pending == intent:
            return {"intent": pending, "recovery": True}
        raise AuthorityRefusal("revision_conflict", field="workspace",
                              detail="another workspace change needs recovery first")
    original = row["cwd"]
    if not isinstance(original, str) or not os.path.isabs(original) or not os.path.isdir(original):
        raise AuthorityRefusal("storage_unavailable", field="workspace", detail="linked workspace is unavailable")
    config["codex_pending_cwd"] = intent
    conn.execute("UPDATE sessions SET model_config=? WHERE id=?", (_json(config), request.session_id))
    return {"intent": intent, "recovery": False}


def _publish(conn, request, row, config, intent, target_cwd, branch, repo_root, key, digest):
    if config.get("codex_pending_cwd") != intent or row["cwd"] != intent.get("from"):
        raise AuthorityRefusal("revision_conflict", field="workspace", detail="workspace request changed before commit")
    config.pop("codex_pending_cwd")
    conn.execute(
        "UPDATE sessions SET cwd=?, git_branch=?, git_repo_root=?, "
        "git_metadata_generation=COALESCE(git_metadata_generation,0)+1, "
        "runtime_revision=runtime_revision+1, runtime_generation=runtime_generation+1, "
        "model_config=? WHERE id=?",
        (target_cwd, branch, repo_root, _json(config), request.session_id),
    )
    result = {
        "session_id": request.session_id, "revision": request.expected_revision + 1,
        "execution_generation": request.expected_generation + 1, "operation": "workspace",
        "cwd": target_cwd, "branch": branch, "git_repo_root": repo_root,
    }
    conn.execute("INSERT INTO state_meta(key,value) VALUES(?,?)", (key, _json({"digest": digest, "result": result})))
    return {"replay": result}


def mutate_cold_linked_workspace(holder, epoch, request, *, thread_id, target_cwd=None, intent=None,
                                 branch=None, repo_root=None, history=None, owner=None):
    for value in (request.principal_id, request.session_id, request.request_id):
        _text(value)
    payload = {"cwd": request.cwd}
    parse_mutation("workspace", payload)
    if any(type(value) is not int or value < 0 for value in (request.expected_revision, request.expected_generation)):
        raise AuthorityRefusal("invalid_params")
    key, digest = mutation_receipt_key(
        request.principal_id, request.session_id, request.request_id, operation="workspace", payload=payload,
        expected_revision=request.expected_revision, expected_generation=request.expected_generation,
    )

    def write(conn):
        _epoch(conn, epoch)
        if (saved := replay_mutation_receipt(conn, key, digest)) is not None:
            return {"replay": saved}
        if target_cwd is None:
            return {}
        row, config = _unopened_row(holder, conn, request, thread_id)
        if history is None:
            raise AuthorityRefusal("permission_denied")
        authorize_write(conn, history, request.session_id, owner)
        if intent is None:
            return _claim(conn, request, row, config, thread_id, target_cwd)
        return _publish(conn, request, row, config, intent, target_cwd, branch, repo_root, key, digest)

    return holder._execute_write(write)
