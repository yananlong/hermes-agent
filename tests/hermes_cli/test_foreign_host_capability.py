"""Host capability for linked foreign sessions is answered for the SERVING host.

A remote Desktop/Webapp never stats its own machine for a linked thread, so every field these
tests pin comes from ``session.foreign.status``'s backend probe: the native store, the native CLI
and the writable-continue verdict, all resolved inside the owning profile. The probe must also be
safe on a host that has none of the tools — one missing agent may not blank the other two — and it
may not widen what a client learns about host paths.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hermes_cli import foreign_host_capability as cap
from hermes_cli import linked_send_admission


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """A profile home with no foreign stores at all (the 'nothing installed' host)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("VIBE_HOME", raising=False)
    monkeypatch.delenv("MISTRAL_VIBE_SESSION_ROOT", raising=False)
    # The real host may well ship claude/vibe; the CLI assertions below resolve from PATH only.
    monkeypatch.setattr(cap, "_transport_binary", lambda agent, config: None)
    return tmp_path


@pytest.fixture
def installed(homes):
    """Every store present, like a host that runs all three native tools."""
    (homes / ".codex").mkdir()
    (homes / ".claude" / "projects" / "project").mkdir(parents=True)
    (homes / ".vibe" / "logs" / "session").mkdir(parents=True)
    return homes


def _bin(homes, name, version="9.9.9"):
    path = homes / "bin" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "{name}-cli {version}"\n', encoding="utf-8")
    path.chmod(0o755)
    return path


# ── store ────────────────────────────────────────────────────────────────────────────────────────


def test_every_agent_reports_a_missing_store_without_failing_the_others(homes):
    result = cap.host_capabilities(host="gw.example")
    assert [row["agent"] for row in result["agents"]] == ["codex", "claude", "mistral"]
    assert result["host"] == "gw.example"
    for row in result["agents"]:
        assert row["store"] == {"available": False, "readable": False,
                                "reason_code": cap.REASON_STORE_MISSING,
                                "reason": row["store"]["reason"]}
        assert row["label"] in row["store"]["reason"]
        assert "gw.example" in row["store"]["reason"]
        # No store means no continue path, and the reason is the store's, not a generic one.
        assert row["continue_status"]["writable"] is False
        assert row["continue_status"]["reason_code"] == cap.REASON_STORE_MISSING


def test_present_store_is_available_and_enumerable(installed):
    store = cap.store_capability("claude", host="gw.example")
    assert store == {"available": True, "readable": True, "reason_code": None, "reason": None}


def test_store_is_metadata_only_and_reports_unreadable(installed, monkeypatch):
    """A store this gateway cannot enumerate says so; nothing is opened or walked."""
    opened = []
    real_scandir = os.scandir

    def spy(path):
        opened.append(str(path))
        raise PermissionError("denied")

    monkeypatch.setattr(cap.os, "scandir", spy)
    try:
        store = cap.store_capability("mistral", host="gw.example")
    finally:
        monkeypatch.setattr(cap.os, "scandir", real_scandir)
    assert store["available"] is True and store["readable"] is False
    assert store["reason_code"] == cap.REASON_STORE_UNREADABLE
    assert opened and opened[0].endswith("session")


def test_routed_profile_without_its_own_store_reads_as_profile_scope(homes, monkeypatch, tmp_path):
    """A profile-scope refusal is not the same as a host without the tool."""
    other = tmp_path / "sibling"
    other.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(other))
    store = cap.store_capability("codex", tmp_path / ".hermes", host="gw.example")
    assert store["reason_code"] == cap.REASON_PROFILE_SCOPE
    assert "profile" in store["reason"]


# ── cli ─────────────────────────────────────────────────────────────────────────────────────────


def test_missing_cli_names_the_host(homes):
    cli = cap.cli_capability("claude", env={"PATH": str(homes / "empty")}, host="gw.example")
    assert cli["available"] is False
    assert cli["path"] is None and cli["version"] is None
    assert cli["reason_code"] == cap.REASON_CLI_MISSING
    assert cli["reason"] == "Claude is not installed on gw.example."


def test_path_cli_resolves_with_a_collapsed_path_and_no_absolute_leak(homes):
    _bin(homes, "claude")
    cli = cap.cli_capability("claude", env={"PATH": str(homes / "bin")}, host="gw.example")
    assert cli["available"] is True
    assert cli["command"] == "claude"
    assert cli["path"] == "~/bin/claude"
    assert str(homes) not in json.dumps(cli)
    # Version probing is opt-in: a status call on mount must not spawn three CLIs.
    assert cli["version"] is None


def test_version_probe_is_opt_in_bounded_and_path_free(homes):
    _bin(homes, "codex", version="0.42.0")
    cli = cap.cli_capability("codex", env={"PATH": str(homes / "bin")}, probe_version=True,
                             host="gw.example")
    assert cli["version"] == "codex-cli 0.42.0"
    assert str(homes) not in cli["version"]


def test_configured_absolute_binary_collapses_to_its_basename(installed):
    binary = _bin(installed, "codex")
    cli = cap.cli_capability("codex", config={"model": {"codex_bin": str(binary)}},
                             host="gw.example")
    assert cli["available"] is True
    assert cli["source"] == "config"
    assert cli["command"] == "codex"
    assert cli["path"] == "~/bin/codex"
    assert str(installed) not in json.dumps(cli)


def test_configured_binary_that_is_gone_is_unavailable(installed):
    cli = cap.cli_capability("codex", config={"model": {"codex_bin": str(installed / "nope")}},
                             host="gw.example")
    assert cli["available"] is False
    assert cli["reason_code"] == cap.REASON_CLI_MISSING
    assert "configured binary" in cli["reason"]


