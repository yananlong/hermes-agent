"""The classic CLI sends into a linked Codex thread whatever the profile's own provider can do.

Codex authenticates a linked thread itself and the gateway runs it on the app-server, so a profile
whose provider has no credential (here ``openai-codex`` with no Hermes login) must still reach the
native thread instead of being refused by the CLI's provider check before the gateway is asked.
"""

from __future__ import annotations

import json
import time

import pytest

from tests.e2e.core.cli_tmux._classic import ClassicHome, Pane, require_tmux
from tests.fakes.fake_llm_provider import OFFER_VERSION
from tests.fakes.providers.codex_app_server import FakeCodex

pytestmark = pytest.mark.platforms("linux")

THREAD = "01a1c4a4-7a29-7011-8493-04445af16d6e"
CONFIG = """model:
  provider: openai-codex
  default: gpt-6-luna
  codex_bin: {codex_bin}
agent:
  api_max_retries: 1
telemetry:
  shared_metrics:
    enabled: false
    send: false
    offer_version: {offer}
display:
  interface: cli
updates:
  check: false
models_dev:
  url: http://127.0.0.1:9/api.json
auxiliary:
  title_generation:
    enabled: false
"""


def test_linked_thread_send_is_not_refused_by_the_profile_provider_credential(tmp_path):
    require_tmux()
    ch = ClassicHome(tmp_path)
    fake = FakeCodex(tmp_path, [{"steps": [{"kind": "message", "text": "native answer"}]}])
    ch.hermes_home.mkdir(parents=True, exist_ok=True)
    ch.work.mkdir(parents=True, exist_ok=True)
    (ch.hermes_home / "config.yaml").write_text(
        CONFIG.format(codex_bin=fake.bin, offer=OFFER_VERSION), encoding="utf-8")
    (ch.hermes_home / ".env").write_text("", encoding="utf-8")
    (ch.home / ".codex").mkdir(parents=True, exist_ok=True)
    (fake.state_dir / "threads.json").write_text(json.dumps({
        "threads": {THREAD: {"createdAt": int(time.time()), "cwd": str(ch.work), "turns": []}},
        "turn_cursor": 0}))
    pane = Pane(ch)
    try:
        pane.launch()
        assert pane.wait(["❯"], 120), pane.scrollback()[-3000:]
        pane.settle(quiet=3.0, timeout=60)
        pane.submit(f"/link-thread codex {THREAD}")
        assert pane.wait(["opening it"], 120), pane.scrollback()[-3000:]
        pane.settle(quiet=2.0, timeout=20)
        pane.submit("hello")
        hit = pane.wait(["native answer", "No Codex credentials", "require the Codex app-server"], 120)
        assert hit == "native answer", pane.scrollback()[-4000:]
        assert [entry["msg"]["params"]["threadId"] for entry in fake.requests("turn/start")] == [THREAD]
        assert not fake.requests("thread/start")
    finally:
        pane.kill()
        ch.reap()
