"""Anthropic Messages API <-> OpenAI Chat Completions bridge.

Lets Claude Code (and anything else that speaks ``/v1/messages``) talk to the
Loomy upstream, including tool calls and streaming.  Reasoning output
(``reasoning_content``) is surfaced as Anthropic ``thinking`` blocks so nothing
is silently dropped.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Iterable, List, Optional

__all__ = ["anthropic_to_openai", "openai_to_anthropic", "StreamTranslator",
           "estimate_tokens"]


def _blocks_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif block.get("type") == "tool_result":
                    parts.append(_blocks_to_text(block.get("content")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return "" if content is None else str(content)


def _tool_result_text(block: Dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    return _blocks_to_text(content)


def anthropic_to_openai(req: Dict[str, Any]) -> Dict[str, Any]:
    """Translate an Anthropic ``/v1/messages`` request into Chat Completions."""
    messages: List[Dict[str, Any]] = []

    system = req.get("system")
    if system:
        messages.append({"role": "system", "content": _blocks_to_text(system)})

    for message in req.get("messages") or []:
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        texts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        tool_messages: List[Dict[str, Any]] = []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                texts.append(str(block.get("text") or ""))
            elif kind == "tool_use":
                tool_calls.append({
                    "id": block.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input") or {},
                                                ensure_ascii=False),
                    },
                })
            elif kind == "tool_result":
                tool_messages.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id") or "",
                    "content": _tool_result_text(block),
                })

        # tool_result blocks arrive inside a *user* message; they become
        # standalone "tool" messages, and no empty user scaffold is emitted.
        if texts or tool_calls:
            out_message: Dict[str, Any] = {
                "role": role,
                "content": "\n".join(t for t in texts if t) or None,
            }
            if tool_calls:
                out_message["tool_calls"] = tool_calls
            messages.append(out_message)
        messages.extend(tool_messages)

    out: Dict[str, Any] = {
        "model": req.get("model"),
        "messages": messages,
        "stream": bool(req.get("stream")),
    }
    for src, dst in (("max_tokens", "max_tokens"), ("temperature", "temperature"),
                     ("top_p", "top_p")):
        if req.get(src) is not None:
            out[dst] = req[src]
    if req.get("stop_sequences"):
        out["stop"] = req["stop_sequences"]

    tools = req.get("tools") or []
    if tools:
        out["tools"] = [{
            "type": "function",
            "function": {
                "name": t.get("name"),
                "description": t.get("description") or "",
                "parameters": t.get("input_schema") or {"type": "object",
                                                        "properties": {}},
            },
        } for t in tools if t.get("name")]

    choice = req.get("tool_choice")
    if isinstance(choice, dict):
        if choice.get("type") == "auto":
            out["tool_choice"] = "auto"
        elif choice.get("type") == "any":
            out["tool_choice"] = "required"
        elif choice.get("type") == "tool" and choice.get("name"):
            out["tool_choice"] = {"type": "function",
                                 "function": {"name": choice["name"]}}
    return out


def openai_to_anthropic(obj: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Translate a non-streaming Chat Completion into an Anthropic message."""
    choice = (obj.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content: List[Dict[str, Any]] = []

    reasoning = message.get("reasoning_content")
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning, "signature": ""})
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:                               # noqa: BLE001
            args = {}
        content.append({
            "type": "tool_use",
            "id": call.get("id") or f"call_{uuid.uuid4().hex[:12]}",
            "name": fn.get("name"),
            "input": args,
        })

    finish = choice.get("finish_reason")
    stop_reason = {"length": "max_tokens", "tool_calls": "tool_use"}.get(finish, "end_turn")
    usage = obj.get("usage") or {}
    return {
        "id": obj.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("completion_tokens") or 0,
        },
    }


def estimate_tokens(text: str) -> int:
    """Rough token estimate for the streaming ``message_start`` usage field."""
    return max(1, len(text) // 4)


class StreamTranslator:
    """Converts OpenAI SSE chunks into Anthropic SSE events.

    Usage::

        st = StreamTranslator(model)
        for event, data in st.start(): ...      # emit
        for chunk in openai_chunks:
            for event, data in st.feed(chunk): ...
        for event, data in st.finish(): ...     # emit
    """

    def __init__(self, model: str):
        self.model = model
        self.message_id = f"msg_{uuid.uuid4().hex[:24]}"
        self.usage: Dict[str, Any] = {}
        self.finish_reason: Optional[str] = None
        self._text_open = False
        self._text_index = 0
        self._think_open = False

    # -- helpers --------------------------------------------------------

    def _ev(self, event: str, data: Dict[str, Any]):
        return event, data

    def start(self) -> Iterable:
        yield self._ev("message_start", {
            "type": "message_start",
            "message": {
                "id": self.message_id,
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        })

    def feed(self, obj: Dict[str, Any]) -> Iterable:
        usage = obj.get("usage")
        if isinstance(usage, dict):
            self.usage.update(usage)

        choices = obj.get("choices") or []
        if not choices:
            return
        choice = choices[0]
        delta = choice.get("delta") or {}

        thinking = delta.get("reasoning_content")
        if thinking:
            if not self._think_open:
                self._think_open = True
                yield self._ev("content_block_start", {
                    "type": "content_block_start", "index": 0,
                    "content_block": {"type": "thinking", "thinking": ""}})
            yield self._ev("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "thinking_delta", "thinking": thinking}})

        text = delta.get("content")
        if text:
            if self._think_open and not self._text_open:
                self._think_open = False
                yield self._ev("content_block_stop", {
                    "type": "content_block_stop", "index": 0})
            if not self._text_open:
                self._text_open = True
                self._text_index = 0
                yield self._ev("content_block_start", {
                    "type": "content_block_start", "index": self._text_index,
                    "content_block": {"type": "text", "text": ""}})
            yield self._ev("content_block_delta", {
                "type": "content_block_delta", "index": self._text_index,
                "delta": {"type": "text_delta", "text": text}})

        # tool calls are streamed as complete blocks (upstream sends them whole)
        for call in delta.get("tool_calls") or []:
            fn = call.get("function") or {}
            if not fn.get("name"):
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:                           # noqa: BLE001
                args = {}
            index = self._text_index + 1 if self._text_open else 0
            yield self._ev("content_block_start", {
                "type": "content_block_start", "index": index,
                "content_block": {"type": "tool_use",
                                  "id": call.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                                  "name": fn["name"], "input": {}}})
            yield self._ev("content_block_delta", {
                "type": "content_block_delta", "index": index,
                "delta": {"type": "input_json_delta",
                          "partial_json": json.dumps(args, ensure_ascii=False)}})
            yield self._ev("content_block_stop", {
                "type": "content_block_stop", "index": index})

        self.finish_reason = choice.get("finish_reason") or self.finish_reason

    def finish(self) -> Iterable:
        if self._think_open:
            self._think_open = False
            yield self._ev("content_block_stop", {"type": "content_block_stop", "index": 0})
        if self._text_open:
            self._text_open = False
            yield self._ev("content_block_stop", {
                "type": "content_block_stop", "index": self._text_index})
        stop_reason = {"length": "max_tokens", "tool_calls": "tool_use"}.get(
            self.finish_reason, "end_turn")
        yield self._ev("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": self.usage.get("completion_tokens") or 0},
        })
        yield self._ev("message_stop", {"type": "message_stop"})
