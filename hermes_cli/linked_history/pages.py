"""A display-only, newest-first window into the gateway's stored transcript."""

from __future__ import annotations

import json
import hashlib

from hermes_cli.gateway_client import GatewayClientError

INITIAL_BYTES = 8 * 1024 * 1024
PAGE_ROWS = 500
MAX_PAGE_MARKERS = 8192
MAX_INITIAL_PAGES = 32


class LinkedHistory:
    """A window bound to one gateway session. Older pages precede the existing window."""

    def __init__(self, call, session_id: str):
        self.call = call
        self.session_id = session_id
        self._pages: list[dict] = []  # oldest first
        self._newer: list[dict] = []  # evicted pages, nearest last
        self.started = False
        self.bytes = 0
        self.last_added: list[dict] = []
        self.last_evicted: list[dict] = []
        self.newer_lost = False
        self.revision = 0

    @property
    def rows(self):
        return [row for page in self._pages for row in page["rows"]]

    @property
    def cursor(self):
        return self._pages[0]["after"] if self._pages else None

    @property
    def has_newer(self):
        return bool(self._newer)

    def _fetch(self, cursor):
        params = {"session_id": self.session_id, "order": "latest", "limit": PAGE_ROWS}
        if cursor is not None:
            params["cursor"] = cursor
        result = self.call("session.transcript", **params)
        if (not isinstance(result, dict) or result.get("session_id") != self.session_id
                or not isinstance(result.get("rows"), list)
                or any(not isinstance(row, dict) for row in result["rows"])
                or not isinstance(result.get("reset"), bool)
                or not isinstance(result.get("next_cursor"), (str, type(None)))):
            raise GatewayClientError("The linked history page was invalid")
        if cursor is not None and result["next_cursor"] == cursor:
            raise GatewayClientError("The linked history cursor did not advance")
        return result

    @staticmethod
    def _page(before, response):
        raw = json.dumps(response["rows"], ensure_ascii=False, sort_keys=True).encode("utf-8")
        return {"before": before, "after": response["next_cursor"], "rows": response["rows"],
                "size": len(raw), "digest": hashlib.sha256(raw).digest()}

    def load_initial(self):
        """Load the newest complete pages within the memory budget; retain the older cursor."""
        self._pages, self._newer, self.started, self.bytes = [], [], False, 0
        self.newer_lost = False
        cursor = None
        seen = set()
        for page_count in range(MAX_INITIAL_PAGES):
            if cursor in seen:
                raise GatewayClientError("Linked history returned a repeating cursor")
            seen.add(cursor)
            response = self._fetch(cursor)
            if cursor is not None and response["reset"]:
                raise GatewayClientError("Linked history changed while loading; try /history again")
            page = self._page(cursor, response)
            if self.started and self.bytes + page["size"] > INITIAL_BYTES:
                break  # Leave this page's cursor untouched so it can be fetched on demand.
            self._pages.insert(0, page)
            self.started = True
            self.bytes += page["size"]
            cursor = page["after"]
            if cursor is None or self.bytes >= INITIAL_BYTES or page_count + 1 == MAX_INITIAL_PAGES:
                break
            if cursor in seen:
                # The page just consumed points back at a cursor already walked: a cycle, not a
                # boundary. Detected HERE because the next iteration would exit on the page cap
                # before its own top-of-loop check.
                raise GatewayClientError("Linked history returned a repeating cursor")
        self.revision += 1
        return self.rows

    def load_older(self):
        """Prepend one page. A reset replaces the window, never splices different snapshots."""
        if not self.started:
            self.load_initial()
            return True
        if self.cursor is None:
            return False
        cursor = self.cursor
        response = self._fetch(cursor)
        if response["reset"]:
            raise GatewayClientError("Linked history changed; reopen /history to refresh it")
        page = self._page(cursor, response)
        boundaries = {item["before"] for item in self._pages + self._newer}
        if page["after"] is not None and page["after"] in boundaries:
            raise GatewayClientError("Linked history returned a repeating cursor")
        self._pages.insert(0, page)
        self.bytes += page["size"]
        self.last_added, self.last_evicted = page["rows"], []
        while self.bytes > INITIAL_BYTES and len(self._pages) > 1:
            removed = self._pages.pop()
            self.bytes -= removed["size"]
            self.last_evicted = removed["rows"] + self.last_evicted
            self._newer.append({key: value for key, value in removed.items() if key != "rows"})
            if len(self._newer) > MAX_PAGE_MARKERS:
                self._newer.pop(0)
                self.newer_lost = True
        self.revision += 1
        return True

    def load_newer(self):
        """Reload an evicted page only if its cursor and content still name the same snapshot."""
        if not self._newer:
            return False
        marker = self._newer[-1]
        response = self._fetch(marker["before"])
        page = self._page(marker["before"], response)
        if ((marker["before"] is not None and response["reset"]) or page["after"] != marker["after"]
                or page["digest"] != marker["digest"]):
            raise GatewayClientError("Linked history changed; reopen /history to refresh it")
        self._newer.pop()
        self._pages.append(page)
        self.bytes += page["size"]
        self.last_added, self.last_evicted = page["rows"], []
        while self.bytes > INITIAL_BYTES and len(self._pages) > 1:
            removed = self._pages.pop(0)
            self.bytes -= removed["size"]
            self.last_evicted.extend(removed["rows"])
        self.revision += 1
        return True


def conversation_rows(rows):
    """Map canonical renderer rows to the recap's display-only message shape."""
    return [{**row, "content": row.get("text", row.get("content"))} for row in rows]
