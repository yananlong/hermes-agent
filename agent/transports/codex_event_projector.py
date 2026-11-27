"""Projects codex app-server ``item/*`` notifications into OpenAI-shaped messages.

userMessage → user; agentMessage → assistant; reasoning → stashed onto the next
assistant entry or flushed at the turn boundary; commandExecution / fileChange /
mcpToolCall / dynamicToolCall / webSearch → assistant tool_call + tool result;
anything else → opaque assistant note.
Each item yields AT MOST one assistant + one tool entry (message-alternation
invariant). ``is_tool_iteration`` ticks once per completed tool-shaped item.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from uuid import UUID


def _deterministic_call_id(item_type: str, item_id: str) -> str:
    """Stable tool_call id: the codex item id, else a hash so replay keeps prefix caches valid."""
    return f"codex_{item_type}_{item_id or hashlib.sha256(f'{item_type}'.encode()).hexdigest()[:16]}"


_MAX_PROJECTED_TOOL_TEXT = 64 * 1024
_MAX_REASONING_DISPLAY_CHARS = 256 * 1024


def _redact(text: str, *, force: bool = True) -> str:
    """Redact credential-like text before native tool data reaches display history."""
    try:
        from agent.redact import redact_sensitive_text
        return redact_sensitive_text(text, force=force)
    except Exception:  # health: allow BLE001 -- redaction failures must fail closed before display
        # A redaction failure must not turn this display path into a secret leak.
        return "[content hidden because redaction failed]"


def _bounded_tool_text(text: str) -> str:
    text = _redact(text)
    if len(text) <= _MAX_PROJECTED_TOOL_TEXT:
        return text
    suffix = "\n[truncated by Hermes display limit]"
    return text[:_MAX_PROJECTED_TOOL_TEXT - len(suffix)] + suffix


def _bounded_reasoning_text(text: str, *, force_redaction: bool = True) -> str:
    """Cap Codex reasoning after applying the redaction policy for its destination."""
    text = _redact(text, force=force_redaction)
    if len(text) <= _MAX_REASONING_DISPLAY_CHARS:
        return text
    suffix = "\n[truncated by Hermes reasoning display limit]"
    return text[:_MAX_REASONING_DISPLAY_CHARS - len(suffix)] + suffix


def _format_tool_args(d: dict) -> str:
    """Format a dict as JSON the way Hermes' existing tool_calls path does."""
    encoded = _redact(json.dumps(d, ensure_ascii=False, sort_keys=True, default=str))
    if len(encoded) <= _MAX_PROJECTED_TOOL_TEXT:
        return encoded
    preview = encoded[:_MAX_PROJECTED_TOOL_TEXT - 128]
    return json.dumps({"_truncated": True, "preview": preview}, ensure_ascii=False)


def _safe_tool_args(raw: Any) -> dict:
    """Return redacted JSON-compatible args for display callbacks as well as stored projections."""
    args = _dict_args(raw)
    try:
        value = json.loads(_format_tool_args(args))
    except (TypeError, ValueError):
        return {"_truncated": True, "preview": "[tool arguments unavailable]"}
    return value if isinstance(value, dict) else {"arguments": value}


_PRIVATE_ITEM_KEYS = {"threadid", "turnid", "senderthreadid", "receiverthreadids", "agentthreadid"}


def _opaque_display_value(value: Any) -> Any:
    """Drop encrypted carriers and private routing identifiers from unknown item previews."""
    if isinstance(value, dict):
        return {
            key: _opaque_display_value(child)
            for key, child in value.items()
            if isinstance(key, str) and "encrypted" not in key.lower() and key.lower() not in _PRIVATE_ITEM_KEYS
        }
    if isinstance(value, list):
        return [_opaque_display_value(child) for child in value]
    return value


def _dict_args(raw: Any) -> dict:
    args = raw or {}
    return args if isinstance(args, dict) else {"arguments": args}


def _safe_agent_path(raw: Any) -> str | None:
    """Keep role paths useful for display without exposing local definition paths."""
    if not isinstance(raw, str) or not raw or len(raw) > 512:
        return None
    path = raw.strip()
    if (not path or path.startswith(("/", "\\", "~")) or re.match(r"^[a-zA-Z]:", path)
            or "://" in path or "\\" in path):
        return None
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return path


def _canonical_thread_id(raw: Any) -> str | None:
    """Only retain canonical native thread IDs that the read-only trace RPC accepts."""
    if not isinstance(raw, str):
        return None
    try:
        value = str(UUID(raw))
    except ValueError:
        return None
    return value if raw == value else None


