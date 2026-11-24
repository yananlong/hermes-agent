"""The accepted input owns its row; transport echoes do not own new rows."""
import threading
from types import SimpleNamespace

import pytest

from agent.codex_runtime import _persist_projected_messages
from agent.message_metadata import append_message
from agent.session_persistence import SessionPersistenceMixin
from agent.transports.codex_event_projector import CodexEventProjector
from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_read import conversation
from tests.fixtures.storage_seed import seed_session


@pytest.mark.parametrize("platform_id", ["2146", None])
@pytest.mark.parametrize("projection", ["echo", "assistant_only", "different", "later_equal"])
def test_only_submitted_leading_echo_is_excluded(tmp_path, platform_id, projection):
    db = open_persistence(profile_scope(home=tmp_path))
    try:
        seed_session(db, "echo", "telegram", model="codex")
        agent = SessionPersistenceMixin()
        agent.session_id = "echo"
        agent._session_db = db
        agent.persistence = db
        agent._session_db_created = True
        agent._last_flushed_db_idx = 0
        agent._session_persist_lock = threading.RLock()
        messages = []
        # Two independently accepted identical inputs must both survive.
        for turn_index in range(2):
            append_message(messages, {"role": "user", "content": "accepted", "platform_message_id": platform_id})
            assert agent._flush_messages_to_session_db(messages)
            projector = CodexEventProjector()
            items = []
            if projection != "assistant_only":
                items.append({"type": "userMessage", "id": f"u{turn_index}", "content": [
                    {"type": "text", "text": "different" if projection == "different" else "wire caption"}]})
            items.append({"type": "agentMessage", "id": f"a{turn_index}", "text": "reply"})
            if projection == "later_equal":
                items.append({"type": "userMessage", "id": f"s{turn_index}", "content": [
                    {"type": "text", "text": "wire caption"}]})
            projected = []
            for item in items:
                projected.extend(projector.project({"method": "item/completed", "params": {"item": item}}).messages)
            _persist_projected_messages(agent, SimpleNamespace(
                projected_messages=projected, submitted_user_text="wire caption"), messages)
            assert agent._flush_messages_to_session_db(messages)
        users = [row["content"] for row in conversation(db, "echo") if row["role"] == "user"]
        extra = {"different": ["different"], "later_equal": ["wire caption"]}.get(projection, [])
        assert users == (["accepted"] + extra) * 2
    finally:
        db.close()


def test_linked_codex_projection_stays_in_memory_without_a_transcript_mirror():
    """A linked thread keeps its accepted/native history only in Codex's rollout."""
    db = SimpleNamespace(get_session_model_config_value=lambda _sid, key: {
        "codex_linked_thread": True, "codex_thread_id": "native-thread",
    }.get(key))
    agent = SimpleNamespace(_session_db=db, session_id="linked")
    flushes = []
    agent._flush_messages_to_session_db = lambda messages: flushes.append(messages)
    messages = [{"role": "user", "content": "accepted"}]
    turn = SimpleNamespace(projected_messages=[
        {"role": "user", "content": "wire echo"},
        {"role": "assistant", "content": "native reply"},
    ], submitted_user_text="wire echo")

    assert _persist_projected_messages(agent, turn, messages) is False
    assert [(message["role"], message["content"]) for message in messages] == [
        ("user", "accepted"), ("assistant", "native reply")]
    assert flushes == []
