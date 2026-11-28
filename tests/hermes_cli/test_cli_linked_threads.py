"""``/link-thread`` in the classic CLI: argument parsing, host-capability gating, and the RPCs.

Behaviour, not the source's shape: every case drives :func:`linked_agent_states` or the command
handler with the payloads a gateway actually answers, and asserts what the user would see. The
TUI's ``linkedThreads.ts`` and this module must agree, so the vocabulary cases are the contract
both surfaces are checked against.
"""

from __future__ import annotations

from hermes_cli.cli_linked_threads import (
    link_method,
    link_params,
    linked_agent_states,
    parse_link_arg,
)


def _status(*agents):
    """A ``session.foreign.status`` result carrying the given per-agent rows."""
    return {"host": "testhost", "agents": list(agents)}


def _call_returning(payload, seen=None):
    def call(method, **params):
        if seen is not None:
            seen.append((method, params))
        return payload
    return call


def _call_raising(exc):
    def call(method, **params):
        raise exc
    return call


# -- argument parsing ---------------------------------------------------------------------------


def test_agent_and_thread_id_are_both_parsed():
    assert parse_link_arg("codex 01a212d9") == ("codex", "01a212d9")


def test_thread_id_alone_leaves_the_agent_to_the_capability_answer():
    assert parse_link_arg("01a212d9") == (None, "01a212d9")


def test_bare_and_agent_only_arguments_browse():
    assert parse_link_arg("") == (None, "")
    assert parse_link_arg("claude") == ("claude", "")
    assert parse_link_arg("   ") == (None, "")


def test_agent_name_is_matched_case_insensitively():
    assert parse_link_arg("Codex 0a1b") == ("codex", "0a1b")


def test_an_unknown_first_token_is_treated_as_a_thread_id():
    """Only the three real agents name an agent; anything else is an id, never a silent drop."""
    assert parse_link_arg("gemini 0a1b") == (None, "gemini")


def test_extra_tokens_after_the_thread_id_are_ignored():
    assert parse_link_arg("codex 0a1b extra") == ("codex", "0a1b")


# -- method and parameter vocabulary ------------------------------------------------------------


def test_each_agent_has_its_own_link_method():
    assert link_method("codex") == "session.codex.link"
    assert link_method("claude") == "session.claude.link"
    assert link_method("mistral") == "session.mistral.link"


def test_codex_takes_thread_id_while_the_siblings_take_session_id():
    """The wire contract differs per agent; sending the wrong key is a 4000 invalid_params."""
    assert link_params("codex", "X") == {"thread_id": "X"}
    assert link_params("claude", "X") == {"session_id": "X"}
    assert link_params("mistral", "X") == {"session_id": "X"}


# -- host capability ---------------------------------------------------------------------------


def test_all_three_agents_are_reported_in_order_even_when_absent():
    states = linked_agent_states(_call_returning(_status()))
    assert [state["agent"] for state in states] == ["codex", "claude", "mistral"]
    assert all(state["writable"] is False for state in states)


def test_an_explicit_writable_verdict_is_the_only_thing_that_allows_linking():
    states = linked_agent_states(_call_returning(_status(
        {"agent": "codex", "label": "Codex", "continue_status": {"writable": True}},
        {"agent": "claude", "label": "Claude Code",
         "continue_status": {"writable": False, "reason": "claude CLI not on PATH"}},
    )))
    by_agent = {state["agent"]: state for state in states}
    assert by_agent["codex"]["writable"] is True
    assert by_agent["codex"]["reason"] == ""
    assert by_agent["claude"]["writable"] is False
    assert by_agent["claude"]["reason"] == "claude CLI not on PATH"


def test_a_truthy_but_not_boolean_writable_does_not_count():
    """Only ``is True`` counts: a gateway that answers the string "yes" is not a yes."""
    states = linked_agent_states(_call_returning(_status(
        {"agent": "codex", "continue_status": {"writable": "yes"}})))
    assert states[0]["writable"] is False


def test_a_reason_is_lifted_from_whichever_capability_section_carries_one():
    for section in ("continue_status", "store", "cli"):
        states = linked_agent_states(_call_returning(_status(
            {"agent": "mistral", "label": "Mistral Vibe", section: {"reason": f"no {section}"}})))
        assert states[2]["reason"] == f"no {section}"


def test_an_agent_the_host_never_mentions_is_unavailable_not_writable():
    states = linked_agent_states(_call_returning(_status(
        {"agent": "codex", "continue_status": {"writable": True}})))
    assert states[0]["writable"] is True
    assert states[1]["writable"] is False
    assert "did not report" in states[1]["reason"]


def test_a_backend_without_the_method_is_reported_as_no_answer():
    """The stale-owner verdict: an owner older than the feature answers -32601, which is a
    different problem from an agent being unavailable and must not read as three refusals."""
    assert linked_agent_states(_call_raising(RuntimeError("-32601 unknown method"))) is None


def test_a_malformed_answer_is_treated_as_no_answer():
    assert linked_agent_states(_call_returning({"host": "testhost"})) is None
    assert linked_agent_states(_call_returning({"agents": "not-a-list"})) is None


def test_the_status_probe_asks_the_one_method_without_a_version_probe():
    seen: list = []
    linked_agent_states(_call_returning(_status(), seen))
    assert seen == [("session.foreign.status", {"probe_version": False})]
