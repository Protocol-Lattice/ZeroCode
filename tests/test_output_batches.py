"""Continue token-limited generations without running incomplete tool calls."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.test_agent import EXE, MockAPI, StreamReply, environment, reply
from tests.test_scaling import assert_tool_pairs
from tests.test_streaming import chunk, claude_events, sse


def limited(provider, text="", calls=None):
    payload = reply(provider, text, calls)
    if provider == "claude":
        payload["stop_reason"] = "max_tokens"
    else:
        payload["choices"][0]["finish_reason"] = "length"
    return payload


def text_batch(provider, text):
    if provider == "claude":
        events = claude_events([({"type": "text", "text": ""},
                                [{"type": "text_delta", "text": text}])], "max_tokens")
    else:
        events = [chunk({"content": text}, "length"), "[DONE]"]
    return StreamReply([sse(event) for event in events])


class OutputBatchTests(unittest.TestCase):
    def run_agent(self, api, folder, provider="openrouter", extra=(), turns=10):
        return subprocess.run([str(EXE), "--cwd", folder, "--provider", provider,
                               "--no-learning", "--no-memory", "--no-skills", "--approve",
                               "--max-output-tokens", "1024", "--max-turns", str(turns),
                               "--prompt", "Original task: produce the complete requested output.", *extra],
                              env=environment(api.url), text=True, capture_output=True, timeout=30)

    def test_text_continues_across_json_and_streamed_responses_all_providers(self):
        for provider in ("openrouter", "openai", "claude", "gemini"):
            for streamed in (False, True):
                factory = text_batch if streamed else limited
                with self.subTest(provider=provider, streamed=streamed), tempfile.TemporaryDirectory() as folder:
                    with MockAPI([factory(provider, "FIRST_CHUNK żółw"), factory(provider, "SECOND_CHUNK"),
                                  reply(provider, "LAST_CHUNK")]) as api:
                        run = self.run_agent(api, folder, provider)
                        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                        self.assertEqual(len(api.requests), 3)
                        for label in ("FIRST_CHUNK", "SECOND_CHUNK", "LAST_CHUNK"):
                            self.assertEqual(run.stdout.count(label), 1, run.stdout)
                        key = "max_completion_tokens" if provider == "openai" else "max_tokens"
                        for _, body in api.requests:
                            self.assertEqual(body[key], 1024)
                            assert_tool_pairs(self, body["messages"], provider)
                        last = api.requests[-1][1]
                        system = last.get("system") or last["messages"][0]["content"]
                        self.assertIn("Original task: produce the complete requested output.", system)
                        self.assertIn("FIRST_CHUNK", json.dumps(last, ensure_ascii=False))
                        self.assertIn("SECOND_CHUNK", json.dumps(last))

    def test_truncated_tool_batch_executes_nothing_then_recovers_in_smaller_edits(self):
        for provider in ("openrouter", "claude"):
            for streamed in (False, True):
                with self.subTest(provider=provider, streamed=streamed), tempfile.TemporaryDirectory() as folder:
                    target = Path(folder, "large.txt")
                    target.write_text("Header\nold section\nTail\n")
                    if streamed and provider == "claude":
                        events = claude_events([
                            ({"type": "tool_use", "id": "partial", "name": "write_file", "input": {}},
                             [{"type": "input_json_delta", "partial_json": '{"path":"large.txt","content":"incomplete'}])],
                            "max_tokens")
                        first = StreamReply([sse(event) for event in events])
                    elif streamed:
                        events = [chunk({"tool_calls": [{"index": 0, "id": "partial", "type": "function",
                                   "function": {"name": "write_file", "arguments": '{"path":"large.txt","content":"incomplete'}}]}, "length"), "[DONE]"]
                        first = StreamReply([sse(event) for event in events])
                    else:
                        first = limited(provider, calls=[("write_file", {"path": "large.txt", "content": "incomplete"}),
                                                         ("write_file", {"path": "unsafe.txt", "content": "must not run"})])

                    def recover(body):
                        self.assertEqual(target.read_text(), "Header\nold section\nTail\n")
                        self.assertFalse(Path(folder, "unsafe.txt").exists())
                        self.assertNotIn('"id": "partial"', json.dumps(body))
                        assert_tool_pairs(self, body["messages"], provider)
                        return reply(provider, "", [("edit_file", {"path": "large.txt", "old_text": "old section", "new_text": "section one"})])

                    with MockAPI([first, recover,
                                  reply(provider, "", [("edit_file", {"path": "large.txt", "old_text": "Tail", "new_text": "section two\nTail"})]),
                                  reply(provider, "All chunks complete.")]) as api:
                        run = self.run_agent(api, folder, provider)
                        self.assertEqual(run.returncode, 0, run.stdout + run.stderr + str(api.errors))
                        self.assertEqual(target.read_text(), "Header\nsection one\nsection two\nTail\n")
                        self.assertEqual(len(api.requests), 4)
                        self.assertFalse(api.errors, api.errors)

    def test_truncated_tool_retries_are_bounded(self):
        with tempfile.TemporaryDirectory() as folder, MockAPI([
            limited("openrouter", calls=[("write_file", {"path": "partial.txt", "content": "partial"})])]) as api:
            run = self.run_agent(api, folder)
            self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual(len(api.requests), 3)
            self.assertFalse(Path(folder, "partial.txt").exists())
            self.assertIn("smaller", run.stdout)

    def test_text_continuations_share_the_request_budget(self):
        with tempfile.TemporaryDirectory() as folder, MockAPI([limited("openrouter", "More output.")]) as api:
            run = self.run_agent(api, folder, turns=2)
            self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual(len(api.requests), 2)
            self.assertIn("request limit", run.stdout)

    def test_chat_only_continues_without_advertising_tools(self):
        with tempfile.TemporaryDirectory() as folder, MockAPI([
            limited("openrouter", "Part one."), reply("openrouter", "Part two.")]) as api:
            run = self.run_agent(api, folder, extra=("--chat-only",))
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertTrue(all("tools" not in body for _, body in api.requests))

    def test_output_budget_is_inherited_by_subagents(self):
        def route(body):
            system = body["messages"][0]["content"]
            self.assertEqual(body["max_tokens"], 1024)
            if "You are a delegated file subagent" in system:
                return reply("openrouter", "Reviewed the assigned file.")
            if not any(message["role"] == "tool" for message in body["messages"]):
                return reply("openrouter", "", [("delegate_tasks", {"tasks": [
                    {"mode": "read", "paths": ["note.txt"], "prompt": "Review note.txt."}]})])
            return reply("openrouter", "Delegation complete.")
        with tempfile.TemporaryDirectory() as folder, MockAPI([route]) as api:
            Path(folder, "note.txt").write_text("note")
            run = self.run_agent(api, folder)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr + str(api.errors))
            self.assertEqual(len(api.requests), 3)
            self.assertFalse(api.errors)

    def test_invalid_output_budgets_fail_locally(self):
        with tempfile.TemporaryDirectory() as folder:
            for values in ((), ("--snapshot",), ("0",), ("abc",), ("-1",), ("65537",)):
                with self.subTest(values=values):
                    run = subprocess.run([str(EXE), "--cwd", folder, "--max-output-tokens", *values],
                                         env=environment(keys=False), text=True, capture_output=True, timeout=5)
                    self.assertEqual(run.returncode, 1)
                    self.assertIn("Invalid options", run.stdout)
                    self.assertIn("--max-output-tokens requires an integer from 256 to 65536", run.stdout)
                    self.assertIn("zero-code --max-output-tokens 4096", run.stdout)


if __name__ == "__main__":
    unittest.main()
