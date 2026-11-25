"""A trace upload must not turn a linked native pointer into a partial Hermes transcript."""

import sys

import pytest

from persistence_assembly import open_persistence, profile_scope
from tests.fixtures.storage_seed import seed_messages, seed_session


@pytest.mark.parametrize("local_messages", [False, True])
def test_trace_upload_refuses_linked_codex_before_invoking_uploader(
    monkeypatch, tmp_path, capsys, local_messages
):
    from agent import trace_upload
    import hermes_cli.main as main_mod

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = open_persistence(profile_scope(home=tmp_path))
    seed_session(
        db, "linked",
        "desktop",
        model_config={"codex_thread_id": "native-thread", "codex_linked_thread": True},
    )
    if local_messages:
        seed_messages(db, "linked", {"role": "user", "content": "Hermes-side row"})
    db.close()

    uploaded = []
    monkeypatch.setattr(trace_upload, "upload_session_trace", lambda *args, **kwargs: uploaded.append(args))
    monkeypatch.setattr(
        sys,
        "argv",
        ["hermes", "sessions", "export", "--format", "trace", "--upload", "--session-id", "linked"],
    )

    with pytest.raises(SystemExit) as exc_info:
        main_mod.main()

    assert exc_info.value.code == 1
    assert uploaded == []
    output = capsys.readouterr().out
    assert "linked Codex thread" in output
    assert "history is stored in Codex" in output
