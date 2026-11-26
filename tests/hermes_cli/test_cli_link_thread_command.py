"""``/link-thread`` command behaviour through the classic CLI's dispatch surface.

Drives the real handler with a fake gateway owner, so each case asserts the sentence a user gets
and the RPC that produced it. A fake owner is the honest seam here: the handler's contract is
"ask the owner, then report", and the owner is a thin client by design.
"""

from __future__ import annotations

import pytest

from hermes_cli.cli_commands_mixin import CLICommandsMixin
from hermes_cli.cli_session_mixin import CLISessionMixin


class Owner:
    """A gateway owner stand-in that records calls and answers scripted results."""

    def __init__(self, status=None, links=None, listings=None, fail=None):
        self.status = status if status is not None else {"host": "h", "agents": []}
        self.links = links or {}
        self.listings = listings or {}
        self.fail = fail or {}
        self.calls: list[tuple[str, dict]] = []

    def call(self, method, **params):
        self.calls.append((method, params))
        if method in self.fail:
            raise self.fail[method]
        if method == "session.foreign.status":
            return self.status
        if method.endswith(".link"):
            return self.links[method]
        if method.endswith(".list"):
            return self.listings.get(method, {"threads": []})
        raise AssertionError(f"unexpected method {method}")


def _writable(agent, label):
    return {"agent": agent, "label": label, "continue_status": {"writable": True}}


def _unwritable(agent, label, reason):
    return {"agent": agent, "label": label, "continue_status": {"writable": False, "reason": reason}}


class CLI(CLICommandsMixin, CLISessionMixin):
    """Just the handlers under test plus the attributes the CLI would have."""

    _agent_running = False

    def __init__(self, owner, resumed=None):
        self._session_db = owner
        self.resumed = resumed if resumed is not None else []
        self._pending_link_threads = None

    def _handle_resume_command(self, cmd_original):
        self.resumed.append(cmd_original)