# ── continue (writable capability) ──────────────────────────────────────────────────────────────


def test_send_admission_admits_every_linked_agent():
    """All three agents append a turn in this build, so none of them refuses a send as read-only."""
    assert linked_send_admission.LINKED_SEND_ADMITTED == frozenset({"codex", "claude", "mistral"})
    for agent in ("codex", "claude", "mistral"):
        assert linked_send_admission.linked_send_refusal(agent) is None


def test_send_admission_is_the_single_source_of_truth(monkeypatch):
    """The refusal prompt.submit enforces is the reason the probe reports — for one switch."""
    monkeypatch.setattr(linked_send_admission, "LINKED_SEND_ADMITTED", frozenset({"codex"}))
    store = {"available": True, "readable": True, "reason_code": None, "reason": None}
    cli = {"available": True, "reason_code": None, "reason": None}
    for agent, label in (("claude", "Claude"), ("mistral", "Mistral")):
        assert linked_send_admission.linked_send_refusal(agent) == (
            f"linked foreign sessions are read-only — this is a linked {label} session; "
            f"send new messages in {label} instead")
        assert cap.continue_capability(agent, store=store, cli=cli) == {
            "supported": True, "writable": False, "reason_code": cap.REASON_SEND_READ_ONLY,
            "reason": (f"Sending is read-only in this Hermes build — it cannot append to linked "
                       f"{label} threads yet.")}
    assert linked_send_admission.linked_send_refusal("codex") is None
    assert cap.continue_capability("codex", store=store, cli=cli)["writable"] is True
    assert linked_send_admission.linked_send_refusal("other").startswith(
        "linked foreign sessions are read-only — this is a linked other session")


def test_continue_reports_build_unsupported_when_the_adapter_is_absent(installed, monkeypatch):
    monkeypatch.setattr(cap, "append_supported", lambda agent: False)
    _bin(installed, "vibe")
    row = cap.agent_capability("mistral", config={}, host="gw.example")
    assert row["continue_status"]["supported"] is False
    assert row["continue_status"]["writable"] is False
    assert row["continue_status"]["reason_code"] == cap.REASON_BUILD_UNSUPPORTED
    assert "cannot continue" in row["continue_status"]["reason"]


def test_continue_is_writable_for_every_admitted_agent(installed):
    """The shipped admission admits all three agents, so the probe reports them writable."""
    for agent, key, binary in (("claude", "claude_bin", "claude"),
                               ("mistral", "vibe_bin", "vibe")):
        path = _bin(installed, binary)
        row = cap.agent_capability(agent, config={"model": {key: str(path)}}, host="gw.example")
        assert row["store"]["readable"] is True and row["cli"]["available"] is True
        assert row["continue_status"] == {"supported": True, "writable": True,
                                          "reason_code": None, "reason": None}
    # Codex needs no transport probe: its app-server already appends with resume_thread_id.
    row = cap.agent_capability("codex", config={}, host="gw.example")
    assert row["continue_status"]["supported"] is True


def test_continue_stays_read_only_when_an_agent_is_not_admitted(installed, monkeypatch):
    """The refusal machinery is still load-bearing: drop the agent and the probe follows."""
    monkeypatch.setattr(linked_send_admission, "LINKED_SEND_ADMITTED", frozenset({"codex"}))
    path = _bin(installed, "claude")
    row = cap.agent_capability("claude", config={"model": {"claude_bin": str(path)}},
                               host="gw.example")
    assert row["store"]["readable"] is True and row["cli"]["available"] is True
    assert row["continue_status"] == {
        "supported": True, "writable": False, "reason_code": cap.REASON_SEND_READ_ONLY,
        "reason": ("Sending is read-only in this Hermes build — it cannot append to linked "
                   "Claude threads yet.")}


def test_continue_becomes_writable_when_the_adapter_and_admission_agree(installed, monkeypatch):
    """Flip exactly the two switches the resume work owns; the probe follows, nothing else."""
    _bin(installed, "claude")
    monkeypatch.setattr(cap, "linked_send_refusal", lambda agent: None)
    row = cap.agent_capability("claude", config={"model": {"claude_bin": "claude"}},
                               host="gw.example")
    assert row["store"]["readable"] is True and row["cli"]["available"] is True
    assert row["continue_status"] == {"supported": True, "writable": True,
                                      "reason_code": None, "reason": None}


def test_continue_prefers_the_first_failing_condition(installed, monkeypatch):
    """Store before CLI before admission: the reason names the one the user can act on."""
    monkeypatch.setattr(cap, "linked_send_refusal", lambda agent: None)
    cap_store = {"available": False, "readable": False, "reason_code": cap.REASON_STORE_MISSING,
                 "reason": "Mistral has no session store on gw.example."}
    cli = {"available": False, "reason_code": cap.REASON_CLI_MISSING,
           "reason": "Mistral is not installed on gw.example."}
    assert cap.continue_capability("mistral", store=cap_store, cli=cli)["reason_code"] == (
        cap.REASON_STORE_MISSING)
    good_store = {"available": True, "readable": True, "reason_code": None, "reason": None}
    assert cap.continue_capability("mistral", store=good_store, cli=cli)["reason_code"] == (
        cap.REASON_CLI_MISSING)


def test_capability_payload_carries_no_absolute_host_paths(installed):
    _bin(installed, "vibe")
    payload = json.dumps(cap.host_capabilities(config={}, host="gw.example"))
    assert str(installed) not in payload
    assert str(Path.home()) not in payload
