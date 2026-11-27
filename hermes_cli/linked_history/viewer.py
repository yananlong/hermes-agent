"""Interactive, scrollable display of linked transcript pages in the classic terminal."""

from __future__ import annotations

import json

from tools.ansi_strip import sanitize_display_text


def _segments(text):
    """Split on newlines lazily, including empty lines, without copying a large message."""
    start = 0
    while start <= len(text):
        end = text.find("\n", start)
        if end < 0:
            yield text[start:]
            return
        yield text[start:end]
        start = end + 1


def _wrapped_count(text, width):
    return sum(max(1, (len(line) + width - 1) // width) for line in _segments(text))


def _wrapped_slice(text, width, start, count):
    result = []
    for line in _segments(text):
        chunks = max(1, (len(line) + width - 1) // width)
        if start >= chunks:
            start -= chunks
            continue
        for index in range(start, min(chunks, start + count - len(result))):
            result.append(line[index * width:(index + 1) * width])
        start = 0
        if len(result) >= count:
            break
    return result


class _HistoryLayout:
    """Count wrapped rows without expanding an 8 MiB message into millions of Python strings."""

    def __init__(self, rows, width):
        self.width = max(1, width - 2)
        self.entries = []
        self.total = 0
        for row in rows:
            if row.get("display_kind") == "hidden":
                continue
            text = _row_text(row)
            if not text:
                continue
            text = sanitize_display_text(text)
            body = _wrapped_count(text, self.width)
            truncated = bool(row.get("truncated"))
            count = 2 + body + int(truncated)  # heading, body, optional marker, blank line
            self.entries.append((str(row.get("role") or "system").capitalize(), text, body, truncated, count))
            self.total += count

    def visible(self, start, count):
        result = []
        for role, text, body, truncated, entry_count in self.entries:
            if start >= entry_count:
                start -= entry_count
                continue
            if start == 0:
                result.append(f"{role}:")
            else:
                start -= 1
            if len(result) < count and start < body:
                result.extend(_wrapped_slice(text, self.width, start, count - len(result)))
                start = 0
            else:
                start = max(0, start - body)
            if len(result) < count and truncated:
                if start == 0:
                    result.append("[This message was truncated by the gateway.]")
                else:
                    start -= 1
            if len(result) < count and start == 0:
                result.append("")
            if len(result) >= count:
                break
            start = 0
        return result[:count]


def _row_text(row):
    """Only fields returned by the gateway's policy-filtered display projection."""
    parts = []
    for label, value in (("Reasoning", row.get("reasoning") or row.get("reasoning_content")),
                         ("Tool calls", row.get("tool_calls")),
                         ("Arguments", row.get("args")),
                         ("Result", row.get("content") if row.get("role") == "tool" else None)):
        if value:
            rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
            parts.append(f"{label}:\n{rendered}")
    if isinstance(row.get("text"), str) and row["text"]:
        parts.append(row["text"])
    if row.get("role") == "tool" and row.get("name"):
        parts.insert(0, str(row["name"]))
    return "\n".join(parts)


def _lines(rows, width):
    """Expanded rows for small displays and tests; the viewer itself only requests a viewport."""
    layout = _HistoryLayout(rows, width)
    return layout.visible(0, layout.total)


def show_linked_history(history):
    """Scroll with arrows, wheel or Page Up; q returns to chat."""
    try:
        import curses
    except ImportError:
        from cli import _cli_visible_print
        _cli_visible_print("The linked history viewer is unavailable in this terminal.")
        return

    def draw(screen):
        try:
            curses.curs_set(0)
            curses.mousemask(getattr(curses, "BUTTON4_PRESSED", 0) | getattr(curses, "BUTTON5_PRESSED", 0))
        except curses.error:
            # Some terminals cannot hide the cursor or report mouse buttons; keyboard browsing still works.
            pass
        screen.keypad(True)
        top, started, notice, layout_key, layout = 0, False, "", None, None
        while True:
            height, width = screen.getmaxyx()
            screen.erase()
            if height < 3 or width < 4:
                screen.addnstr(0, 0, "Resize terminal; q closes", max(0, width - 1))
                screen.refresh()
                if screen.getch() in (ord("q"), 27):
                    return
                continue
            visible = height - 2
            key = (history.revision, width)
            if key != layout_key:
                layout, layout_key = _HistoryLayout(history.rows, width), key
            if not started:
                top, started = max(0, layout.total - visible), True
            top = max(0, min(top, max(0, layout.total - visible)))
            title = "Linked history · ↑/↓ scroll · PgUp/PgDn page · q close"
            screen.addnstr(0, 0, title, width - 1)
            for index, line in enumerate(layout.visible(top, visible), 1):
                screen.addnstr(index, 0, line, width - 1)
            status = notice or ("Scroll up for older history" if history.cursor else "Start of available history")
            screen.addnstr(height - 1, 0, status, width - 1)
            screen.refresh()
            pressed = screen.getch()
            if pressed in (ord("q"), 27):
                return
            delta = {curses.KEY_UP: -1, ord("k"): -1, curses.KEY_DOWN: 1, ord("j"): 1,
                     curses.KEY_PPAGE: -visible, curses.KEY_NPAGE: visible}.get(pressed, 0)
            if pressed == curses.KEY_MOUSE:
                _, _, _, _, buttons = curses.getmouse()
                if buttons & getattr(curses, "BUTTON4_PRESSED", 0):
                    delta = -3
                elif buttons & getattr(curses, "BUTTON5_PRESSED", 0):
                    delta = 3
            if delta < 0 and top == 0 and history.cursor is not None:
                try:
                    history.load_older()
                    top = max(0, top + _HistoryLayout(history.last_added, width).total + delta)
                    notice = ""
                except Exception as exc:  # health: allow BLE001 -- history read failures stay in the viewer
                    notice = f"Could not load older history: {sanitize_display_text(str(exc))}"
            elif delta > 0 and top >= max(0, layout.total - visible) and history.has_newer:
                try:
                    history.load_newer()
                    top = max(0, top - _HistoryLayout(history.last_evicted, width).total + delta)
                    notice = ""
                except Exception as exc:  # health: allow BLE001 -- history read failures stay in the viewer
                    notice = f"Could not load newer history: {sanitize_display_text(str(exc))}"
            elif delta > 0 and top >= max(0, layout.total - visible) and history.newer_lost:
                notice = "Reopen /history to return to the latest messages."
            else:
                top += delta

    curses.wrapper(draw)