def _file_change_changes(item: dict) -> list[dict[str, Any]]:
    """Normalize app-server file changes, retaining bounded-display input such as unified diffs."""
    raw_changes = item.get("changes")
    if not isinstance(raw_changes, list):
        return []
    changes = []
    for change in raw_changes:
        if not isinstance(change, dict):
            continue
        kind = change.get("kind")
        kind = kind if isinstance(kind, dict) else {}
        operation = kind.get("type")
        normalized = {
            "kind": operation if isinstance(operation, str) and operation else "update",
            "path": change.get("path") if isinstance(change.get("path"), str) else "",
        }
        move_path = kind.get("move_path")
        if isinstance(move_path, str) and move_path:
            normalized["move_path"] = move_path
        diff = change.get("diff")
        if isinstance(diff, str) and diff:
            normalized["diff"] = diff
        changes.append(normalized)
    return changes


def _file_change_result_text(item: dict) -> str:
    """Build the same safe, bounded file-change result for live cards and stored history."""
    changes = _file_change_changes(item)
    status = item.get("status") or "unknown"
    summary = f"apply_patch status={status}, {len(changes)} change(s)"
    diff_sections = []
    for change in changes:
        diff = change.get("diff")
        if not diff:
            continue
        kind, path = change["kind"], change["path"]
        move_path = change.get("move_path")
        header = f"{kind}: {path}" + (f" -> {move_path}" if move_path else "")
        diff_sections.append(f"{header}\n{diff}")
    if diff_sections:
        summary += "\n\n" + "\n\n".join(diff_sections)
    return _bounded_tool_text(summary)


@dataclass
class ProjectionResult:
    """Output of projecting one Codex item; empty ``messages`` = ignored (e.g. a streaming delta)."""

    messages: list[dict] = field(default_factory=list)
    is_tool_iteration: bool = False
    final_text: Optional[str] = None  # Set when an agentMessage completes