@pytest.fixture
def outputs(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr("hermes_cli.cli_commands_mixin._cp", lambda *lines: seen.extend(lines))
    monkeypatch.setattr("cli._cprint", lambda text, *a, **k: seen.append(text))
    return seen


def test_a_link_resumes_the_pointer_row_the_owner_created(outputs):
    owner = Owner(status={"agents": [_writable("codex", "Codex")]},
                  links={"session.codex.link": {"session_id": "s-1", "already_linked": False}})
    cli = CLI(owner)
    cli._handle_link_thread_command("/link-thread codex 01a212d9")
    assert owner.calls[-1] == ("session.codex.link", {"thread_id": "01a212d9"})
    assert cli.resumed == ["/resume s-1"]


def test_an_already_linked_thread_says_so_and_still_opens(outputs):
    owner = Owner(status={"agents": [_writable("codex", "Codex")]},
                  links={"session.codex.link": {"session_id": "s-1", "already_linked": True}})
    cli = CLI(owner)
    cli._handle_link_thread_command("/link-thread codex 01a212d9")
    assert any("already linked" in line for line in outputs)
    assert cli.resumed == ["/resume s-1"]


def test_claude_and_mistral_send_session_id_not_thread_id(outputs):
    for agent in ("claude", "mistral"):
        owner = Owner(status={"agents": [_writable(agent, agent.title())]},
                      links={f"session.{agent}.link": {"session_id": "s-2", "already_linked": False}})
        CLI(owner)._handle_link_thread_command(f"/link-thread {agent} 0b2c")
        assert owner.calls[-1] == (f"session.{agent}.link", {"session_id": "0b2c"})


def test_an_unavailable_agent_refuses_with_the_host_reason_and_links_nothing(outputs):
    owner = Owner(status={"agents": [_unwritable("codex", "Codex", "codex CLI not on PATH")]})
    cli = CLI(owner)
    cli._handle_link_thread_command("/link-thread codex 01a212d9")
    assert any("codex CLI not on PATH" in line for line in outputs)
    assert not any(method.endswith(".link") for method, _ in owner.calls)
    assert cli.resumed == []


def test_a_thread_id_alone_links_when_exactly_one_agent_can_serve_it(outputs):
    owner = Owner(status={"agents": [_writable("codex", "Codex"),
                                     _unwritable("claude", "Claude Code", "no claude")]},
                  links={"session.codex.link": {"session_id": "s-3", "already_linked": False}})
    cli = CLI(owner)
    cli._handle_link_thread_command("/link-thread 01a212d9")
    assert owner.calls[-1] == ("session.codex.link", {"thread_id": "01a212d9"})
    assert cli.resumed == ["/resume s-3"]


def test_a_thread_id_alone_asks_which_agent_when_several_could_serve_it(outputs):
    owner = Owner(status={"agents": [_writable("codex", "Codex"), _writable("claude", "Claude Code")]})
    cli = CLI(owner)
    cli._handle_link_thread_command("/link-thread 01a212d9")
    assert any("Name the agent" in line and "01a212d9" in line for line in outputs)
    assert not any(method.endswith(".link") for method, _ in owner.calls)


def test_no_writable_agent_reports_every_reason(outputs):
    owner = Owner(status={"agents": [_unwritable("codex", "Codex", "no codex"),
                                     _unwritable("claude", "Claude Code", "no claude")]})
    CLI(owner)._handle_link_thread_command("/link-thread 01a212d9")
    joined = " ".join(outputs)
    assert "no codex" in joined and "no claude" in joined


def test_a_stale_owner_is_reported_as_a_backend_problem_not_three_refusals(outputs):
    owner = Owner(fail={"session.foreign.status": RuntimeError("-32601 unknown method")})
    CLI(owner)._handle_link_thread_command("/link-thread codex 01a212d9")
    joined = " ".join(outputs)
    assert "session.foreign.status" in joined
    assert "Restart the gateway owner" in joined


def test_a_failed_link_reports_the_error_without_resuming(outputs):
    owner = Owner(status={"agents": [_writable("codex", "Codex")]},
                  fail={"session.codex.link": RuntimeError("Codex did not return the requested thread")})
    cli = CLI(owner)
    cli._handle_link_thread_command("/link-thread codex 01a212d9")
    assert any("Codex did not return the requested thread" in line for line in outputs)
    assert cli.resumed == []


def test_a_link_answer_without_a_session_id_is_reported(outputs):
    owner = Owner(status={"agents": [_writable("codex", "Codex")]},
                  links={"session.codex.link": {"already_linked": False}})
    cli = CLI(owner)
    cli._handle_link_thread_command("/link-thread codex 01a212d9")
    assert any("Invalid response" in line for line in outputs)
    assert cli.resumed == []


def test_a_running_turn_refuses_before_touching_the_owner(outputs):
    owner = Owner(status={"agents": [_writable("codex", "Codex")]})
    cli = CLI(owner)
    cli._agent_running = True
    cli._handle_link_thread_command("/link-thread codex 01a212d9")
    assert owner.calls == []


# -- the browse step ---------------------------------------------------------------------------


def test_bare_link_thread_lists_native_threads_and_arms_one_shot_selection(outputs):
    owner = Owner(
        status={"agents": [_writable("codex", "Codex")]},
        listings={"session.codex.list": {"threads": [
            {"id": "01a212d9", "title": "Hermes: add capabilities"},
            {"id": "01b2", "title": "other work"}]}},
        links={"session.codex.link": {"session_id": "s-9", "already_linked": False}})
    cli = CLI(owner)
    cli._handle_link_thread_command("/link-thread")
    assert any("Hermes: add capabilities" in line for line in outputs)
    assert [row["id"] for row in cli._pending_link_threads] == ["01a212d9", "01b2"]
    # the one-shot number the user then types
    assert cli._consume_pending_link_selection("2") is True
    assert owner.calls[-1] == ("session.codex.link", {"thread_id": "01b2"})
    assert cli.resumed == ["/resume s-9"]


def test_the_pending_selection_is_one_shot(outputs):
    owner = Owner(status={"agents": [_writable("codex", "Codex")]},
                  listings={"session.codex.list": {"threads": [{"id": "a", "title": "t"}]}})
    cli = CLI(owner)
    cli._handle_link_thread_command("/link-thread")
    assert cli._consume_pending_link_selection("1") is True
    assert cli._pending_link_threads is None
    assert cli._consume_pending_link_selection("1") is False


def test_a_non_numeric_reply_does_not_select(outputs):
    cli = CLI(Owner(status={"agents": [_writable("codex", "Codex")]}))
    cli._pending_link_threads = [{"agent": "codex", "id": "a", "title": "t", "label": "Codex"}]
    assert cli._consume_pending_link_selection("hello") is False
    assert cli._pending_link_threads is None


def test_an_out_of_range_number_reports_and_links_nothing(outputs):
    owner = Owner(status={"agents": [_writable("codex", "Codex")]})
    cli = CLI(owner)
    cli._pending_link_threads = [{"agent": "codex", "id": "a", "title": "t", "label": "Codex"}]
    assert cli._consume_pending_link_selection("7") is True
    assert any("No thread 7" in line for line in outputs)
    assert not any(method.endswith(".link") for method, _ in owner.calls)


def test_one_agents_failing_store_does_not_hide_anothers_threads(outputs):
    owner = Owner(
        status={"agents": [_writable("codex", "Codex"), _writable("claude", "Claude Code")]},
        listings={"session.claude.list": {"threads": [{"id": "c1", "title": "claude work"}]}},
        fail={"session.codex.list": RuntimeError("daemon unavailable")})
    cli = CLI(owner)
    cli._handle_link_thread_command("/link-thread")
    assert any("daemon unavailable" in line for line in outputs)
    assert [row["id"] for row in cli._pending_link_threads] == ["c1"]


def test_a_thread_id_alone_resolves_its_owning_agent_from_the_stores(outputs):
    """The id names its agent: ask each writable store, and link the unique match."""
    owner = Owner(status={"agents": [_writable("codex", "Codex"), _writable("claude", "Claude Code")]},
                  listings={"session.claude.list": {"threads": [{"id": "0b2c", "title": "claude work"}]},
                            "session.codex.list": {"threads": [{"id": "0a1b", "title": "codex work"}]}},
                  links={"session.claude.link": {"session_id": "s-4", "already_linked": False}})
    cli = CLI(owner)
    cli._handle_link_thread_command("/link-thread 0b2c")
    assert owner.calls[-1] == ("session.claude.link", {"session_id": "0b2c"})
    assert cli.resumed == ["/resume s-4"]


def test_an_id_no_store_lists_still_asks_which_agent(outputs):
    owner = Owner(status={"agents": [_writable("codex", "Codex"), _writable("claude", "Claude Code")]},
                  listings={"session.codex.list": {"threads": [{"id": "0a1b", "title": "x"}]},
                            "session.claude.list": {"threads": [{"id": "0b2c", "title": "y"}]}})
    cli = CLI(owner)
    cli._handle_link_thread_command("/link-thread 0d4e")
    assert any("Name the agent" in line for line in outputs)
    assert not any(method.endswith(".link") for method, _ in owner.calls)


def test_an_id_listed_by_two_stores_is_not_guessed(outputs):
    """Two stores claiming one id is ambiguous: linking the wrong one fails on the next send."""
    owner = Owner(status={"agents": [_writable("codex", "Codex"), _writable("claude", "Claude Code")]},
                  listings={"session.codex.list": {"threads": [{"id": "dup", "title": "x"}]},
                            "session.claude.list": {"threads": [{"id": "dup", "title": "y"}]}})
    cli = CLI(owner)
    cli._handle_link_thread_command("/link-thread dup")
    assert any("Name the agent" in line for line in outputs)
    assert not any(method.endswith(".link") for method, _ in owner.calls)


def test_a_number_after_the_picker_links_that_row_without_naming_an_agent(outputs):
    """The picker already named the agent, so `/link 1` must not ask for one again."""
    owner = Owner(
        status={"agents": [_writable("codex", "Codex"), _writable("claude", "Claude Code")]},
        listings={"session.codex.list": {"threads": [{"id": "01a212d9", "title": "Hermes: add caps"}]}},
        links={"session.codex.link": {"session_id": "s-5", "already_linked": False}})
    cli = CLI(owner)
    cli._handle_link_thread_command("/link-thread")
    assert cli._pending_link_threads
    cli._handle_link_thread_command("/link-thread 1")
    assert owner.calls[-1] == ("session.codex.link", {"thread_id": "01a212d9"})
    assert cli.resumed == ["/resume s-5"]


def test_an_out_of_range_number_after_the_picker_is_reported_as_such(outputs):
    owner = Owner(status={"agents": [_writable("codex", "Codex")]},
                  listings={"session.codex.list": {"threads": [{"id": "a", "title": "t"}]}})
    cli = CLI(owner)
    cli._handle_link_thread_command("/link-thread")
    cli._handle_link_thread_command("/link-thread 9")
    assert any("No thread 9" in line for line in outputs)
    assert not any(method.endswith(".link") for method, _ in owner.calls)


def test_a_uuid_after_the_picker_is_still_a_thread_id_not_an_index(outputs):
    """Native ids are never bare integers, so a real id must ignore the pending list."""
    owner = Owner(status={"agents": [_writable("codex", "Codex")]},
                  links={"session.codex.link": {"session_id": "s-6", "already_linked": False}})
    cli = CLI(owner)
    cli._pending_link_threads = [{"agent": "claude", "id": "other", "title": "t", "label": "Claude Code"}]
    cli._handle_link_thread_command("/link-thread codex 01a212d9")
    assert owner.calls[-1] == ("session.codex.link", {"thread_id": "01a212d9"})
    assert cli.resumed == ["/resume s-6"]


def test_a_number_selects_without_a_bare_link_first(outputs):
    """/link 1 must work as a first command: the list is fetched rather than demanded."""
    owner = Owner(
        status={"agents": [_writable("codex", "Codex"), _writable("claude", "Claude Code")]},
        listings={"session.codex.list": {"threads": [{"id": "01a212d9", "title": "Hermes: add caps"},
                                                     {"id": "0b2c", "title": "second"}]}},
        links={"session.codex.link": {"session_id": "s-7", "already_linked": False}})
    cli = CLI(owner)
    cli._handle_link_thread_command("/link-thread 2")
    assert owner.calls[-1] == ("session.codex.link", {"thread_id": "0b2c"})
    assert cli.resumed == ["/resume s-7"]


def test_a_number_out_of_range_on_a_first_command_lists_and_says_so(outputs):
    owner = Owner(status={"agents": [_writable("codex", "Codex")]},
                  listings={"session.codex.list": {"threads": [{"id": "a", "title": "only one"}]}})
    cli = CLI(owner)
    cli._handle_link_thread_command("/link-thread 5")
    joined = " ".join(outputs)
    assert "only one" in joined          # the list it referred to is shown
    assert "No thread 5" in joined
    assert not any(method.endswith(".link") for method, _ in owner.calls)


def test_a_number_browses_only_the_agent_it_named(outputs):
    owner = Owner(status={"agents": [_writable("codex", "Codex"), _writable("claude", "Claude Code")]},
                  listings={"session.claude.list": {"threads": [{"id": "c1", "title": "claude work"}]}},
                  links={"session.claude.link": {"session_id": "s-8", "already_linked": False}})
    cli = CLI(owner)
    cli._handle_link_thread_command("/link-thread claude 1")
    assert [m for m, _ in owner.calls if m.endswith(".list")] == ["session.claude.list"]
    assert owner.calls[-1] == ("session.claude.link", {"session_id": "c1"})


def test_a_number_is_never_mistaken_for_a_thread_id(outputs):
    """Real ids are UUIDs, so a digit run is unambiguously a list position."""
    owner = Owner(status={"agents": [_writable("codex", "Codex")]},
                  listings={"session.codex.list": {"threads": [{"id": "01a212d9", "title": "t"}]}},
                  links={"session.codex.link": {"session_id": "s-9b", "already_linked": False}})
    cli = CLI(owner)
    cli._handle_link_thread_command("/link-thread 1")
    linked_id = owner.calls[-1][1].get("thread_id")
    assert linked_id == "01a212d9"


def test_no_threads_anywhere_says_so_without_arming_a_selection(outputs):
    owner = Owner(status={"agents": [_writable("codex", "Codex")]})
    cli = CLI(owner)
    cli._handle_link_thread_command("/link-thread")
    assert any("No native threads" in line for line in outputs)
    assert cli._pending_link_threads is None


def test_browsing_one_agent_never_lists_the_others(outputs):
    owner = Owner(status={"agents": [_writable("codex", "Codex"), _writable("claude", "Claude Code")]})
    CLI(owner)._handle_link_thread_command("/link-thread claude")
    assert [method for method, _ in owner.calls if method.endswith(".list")] == ["session.claude.list"]
