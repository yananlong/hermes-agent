"""The classic CLI's two resume paths render a linked Codex pointer's native transcript.

A linked row is a locator: Hermes stores no transcript for it, so the local mirror is empty (or
stale) and the pre-fix CLI printed "found but has no messages. Starting fresh." The display rows
must come from the native thread through the canonical stored read (``session.transcript``,
``order="latest"``), which pages the native trace newest-first and applies the owning profile's
saved display policy (a turn with no settled overlay fails closed).

Both branches are pinned here: the startup ``--resume`` preload (which reads
``get_resume_conversations``) and its late ``-q`` twin (``get_messages_as_conversation``).
"""

from __future__ import annotations

from hermes_cli.gateway_client import GatewayClientError
from hermes_cli.gateway_session_store import GatewaySessionStore
from hermes_cli.linked_history.pages import LinkedHistory

SID = "20261126_000000_deadbeef0001"


def rows(*texts, role="user"):
    return [{"role": role, "text": text, "row_kind": "message"} for text in texts]


class Owner:
    """A scripted owner: ``session.transcript`` pages a thread from its TAIL like the gateway.

    ``order="latest"`` on the real RPC returns the newest rows (ascending inside the page) and a
    ``next_cursor`` that walks toward the head, so ``cursor`` here is a distance from the tail.
    """

    def __init__(self, native_rows, *, unavailable=None, page_rows=100):
        self.native_rows = list(native_rows)
        self.unavailable = unavailable
        self.page_rows = page_rows
        self.calls: list[dict] = []

    def call(self, method, **params):
        self.calls.append({"method": method, **params})
        if self.unavailable is not None:
            raise self.unavailable
        assert method == "session.transcript", method
        assert params["order"] == "latest"
        size = min(params["limit"], self.page_rows)
        consumed = int(params.get("cursor") or 0)
        end = len(self.native_rows) - consumed
        start = max(0, end - size)
        # ``reset`` is True only on a cursor-less read: the client's mid-window guard treats a
        # reset with a cursor as "the snapshot moved under you".
        return {"session_id": params["session_id"],
                "rows": self.native_rows[start:end],
                "next_cursor": str(consumed + (end - start)) if start > 0 else None,
                "reset": params.get("cursor") is None}


class Store(GatewaySessionStore):
    """The store's real linked-history methods over a scripted owner (no gateway connection)."""

    def __init__(self, owner, *, linked=True, store_rows=None):
        self._linked_histories = {}
        self.connection = None
        self.owner = owner
        self.linked = linked
        self.store_rows = store_rows or ([], [])

    def __bool__(self):
        return True

    def call(self, method, **params):
        return self.owner.call(method, **params)

    def _db(self):
        store_rows = self.store_rows

        class Reader:
            def get_resume_conversations(self, _sid, _rows=store_rows):
                return _rows

            def get_messages_as_conversation(self, _sid, **_kwargs):
                raise AssertionError("a pointer row must not be read from the local mirror")

        return Reader()

    def _linked_codex(self, session_id):
        self.owner.calls.append({"method": "_linked_codex", "session_id": session_id})
        return self.linked


def test_initial_load_is_the_newest_rows_and_reads_newest_page_first():
    """(b) Latest-first: the first read is the tail, and later reads walk toward the head."""
    owner = Owner(rows(*[f"message-{number}" for number in range(300)]), page_rows=100)
    view = LinkedHistory(owner.call, SID)
    view.load_initial()

    assert [row["text"] for row in view.rows] == [f"message-{number}" for number in range(300)]
    # Three 100-row pages, each ascending: the window is intact and starts at message-0 only
    # because the whole thread fits three pages.
    reads = [call for call in owner.calls if call["method"] == "session.transcript"]
    assert reads[0].get("cursor") is None, "the first read must anchor at the tail"
    assert all(call.get("cursor") is not None for call in reads[1:])
    assert view.cursor is None, "the head of this thread was reached"
    assert view.bytes <= 8 * 1024 * 1024


def test_a_long_tail_thread_yields_its_newest_window_not_its_head(monkeypatch):
    """(b) The window is anchored at the tail: a thread longer than the cap keeps its newest rows."""
    from hermes_cli.linked_history import pages

    monkeypatch.setattr(pages, "PAGE_ROWS", 2)
    monkeypatch.setattr(pages, "INITIAL_BYTES", 2 * 4096)
    big = [{"role": "user", "text": "x" * 4096 + f"-{number}", "row_kind": "message"}
           for number in range(50)]
    owner = Owner(big)
    view = LinkedHistory(owner.call, SID)
    view.load_initial()

    reads = [call for call in owner.calls if call["method"] == "session.transcript"]
    assert len(reads) < len(big), "the initial load must not truncate through the whole thread"
    assert view.rows[-1]["text"].endswith("-49"), "the newest row must be resident"
    assert view.cursor is not None, "the older boundary must stay reachable"
    assert not view.rows[0]["text"].endswith("-0"), "the head must NOT be what was loaded"


