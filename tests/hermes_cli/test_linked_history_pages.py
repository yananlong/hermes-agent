"""Display-only linked history is paged backwards without losing the budget boundary."""

import json

import pytest

from hermes_cli.gateway_client import GatewayClientError
from hermes_cli.linked_history.pages import LinkedHistory
from hermes_cli.linked_history.viewer import _HistoryLayout, _lines


def test_initial_budget_retains_the_cursor_and_older_pages_remain_reachable(monkeypatch):
    from hermes_cli.linked_history import pages

    def row(text):
        return {"role": "user", "text": text}

    pages_by_cursor = {
        None: {"rows": [row("new" * 20)], "next_cursor": "before-new", "reset": True},
        "before-new": {"rows": [row("middle" * 10)], "next_cursor": "before-middle", "reset": False},
        "before-middle": {"rows": [row("old")], "next_cursor": None, "reset": False},
    }
    first_size = len(json.dumps(pages_by_cursor[None]["rows"]).encode())
    monkeypatch.setattr(pages, "INITIAL_BYTES", first_size + 1)
    calls = []

    def call(method, **params):
        calls.append((method, params))
        return {"session_id": params["session_id"], **pages_by_cursor[params.get("cursor")]}

    history = LinkedHistory(call, "owned-session")
    assert history.load_initial() == pages_by_cursor[None]["rows"]
    assert history.cursor == "before-new"
    assert history.load_older()
    assert history.has_newer
    assert history.load_older()
    assert [row["text"] for row in history.rows] == ["old"]
    assert history.cursor is None
    assert not history.load_older()
    assert history.load_newer()
    assert [row["text"] for row in history.rows] == ["middle" * 10]
    assert history.load_newer()
    assert [row["text"] for row in history.rows] == ["new" * 20]
    assert history.bytes <= pages.INITIAL_BYTES
    assert all(method == "session.transcript" and params["order"] == "latest" for method, params in calls)


def test_reset_never_splices_an_invalidated_window(monkeypatch):
    from hermes_cli.linked_history import pages
    monkeypatch.setattr(pages, "INITIAL_BYTES", 1)
    results = iter([
        {"session_id": "s", "rows": [{"role": "user", "text": "old-window"}],
         "next_cursor": "c", "reset": True},
        {"session_id": "s", "rows": [{"role": "user", "text": "new-window"}],
         "next_cursor": None, "reset": True},
    ])
    history = LinkedHistory(lambda *_a, **_k: next(results), "s")
    history.load_initial()
    with pytest.raises(GatewayClientError):
        history.load_older()
    assert history.rows == [{"role": "user", "text": "old-window"}]


def test_rejects_cross_session_or_nonadvancing_pages(monkeypatch):
    from hermes_cli.linked_history import pages
    monkeypatch.setattr(pages, "INITIAL_BYTES", 1)
    history = LinkedHistory(lambda *_a, **_k: {"session_id": "other", "rows": [],
                                               "next_cursor": None, "reset": True}, "s")
    with pytest.raises(GatewayClientError):
        history.load_initial()
    history = LinkedHistory(lambda *_a, **_k: {"session_id": "s", "rows": [],
                                               "next_cursor": "c", "reset": False}, "s")
    history.load_initial()
    with pytest.raises(GatewayClientError):
        history.load_older()


def test_viewer_keeps_complete_text_and_sanitizes_terminal_control_sequences():
    text = "start " + "word " * 200 + "end"
    lines = _lines([{"role": "assistant", "text": text + "\x1b]0;forged\x07"}], 40)
    assert "end" in "".join(lines)
    assert "forged" not in "".join(lines)
    assert "\x1b" not in "".join(lines)


def test_cursor_cycle_and_sparse_initial_history_are_bounded(monkeypatch):
    from hermes_cli.linked_history import pages
    monkeypatch.setattr(pages, "MAX_INITIAL_PAGES", 3)
    calls = []

    def sparse(_method, **params):
        calls.append(params.get("cursor"))
        return {"session_id": "s", "rows": [], "reset": params.get("cursor") is None,
                "next_cursor": f"c{len(calls)}"}

    history = LinkedHistory(sparse, "s")
    history.load_initial()
    assert len(calls) == 3
    assert history.cursor == "c3"

    # The cycle only becomes visible once the page cap is wider than the loop: with a 3-page
    # cap the walk above stops before it re-reads "a", which is the cap doing its job.
    monkeypatch.setattr(pages, "MAX_INITIAL_PAGES", 8)

    def cycle(_method, **params):
        cursor = params.get("cursor")
        return {"session_id": "s", "rows": [], "reset": cursor is None,
                "next_cursor": {None: "a", "a": "b", "b": "a"}[cursor]}

    with pytest.raises(GatewayClientError, match="repeating cursor"):
        LinkedHistory(cycle, "s").load_initial()


def test_narrow_view_renders_only_its_requested_portion():
    # Width 3 leaves an inner width of 1, so every character is its own visual row. Rows are
    # heading, one row per character, then a trailing blank: the last four rows are the three
    # characters of "end" and the blank.
    layout = _HistoryLayout([{"role": "assistant", "text": "x" * 2_000_000 + "end"}], 3)
    assert layout.total > 2_000_000
    assert "".join(layout.visible(layout.total - 4, 3)) == "end"
    assert "".join(layout.visible(layout.total - 5, 4)) == "xend"


def test_viewer_keeps_filtered_reasoning_and_full_tool_details():
    rows = [{"role": "assistant", "text": "", "reasoning": "safe reasoning"},
            {"role": "tool", "name": "terminal", "args": {"command": "run the full command"},
             "content": "result " + "x" * 1000 + " result-end"},
            {"role": "assistant", "display_kind": "hidden", "reasoning": "SECRET", "text": "SECRET"}]
    text = "\n".join(_lines(rows, 80))
    assert "safe reasoning" in text
    assert "run the full command" in text
    # Wrapping splits the line at the viewport edge, so compare with the wraps removed: the
    # point is that the tail survives, not that it lands on one visual row.
    assert "result-end" in text.replace("\n", "")
    assert "SECRET" not in text
