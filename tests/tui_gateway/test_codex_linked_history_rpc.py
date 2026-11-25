"""Linked native history RPCs authorize the pointer and return only bounded display projections."""

from contextlib import contextmanager
from types import SimpleNamespace


THREAD = "01a1c4a4-7a29-7011-8493-04445af16d6e"
OTHER = "01a1c702-d451-7cc0-85f2-9a35815f56b0"


def test_codex_history_requires_the_session_pointer_and_strips_native_item_payloads(tmp_path, monkeypatch):
    from tui_gateway import server
    from hermes_cli import codex_linked_threads

    row = SimpleNamespace(model="codex-model", source="local", model_config={
        "codex_linked_thread": True, "codex_thread_id": THREAD,
    })
    overlay = {
        "version": 1, "thread_id": THREAD, "turn_id": "native-turn", "held": False,
        "final_visible": True, "final_text": "safe reply", "platform": "local",
    }
    store = SimpleNamespace(sessions=SimpleNamespace(
        get=lambda sid: row if sid == "linked-session" else None,
        load_linked_codex_display_overlays=lambda _sid, _thread, _turn_ids: {"native-turn": overlay},
    ))

    @contextmanager
    def profile_db(_params):
        yield store

    class Client:
        calls = []

        def request(self, method, params):
            self.calls.append((method, params))
            assert method == "thread/turns/list"
            return {"data": [{"id": "native-turn", "status": "completed", "privateTurnField": "DROP",
                              "items": [{"id": "answer", "type": "agentMessage", "text": "safe reply",
                                         "privateItemField": "DROP"}]}],
                    "nextCursor": None}

    client = Client()

    @contextmanager
    def read_client(*, profile_home=None):
        assert profile_home == tmp_path
        yield client

    monkeypatch.setattr(server, "_profile_db", profile_db)
    monkeypatch.setattr(server, "_profile_home", lambda _profile: tmp_path)
    monkeypatch.setattr(codex_linked_threads, "_codex_client", read_client)

    denied = server._codex_history("denied", {"session_id": "linked-session", "thread_id": OTHER,
                                               "limit": 10})
    assert "error" in denied
    assert client.calls == []

    allowed = server._codex_history("allowed", {"session_id": "linked-session", "thread_id": THREAD,
                                                 "limit": 10})
    assert allowed["result"] == {"turns": [{"id": "native-turn", "status": "completed",
                                             "rows": [{"role": "assistant", "content": "safe reply"}]}],
                                  "next_cursor": None}
    assert "privateTurnField" not in repr(allowed)
    assert "privateItemField" not in repr(allowed)
