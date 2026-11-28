"""A previously linked Codex thread's recap comes from its native transcript."""

from __future__ import annotations

import json
import time

import pytest

from persistence_assembly import open_persistence, profile_scope
from tests.e2e.core.cli_tmux._classic import ClassicHome, Pane, require_tmux
from tests.e2e.core.cli_tmux.test_classic_cli_linked_codex import CONFIG, THREAD
from tests.fakes.fake_llm_provider import OFFER_VERSION
from tests.fakes.providers.codex_app_server import FakeCodex
from tests.fixtures.storage_seed import seed_messages, seed_session


pytestmark = pytest.mark.platforms("linux")
SID = "20261126_000000_deadbeef0001"
NATIVE_QUESTION = "NATIVE prior question amber"
NATIVE_ANSWER = "NATIVE prior answer cobalt"
MIRROR_MARKER = "STALE local mirror message"
SAFE_ANSWER = "safe visible answer " + "x" * 500 + " safe-answer-end"


@pytest.mark.live_system_guard_bypass  # ch.reap() signals a gateway the tmux server owns, not a pytest descendant
@pytest.mark.parametrize("mirror", [False, True], ids=["empty-mirror", "stale-mirror"])
def test_existing_link_shows_native_history_instead_of_its_local_mirror(tmp_path, mirror):
    require_tmux()
    ch = ClassicHome(tmp_path)
    fake = FakeCodex(tmp_path, [{"steps": [{"kind": "message", "text": "native next answer"}]}])
    ch.hermes_home.mkdir(parents=True, exist_ok=True)
    ch.work.mkdir(parents=True, exist_ok=True)
    (ch.home / ".codex").mkdir(parents=True, exist_ok=True)
    (ch.hermes_home / "config.yaml").write_text(
        CONFIG.format(codex_bin=fake.bin, offer=OFFER_VERSION) + "\nsessions:\n  auto_prune: false\n")
    (ch.hermes_home / ".env").write_text("")
    prior = {"id": "native-before-link", "status": "completed", "items": [
        {"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": NATIVE_QUESTION}]},
        {"type": "agentMessage", "id": "a1", "text": NATIVE_ANSWER},
    ]}
    settled = {"id": "native-settled", "status": "completed", "items": [
        {"type": "userMessage", "id": "u2", "content": [{"type": "text", "text": "visible question"}]},
        {"type": "agentMessage", "id": "a2", "text": SAFE_ANSWER},
    ]}
    (fake.state_dir / "threads.json").write_text(json.dumps({
        "threads": {THREAD: {"createdAt": int(time.time()), "cwd": str(ch.work), "turns": [prior, settled]}},
        "turn_cursor": 0,
    }))
    with open_persistence(profile_scope(home=ch.hermes_home)) as db:
        seed_session(db, SID, "desktop", title="already linked", cwd=str(ch.work), model="gpt-6-luna", model_config={
            "codex_linked_thread": True, "codex_thread_id": THREAD,
            "provider": "openai", "api_mode": "codex_app_server",
        })
        assert db.sessions.save_linked_codex_display_overlay(
            SID, THREAD, "native-settled", final_text=SAFE_ANSWER, held=False, platform="desktop")
        if mirror:
            seed_messages(db, SID, {"role": "user", "content": MIRROR_MARKER},
                          {"role": "assistant", "content": "stale local reply"})
    pane = Pane(ch)
    try:
        pane.launch("--resume", SID)
        assert pane.wait(["❯"], 60), pane.scrollback()[-3000:] + ch.diagnostics()
        pane.settle(quiet=2.0, timeout=20)
        initial = pane.scrollback()
        assert NATIVE_QUESTION in initial, initial[-4000:]
        assert MIRROR_MARKER not in initial
        # A turn Hermes never ran cannot carry a stored display policy, so there is nothing to
        # withhold: the prior native answer renders as-is rather than hiding behind a notice.
        assert NATIVE_ANSWER in initial, initial[-4000:]
        assert "Earlier Codex output is hidden" not in initial
        pane.submit("/history")
        assert pane.wait(["Linked history"], 60), pane.screen() + ch.diagnostics()
        assert NATIVE_QUESTION in pane.screen()
        assert NATIVE_ANSWER in pane.screen()
        assert "Earlier Codex output is hidden" not in pane.screen()
        # The settled turn DID store a policy (final_text=SAFE_ANSWER), and that policy wins over
        # the raw native text — it is the only turn whose output a transform could have rewritten.
        assert "safe-answer-end" in pane.screen()
        pane.keys("q")
        assert pane.wait(["❯"], 30), pane.scrollback()[-3000:]
        pane.submit("new prompt")
        assert pane.wait(["native next answer"], 120), pane.scrollback()[-4000:]
        sent = fake.requests("turn/start")
        assert len(sent) == 1 and sent[0]["msg"]["params"]["threadId"] == THREAD
        assert sent[0]["msg"]["params"]["input"] == [{"type": "text", "text": "new prompt"}]
        assert not fake.requests("thread/start")
    finally:
        pane.kill()
        ch.reap()