def test_codex_failure_yields_one_honest_line_and_does_not_block_resume():
    """(c) Codex down is a normal state: one honest notice, no crash, nothing cached."""
    failure = GatewayClientError("Gateway disconnected; turn outcome is unknown.")
    store = Store(Owner([], unavailable=failure))

    model, display = store.get_resume_conversations(SID)

    assert model == [] and len(display) == 1
    notice = display[0]
    assert notice["display_kind"] == "codex_history_unavailable"
    assert "unavailable" in notice["content"].lower()
    assert store._linked_histories == {}, "a failed read must not cache a half-built window"


def test_a_failed_read_does_not_poison_the_next_attempt():
    """(c) The next resume retries instead of serving a cached empty window."""
    attempts = {"left": 1}

    class Flaky(Owner):
        def call(self, method, **params):
            if attempts["left"]:
                attempts["left"] -= 1
                raise GatewayClientError("Codex is not running")
            return super().call(method, **params)

    store = Store(Flaky(rows("native question", "native answer")))
    _model, first = store.get_resume_conversations(SID)
    assert first[0]["display_kind"] == "codex_history_unavailable"

    _model, second = store.get_resume_conversations(SID)
    assert [row["content"] for row in second] == ["native question", "native answer"]


def test_a_linked_row_never_exposes_the_stale_local_mirror_as_model_history():
    """B8: the model projection of a pointer row is EMPTY, whatever the local mirror holds.

    The model projection is what a resume hands the agent as conversation history. For a linked
    row the authority is the native thread, and the gateway re-attaches by ``resume_thread_id``;
    the local mirror holds only the turns Hermes itself submitted. Feeding it to the agent is how
    a resumed linked session ends up with a second, divergent transcript.
    """
    owner = Owner(rows("native question", "native answer", role="user"))
    store = Store(owner, store_rows=(
        [{"role": "user", "content": "STALE mirror question"},
         {"role": "assistant", "content": "STALE mirror answer"}],
        [{"role": "user", "content": "STALE mirror question"}],
    ))

    model, display = store.get_resume_conversations(SID)

    assert model == [], "a linked row must not hand the agent its local mirror as history"
    # The display projection is the native window, not the mirror.
    assert [row["text"] for row in display] == ["native question", "native answer"]
    assert not any("STALE" in str(row) for row in display)


def test_a_linked_row_keeps_an_empty_model_projection_when_codex_is_down():
    """The honest-notice path must not resurrect the mirror either."""
    owner = Owner([], unavailable=GatewayClientError("owner is down"))
    store = Store(owner, store_rows=(
        [{"role": "user", "content": "STALE mirror question"}],
        [{"role": "user", "content": "STALE mirror question"}],
    ))

    model, display = store.get_resume_conversations(SID)

    assert model == []
    assert [row["display_kind"] for row in display] == ["codex_history_unavailable"]


def test_an_ordinary_session_row_never_constructs_a_linked_window():
    """(d) An ordinary row keeps its store history and makes no linked read at all."""
    owner = Owner([])
    store = Store(owner, linked=False,
                  store_rows=([{"role": "user", "content": "hello"}],
                              [{"role": "user", "content": "hello"}]))

    model, display = store.get_resume_conversations(SID)

    assert model == [{"role": "user", "content": "hello"}]
    assert display == [{"role": "user", "content": "hello"}]
    assert store._linked_histories == {}
    assert not any(call["method"] == "session.transcript" for call in owner.calls), owner.calls


# -- the deadlines themselves (both CLI resume branches) ------------------------------------


def _bare_cli(store, **fields):
    """Bare ``HermesCLI`` the way the resume branches see it (no ``__init__``)."""
    import cli as cli_mod

    stub = object.__new__(cli_mod.HermesCLI)
    stub._session_db = store
    stub.session_id = SID
    stub._resumed = True
    stub.conversation_history = []
    stub._resume_display_history = []
    stub._console_print = lambda *_a, **_k: None
    stub._resume_read_id = SID
    stub.resume_display = "minimal"
    stub.tool_progress_mode = "full"
    stub.agent = None
    for key, value in fields.items():
        setattr(stub, key, value)
    return stub


