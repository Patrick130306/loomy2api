"""Anthropic <-> OpenAI protocol translation tests."""

from __future__ import annotations

import json
import unittest

from loomy2api import anthropic as anth


class TestRequestTranslation(unittest.TestCase):
    def test_system_and_blocks(self):
        out = anth.anthropic_to_openai({
            "model": "m", "max_tokens": 16, "temperature": 0.2,
            "system": [{"type": "text", "text": "be brief"}],
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "hi"}]}],
        })
        self.assertEqual(out["messages"][0], {"role": "system", "content": "be brief"})
        self.assertEqual(out["messages"][1], {"role": "user", "content": "hi"})
        self.assertEqual(out["max_tokens"], 16)
        self.assertEqual(out["temperature"], 0.2)

    def test_tools_and_tool_results(self):
        out = anth.anthropic_to_openai({
            "model": "m",
            "messages": [
                {"role": "assistant", "content": [
                    {"type": "text", "text": "calling"},
                    {"type": "tool_use", "id": "tu1", "name": "get_weather",
                     "input": {"city": "hz"}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu1",
                     "content": [{"type": "text", "text": "sunny"}]}]},
            ],
            "tools": [{"name": "get_weather", "description": "w",
                       "input_schema": {"type": "object"}}],
            "tool_choice": {"type": "tool", "name": "get_weather"},
        })
        assistant = out["messages"][0]
        self.assertEqual(assistant["tool_calls"][0]["id"], "tu1")
        self.assertEqual(json.loads(assistant["tool_calls"][0]["function"]["arguments"]),
                         {"city": "hz"})
        self.assertEqual(out["messages"][1], {"role": "tool", "tool_call_id": "tu1",
                                              "content": "sunny"})
        self.assertEqual(out["tools"][0]["function"]["parameters"], {"type": "object"})
        self.assertEqual(out["tool_choice"],
                         {"type": "function", "function": {"name": "get_weather"}})

    def test_stop_sequences(self):
        out = anth.anthropic_to_openai({"model": "m", "messages": [],
                                        "stop_sequences": ["\n\n"]})
        self.assertEqual(out["stop"], ["\n\n"])


class TestResponseTranslation(unittest.TestCase):
    def test_text_reasoning_and_tool_calls(self):
        got = anth.openai_to_anthropic({
            "id": "x",
            "choices": [{"message": {
                "reasoning_content": "because",
                "content": "done",
                "tool_calls": [{"id": "c1", "function": {
                    "name": "f", "arguments": '{"a":1}'}}]},
                "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        }, "m")
        kinds = [b["type"] for b in got["content"]]
        self.assertEqual(kinds, ["thinking", "text", "tool_use"])
        self.assertEqual(got["stop_reason"], "tool_use")
        self.assertEqual(got["usage"], {"input_tokens": 7, "output_tokens": 3})

    def test_length_finish_reason(self):
        got = anth.openai_to_anthropic(
            {"choices": [{"message": {"content": "x"}, "finish_reason": "length"}]}, "m")
        self.assertEqual(got["stop_reason"], "max_tokens")


class TestStreamTranslator(unittest.TestCase):
    def _run(self, chunks):
        translator = anth.StreamTranslator("m")
        events = list(translator.start())
        for chunk in chunks:
            events.extend(translator.feed(chunk))
        events.extend(translator.finish())
        return events

    def test_stream_with_reasoning(self):
        events = self._run([
            {"choices": [{"delta": {"reasoning_content": "hmm"}}]},
            {"choices": [{"delta": {"content": "hi"}}]},
            {"choices": [], "usage": {"completion_tokens": 9, "points_consumed": 2}},
        ])
        names = [e for e, _ in events]
        self.assertEqual(names[0], "message_start")
        self.assertEqual(names[-1], "message_stop")
        indexes = {d["index"] for e, d in events if e == "content_block_delta"}
        self.assertEqual(indexes, {0})                     # thinking and text share idx 0
        kinds = [d["content_block"]["type"] for e, d in events
                 if e == "content_block_start"]
        self.assertEqual(kinds, ["thinking", "text"])
        message_delta = [d for e, d in events if e == "message_delta"][0]
        self.assertEqual(message_delta["usage"]["output_tokens"], 9)
        self.assertEqual(message_delta["delta"]["stop_reason"], "end_turn")

    def test_stream_text_only(self):
        events = self._run([{"choices": [{"delta": {"content": "a"},
                                          "finish_reason": "length"}]}])
        starts = [d["content_block"]["type"] for e, d in events
                  if e == "content_block_start"]
        self.assertEqual(starts, ["text"])
        self.assertEqual([d for e, d in events if e == "message_delta"][0]
                         ["delta"]["stop_reason"], "max_tokens")

    def test_stream_tool_call(self):
        events = self._run([{"choices": [{"delta": {"tool_calls": [
            {"id": "c1", "function": {"name": "f", "arguments": '{"b":2}'}}]},
            "finish_reason": "tool_calls"}]}])
        starts = [d["content_block"]["type"] for e, d in events
                  if e == "content_block_start"]
        self.assertEqual(starts, ["tool_use"])
        partial = [d for e, d in events if d.get("delta", {}).get("type")
                   == "input_json_delta"][0]
        self.assertEqual(json.loads(partial["delta"]["partial_json"]), {"b": 2})


if __name__ == "__main__":
    unittest.main()