class CodexEventProjector:
    """Stateful projector; stashes codex's separate reasoning items onto the next assistant message."""

    def __init__(self, *, force_reasoning_redaction: bool = False) -> None:
        self._pending_reasoning: list[str] = []
        self._force_reasoning_redaction = force_reasoning_redaction
        self._native_thread_id: str | None = None
        self._native_turn_id: str | None = None

    def _reasoning(self, text: str) -> str:
        return _bounded_reasoning_text(text, force_redaction=self._force_reasoning_redaction)

    def project(self, notification: dict) -> ProjectionResult:
        """Project completed items; a turn boundary also flushes otherwise-unpaired readable reasoning."""
        method = notification.get("method", "")
        params = notification.get("params", {}) or {}
        self._remember_native_scope(params)
        if method == "turn/completed":
            return self._stamp_native_rows(self._flush_pending_reasoning(), "turn/completed")
        if method != "item/completed":
            return ProjectionResult()
        item = params.get("item") or {}
        item_type = item.get("type") or ""
        item_id = item.get("id") or ""
        if item_type == "agentMessage":
            return self._stamp_native_rows(self._project_agent_message(item), item_id)
        if item_type == "reasoning":
            # Only plain text the app-server explicitly exposes is displayable. In particular,
            # never inspect or decode encrypted reasoning fields that newer servers may add.
            for key in ("summary", "content"):
                values = item.get(key)
                if isinstance(values, list):
                    text = "\n".join(value for value in values if isinstance(value, str) and value)
                    if text:
                        self._pending_reasoning.append(self._reasoning(text))
            if self._pending_reasoning:
                self._pending_reasoning = [self._reasoning("\n".join(self._pending_reasoning))]
            return ProjectionResult()
        if item_type == "userMessage":
            return self._stamp_native_rows(self._project_user_message(item), item_id)
        tool_projection = self._TOOL_PROJECTIONS.get(item_type)
        if tool_projection is not None:
            return self._stamp_native_rows(self._project_tool_item(item, item_id, tool_projection), item_id)
        # Unknown / rare items (plan, hookPrompt, ...): opaque note, no fabricated tool_call structure.
        return self._stamp_native_rows(self._project_opaque(item, item_type), item_id)

    def _remember_native_scope(self, params: dict) -> None:
        thread_id = params.get("threadId")
        if isinstance(thread_id, str) and thread_id:
            self._native_thread_id = thread_id
        turn_id = params.get("turnId")
        if not isinstance(turn_id, str) or not turn_id:
            turn = params.get("turn")
            turn_id = turn.get("id") if isinstance(turn, dict) else None
        if isinstance(turn_id, str) and turn_id:
            self._native_turn_id = turn_id

    def _stamp_native_rows(self, result: ProjectionResult, item_id: str) -> ProjectionResult:
        if not self._native_thread_id or not self._native_turn_id:
            return result
        identity = item_id if item_id else "turn-completed"
        for index, row in enumerate(result.messages):
            digest = hashlib.sha256(json.dumps(
                [self._native_thread_id, self._native_turn_id, identity, index],
                ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            row["codex_native_row_id"] = f"codex:{digest}"
        return result

    def _assistant_message(self, content: Optional[str], **extra: Any) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": content, **extra}
        if msg.get("codex_native") is True:
            self._mark_codex_native(msg)
        if self._pending_reasoning:
            msg["reasoning"] = self._reasoning("\n".join(self._pending_reasoning))
            self._pending_reasoning = []
        return msg

    @staticmethod
    def _mark_codex_native(message: dict[str, Any]) -> None:
        """Retain a native-row marker through the ordinary message DB metadata field."""
        metadata = message.get("display_metadata")
        message["display_metadata"] = {
            **(metadata if isinstance(metadata, dict) else {}),
            "codex_native": True,
        }

    def _flush_pending_reasoning(self) -> ProjectionResult:
        """Materialize readable reasoning left without a following item at this turn boundary."""
        if not self._pending_reasoning:
            return ProjectionResult()
        return ProjectionResult(messages=[self._assistant_message(None)])

    def _project_agent_message(self, item: dict) -> ProjectionResult:
        text = item.get("text") or ""
        return ProjectionResult(messages=[self._assistant_message(text)], final_text=text)

    @staticmethod
    def _project_user_message(item: dict) -> ProjectionResult:
        # userMessage content is a list of UserInput variants; flatten text
        # fragments and drop non-text parts (Hermes' messages store text only).
        text_parts = [
            (fragment.get("text") or "") if fragment.get("type") == "text" else str(fragment["text"])
            for fragment in item.get("content") or []
            if isinstance(fragment, dict) and (fragment.get("type") == "text" or "text" in fragment)
        ]
        return ProjectionResult(messages=[{"role": "user", "content": "\n".join(text_parts)}])

    def _project_tool_item(
        self, item: dict, item_id: str, spec: Callable[[dict], tuple[str, str, dict, str]]
    ) -> ProjectionResult:
        """Emit the (assistant tool_call, tool result) pair; ``spec(item)`` -> ``(id_type, name, args, content)``."""
        id_type, name, args, content = spec(item)
        call_id = _deterministic_call_id(id_type, item_id)
        assistant_msg = self._assistant_message(
            None,
            tool_calls=[{"id": call_id, "type": "function", "function": {"name": name, "arguments": _format_tool_args(args)}}],
            codex_native=True,
            display_metadata={"codex_native": True},
        )
        tool_msg = {"role": "tool", "tool_call_id": call_id, "tool_name": name,
                    "codex_native": True, "content": _bounded_tool_text(content)}
        self._mark_codex_native(tool_msg)
        return ProjectionResult(messages=[assistant_msg, tool_msg], is_tool_iteration=True)

    @staticmethod
    def _command_spec(item: dict) -> tuple[str, str, dict, str]:
        args = {"command": item.get("command") or "", "cwd": item.get("cwd") or ""}
        output = item.get("aggregatedOutput") or ""
        exit_code = item.get("exitCode")
        if exit_code is not None:
            # Preserve executor status so successful marker-like output is not an interrupt.
            output = json.dumps({"exit_code": exit_code, "output": output}, ensure_ascii=False)
        # Unknown exit status keeps the legacy text shape and conservative replay handling.
        return "exec", "exec_command", args, output

    @staticmethod
    def _file_change_spec(item: dict) -> tuple[str, str, dict, str]:
        # App-server diffs are useful review context, so retain them in the native call/result
        # while applying the same redaction and display cap used for live completion cards.
        changes = _file_change_changes(item)
        return "apply_patch", "apply_patch", {"changes": changes}, _file_change_result_text(item)

    @staticmethod
    def _mcp_tool_call_spec(item: dict) -> tuple[str, str, dict, str]:
        server = item.get("server") or "mcp"
        tool = item.get("tool") or "unknown"
        result, error = item.get("result"), item.get("error")
        if error:
            content = f"[error] {json.dumps(error, ensure_ascii=False, default=str)[:1000]}"
        else:
            content = json.dumps(result, ensure_ascii=False, default=str) if result is not None else ""
        # Call id mirrors the native MCP name convention (mcp__server__tool).
        return f"mcp__{server}__{tool}", f"mcp.{server}.{tool}", _dict_args(item.get("arguments")), content

    @staticmethod
    def _dynamic_tool_call_spec(item: dict) -> tuple[str, str, dict, str]:
        tool = item.get("tool") or "unknown"
        content_items = item.get("contentItems") or []
        content = (
            json.dumps(content_items, ensure_ascii=False, default=str) if isinstance(content_items, list) and content_items
            else f"success={item.get('success')}"
        )
        return f"dyn_{tool}", tool, _safe_tool_args(item.get("arguments")), content

    @staticmethod
    def _collab_agent_tool_call_spec(item: dict) -> tuple[str, str, dict, str]:
        """Expose only fields Codex records for collaboration items; receiver IDs stay private."""
        tool = item.get("tool") or "unknown"
        name = f"collab.{tool}"
        args: dict[str, Any] = {}
        prompt = item.get("prompt")
        if isinstance(prompt, str) and prompt:
            args["prompt"] = prompt

        agents = []
        states = item.get("agentsStates")
        if isinstance(states, dict):
            for state in states.values():
                if not isinstance(state, dict):
                    continue
                agent_state: dict[str, str] = {}
                for key in ("status", "message"):
                    value = state.get(key)
                    if isinstance(value, str) and value:
                        agent_state[key] = value
                if agent_state:
                    agents.append(agent_state)
        result: dict[str, Any] = {"status": item.get("status") or "unknown"}
        if agents:
            result["agents"] = agents
        if not args.get("prompt") and not any(agent.get("message") for agent in agents):
            result["details"] = "Codex did not provide prompt or subagent result details for this collaboration step."
        return name, name, args, json.dumps(result, ensure_ascii=False)

    @staticmethod
    def _subagent_activity_spec(item: dict) -> tuple[str, str, dict, str]:
        """Keep Codex's native child-thread pointer on a bounded, parent-authorized activity card."""
        args: dict[str, Any] = {}
        kind = item.get("kind")
        if isinstance(kind, str) and kind and len(kind) <= 64:
            args["kind"] = kind
        agent_path = _safe_agent_path(item.get("agentPath", item.get("agent_path")))
        if agent_path:
            args["agent_path"] = agent_path
        model = item.get("model")
        # These are labels, not trace payloads; keep them small so the internal trace pointer stays
        # intact even if Codex returns unexpected metadata.
        if isinstance(model, str) and model and len(model) <= 256:
            args["model"] = model
        reasoning_effort = item.get("reasoningEffort", item.get("reasoning_effort"))
        if isinstance(reasoning_effort, str) and reasoning_effort and len(reasoning_effort) <= 128:
            args["reasoning_effort"] = reasoning_effort
        agent_thread_id = _canonical_thread_id(item.get("agentThreadId", item.get("agent_thread_id")))
        if agent_thread_id:
            args["agent_thread_id"] = agent_thread_id
        # Codex exposes activity without a completion body. An empty result preserves the
        # assistant/tool row pairing used by message hydration without inventing a result.
        return "subagent_activity", "codex_subagent_activity", _safe_tool_args(args), ""

    @staticmethod
    def _web_search_spec(item: dict) -> tuple[str, str, dict, str]:
        # Codex ran the search itself; the result names it so a later non-Codex
        # turn does not read this as a Hermes web_search call.
        result = {"provider": "codex"}
        if item.get("status"):
            result["status"] = item["status"]
        return "web_search", "web_search", {"query": item.get("query") or ""}, json.dumps(result, ensure_ascii=False)

    _TOOL_PROJECTIONS: dict[str, Callable[[dict], tuple[str, str, dict, str]]] = {
        "commandExecution": _command_spec,
        "fileChange": _file_change_spec,
        "mcpToolCall": _mcp_tool_call_spec,
        "dynamicToolCall": _dynamic_tool_call_spec,
        "collabAgentToolCall": _collab_agent_tool_call_spec,
        "subAgentActivity": _subagent_activity_spec,
        "webSearch": _web_search_spec,
    }

    def _project_opaque(self, item: dict, item_type: str) -> ProjectionResult:
        try:
            payload = _bounded_tool_text(json.dumps(_opaque_display_value(item), ensure_ascii=False, default=str))[:1500]
        except (TypeError, ValueError):
            payload = _bounded_tool_text(repr(_opaque_display_value(item)))[:1500]
        return ProjectionResult(messages=[self._assistant_message(f"[codex {item_type}] {payload}")])