def test_the_late_resume_branch_shows_native_turns_and_keeps_history_empty():
    """(a) ``hermes chat -q --resume`` takes this branch; it must not read the local mirror."""
    store = Store(Owner(rows("late native question", "late native answer")))
    stub = _bare_cli(store)
    stub._follow_compression_chain = lambda meta, _announce: meta
    stub._resume_history_limit_error = lambda **_k: None
    stub._restore_session_state = lambda *_a, **_k: None
    stub._reopen_session = lambda: None
    store.get_session = lambda _sid: {"title": "linked"}

    assert stub._load_resumed_history_late() is True
    # The recap reads the native rows ...
    assert [row["content"] for row in stub._resume_display_history] == [
        "late native question", "late native answer"]
    # ... while conversation_history stays empty: a pointer must never look Hermes-owned.
    assert stub.conversation_history == []


def test_the_late_resume_branch_opens_the_session_when_codex_is_down(capsys):
    """(c) A failed native read prints ONE honest line and still opens the session.

    Driven in quiet mode (``-q``, the branch's real caller): status lines go to stderr so stdout
    stays machine-readable, which is also where this one has to land.
    """
    store = Store(Owner([], unavailable=GatewayClientError("Codex app-server is not running")))
    store.get_session = lambda _sid: {"title": "linked"}
    stub = _bare_cli(store, tool_progress_mode="off")
    stub._follow_compression_chain = lambda meta, _announce: meta
    stub._resume_history_limit_error = lambda **_k: None
    stub._restore_session_state = lambda *_a, **_k: None
    stub._reopen_session = lambda: None

    assert stub._load_resumed_history_late() is True, "a history read must not block resume"
    err = capsys.readouterr().err
    honest = [line for line in err.splitlines() if "could not be loaded" in line]
    assert len(honest) == 1, err
    assert "not running" in honest[0]
    assert "no messages" not in err


def test_the_late_resume_branch_is_untouched_for_an_ordinary_row():
    """(d) An ordinary row still reads its stored rows; no Codex call is ever attempted."""
    owner = Owner([])
    store = Store(owner, linked=False)
    saved = iter(([{"role": "user", "content": "stored question"},
                   {"role": "assistant", "content": "stored answer"}],))

    def stored_read(session_id, **_kwargs):
        assert session_id == SID
        return next(saved)

    store.get_messages_as_conversation = stored_read
    stub = _bare_cli(store)
    stub._follow_compression_chain = lambda meta, _announce: meta
    stub._resume_history_limit_error = lambda **_k: None
    stub._restore_session_state = lambda *_a, **_k: None
    stub._reopen_session = lambda: None
    store.get_session = lambda _sid: {"title": "ordinary"}

    assert stub._load_resumed_history_late() is True
    assert stub.conversation_history == [{"role": "user", "content": "stored question"},
                                         {"role": "assistant", "content": "stored answer"}]
    # The linked reader never even asked whether this row is a pointer's: no transcript read.
    assert not any(call["method"] == "session.transcript" for call in owner.calls), owner.calls


def test_mid_chat_resume_renders_the_native_window_and_never_fills_history(monkeypatch):
    """(a) ``/resume <linked>`` mid-chat: recap from the native thread, history still empty."""
    from hermes_cli.cli_commands_mixin import CLICommandsMixin

    store = Store(Owner(rows("mid-chat native question", "mid-chat native answer")))
    store.get_session = lambda _sid: {"title": "linked", "id": SID}
    store.resolve_resume_session_id = lambda sid: sid
    stub = _bare_cli(store, session_id="previous-session")
    stub._session_db = store
    stub._agent_running = False
    stub._pending_resume_sessions = None
    stub._pending_title = None
    stub._resume_history_error = None
    stub._show_recent_sessions = lambda **_k: True
    stub._restore_session_cwd = lambda *_a, **_k: None
    stub._restore_session_yolo = lambda *_a, **_k: None
    stub._restore_session_model = lambda *_a, **_k: None
    stub._display_resumed_history = lambda: None

    monkeypatch.setattr("hermes_cli.cli_commands_mixin._cp", lambda *_a, **_k: None)
    monkeypatch.setattr("hermes_cli.cli_commands_mixin._end_current_session", lambda *_a, **_k: None)
    monkeypatch.setattr("hermes_cli.cli_commands_mixin._sync_agent_to_session", lambda *_a, **_k: None)
    monkeypatch.setattr("hermes_cli.cli_commands_mixin._gateway_open", lambda _db, sid: (sid, None))
    # Imported inside the handler (``from cli import _sync_process_session_id``).
    monkeypatch.setattr("cli._sync_process_session_id", lambda *_a, **_k: None)
    monkeypatch.setattr(CLICommandsMixin, "_resolve_resume_target",
                        lambda _self, _target: (SID, {"title": "linked", "id": SID}))

    stub._handle_resume_command(f"/resume {SID}")

    assert stub.session_id == SID
    assert [row["content"] for row in stub._resume_display_history] == [
        "mid-chat native question", "mid-chat native answer"]
    assert stub.conversation_history == []
    assert stub._resume_history_loaded_for == SID
