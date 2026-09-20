"""Real HTTP chunk boundaries, tool deltas, cancellation and stream failures."""
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import threading
import time
import unittest

from tests import test_agent as agent
from tests.test_agent import MockAPI, StreamReply, Terminal, environment, reply


def sse(data):
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return ("data: " + text + "\r\n\r\n").encode()


def fragmented(events, width=13):
    wire = b": keepalive\r\n\r\n" + b"".join(sse(event) for event in events)
    return [wire[i:i + width] for i in range(0, len(wire), width)]


def chunk(delta=None, finish=None, usage=None):
    result = {"choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage is not None:
        result["usage"] = usage
    return result


def claude_events(blocks, stop="end_turn"):
    yield {"type": "message_start", "message": {"role": "assistant", "content": [],
           "usage": {"input_tokens": 12, "output_tokens": 0}}}
    for index, (block, deltas) in enumerate(blocks):
        yield {"type": "content_block_start", "index": index, "content_block": block}
        for delta in deltas:
            yield {"type": "content_block_delta", "index": index, "delta": delta}
        yield {"type": "content_block_stop", "index": index}
    yield {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 8}}
    yield {"type": "message_stop"}


class StreamingTests(unittest.TestCase):
    run_agent = agent.AgentTests.run_agent

    def test_interleaved_arguments_with_mixed_metadata_and_empty_fragments(self):
        contents = [('First "żółw" \\ path\n' * 1400), ('Second 🐢\tline\n' * 1300)]
        args = [json.dumps({"path": f"file{i}.txt", "content": text}, ensure_ascii=False)
                for i, text in enumerate(contents)]
        pieces = [[text[i:i + 37] for i in range(0, len(text), 37)] for text in args]
        for provider in ("openrouter", "openai", "gemini"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                events = [chunk({"role": "assistant", "reasoning_details": [
                    {"index": 0, "type": "reasoning.text", "text": pieces[0][0]}],
                    "tool_calls": [{"index": i, "id": f"write_{i}", "type": "function",
                                    "function": {"name": "write_", "arguments": ""}} for i in (1, 0)]})]
                events.append(chunk({"tool_calls": [{"index": 0, "function": {"name": "file"}}]}))
                for part in range(max(map(len, pieces))):
                    calls = []
                    for i in (1, 0):
                        if part < len(pieces[i]):
                            function = {"arguments": pieces[i][part]}
                            if part == 0 and i == 1:
                                function["name"] = "file"
                            calls.append({"index": i, "function": function})
                    delta = {"tool_calls": calls}
                    if part == 10:
                        delta["tool_calls"][0]["extra_content"] = {"google": {"thought_signature": "preserved"}}
                    events.append(chunk(delta))
                    if part % 100 == 0:
                        events.append(chunk({"tool_calls": [{"index": 0, "function": {"arguments": ""}}]}))
                        events.append(chunk({"tool_calls": [{"index": 1, "function": {"arguments": None}}]}))
                events.extend([chunk(finish="tool_calls"), "[DONE]"])
                with MockAPI([StreamReply([sse(event) for event in events]), reply(provider, "Saved.")]) as api:
                    result = self.run_agent(api, provider, folder, extra=("--approve",), timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    for i, content in enumerate(contents):
                        self.assertEqual(Path(folder, f"file{i}.txt").read_text(), content)
                    message = next(m for m in api.requests[1][1]["messages"] if m["role"] == "assistant")
                    self.assertEqual([call["id"] for call in message["tool_calls"]], ["write_0", "write_1"])
                    self.assertEqual(message["reasoning_details"][0]["text"], pieces[0][0])
                    self.assertEqual(message["tool_calls"][1]["extra_content"]["google"]["thought_signature"], "preserved")

    def test_cancelled_tool_arguments_do_not_leak_into_next_stream(self):
        release = threading.Event()

        def pending():
            yield sse(chunk({"content": "Arguments buffered", "tool_calls": [{"index": 0,
                "id": "cancelled", "type": "function", "function": {"name": "write_file",
                "arguments": '{"path":"cancelled.txt","content":"' + "old data" * 700}}]}))
            release.wait(8)

        args = json.dumps({"path": "fresh.txt", "content": "Only the new stream."})
        events = [chunk({"tool_calls": [{"index": 0, "id": "fresh", "type": "function",
            "function": {"name": "write_file", "arguments": args[:20]}}]}),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": args[20:]}}]}, "tool_calls"), "[DONE]"]
        with tempfile.TemporaryDirectory() as folder, MockAPI([
                StreamReply(pending()), StreamReply([sse(event) for event in events]), reply("openrouter", "Fresh stream completed.")]) as api:
            terminal = Terminal(["--cwd", folder, "--approve"], environment(api.url), rows=40, columns=140)
            try:
                terminal.wait_for("Your terminal.")
                terminal.send("start the old edit\r")
                terminal.wait_for("Arguments buffered")
                terminal.send(b"\x1b")
                terminal.wait_for("Cancelled.")
                release.set()
                terminal.send("start a new edit\r")
                terminal.wait_for("Fresh stream completed.")
                self.assertFalse(Path(folder, "cancelled.txt").exists())
                self.assertEqual(Path(folder, "fresh.txt").read_text(), "Only the new stream.")
                self.assertNotIn("old data", json.dumps(api.requests[-1][1]))
            finally:
                release.set()
                self.assertEqual(terminal.close(), terminal.original)

    def test_invalid_argument_types_and_indices_fail_before_tools_execute(self):
        for function, index in (({"arguments": {}}, 0), ({"arguments": "{}"}, 128), ({"arguments": "{}"}, "bad")):
            with self.subTest(function=function, index=index), tempfile.TemporaryDirectory() as folder:
                events = [chunk({"tool_calls": [{"index": index, "id": "invalid", "type": "function",
                    "function": {"name": "write_file", **function}}]}, "tool_calls"), "[DONE]"]
                with MockAPI([StreamReply([sse(event) for event in events])]) as api:
                    result = self.run_agent(api, directory=folder, extra=("--approve",))
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(len(api.requests), 1)

    def test_many_small_file_deltas_preserve_full_write_and_edit(self):
        content = 'Wiersz: "żółw" i ścieżka C:\\tmp\\plik.\n' * 500
        for provider in ("openrouter", "claude"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                path = Path(folder, "README.md")
                path.write_text("Original content\n" * 1200)
                responses = []
                for index, (name, args) in enumerate([
                    ("write_file", {"path": "README.md", "content": content}),
                    ("edit_file", {"path": "README.md", "old_text": content, "new_text": content + "Koniec.\n"}),
                ]):
                    encoded = json.dumps(args, ensure_ascii=False)
                    pieces = [encoded[i:i + 16] for i in range(0, len(encoded), 16)]
                    if provider == "claude":
                        events = claude_events([({"type": "tool_use", "id": f"dense_{index}", "name": name, "input": {}},
                                                [{"type": "input_json_delta", "partial_json": part} for part in pieces])], "tool_use")
                    else:
                        events = [chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": f"dense_{index}", "type": "function",
                                   "function": {"name": name, "arguments": ""}}]})]
                        events += [chunk({"tool_calls": [{"index": 0, "function": {"arguments": part}}]}) for part in pieces]
                        events += [chunk(finish="tool_calls"), {"choices": [], "usage": {"total_tokens": 42}}, "[DONE]"]
                    responses.append(StreamReply([sse(event) for event in events]))
                with MockAPI([*responses, reply(provider, "Saved every line.")]) as api:
                    result = self.run_agent(api, provider, folder, extra=("--approve",), timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(path.read_text(), content + "Koniec.\n")
                    self.assertEqual(len(api.requests), 3)
                    self.assertNotIn("_input_json", json.dumps(api.requests[-1][1]))

    def test_argument_deltas_do_not_modify_identical_metadata(self):
        first = '{"path":"note.txt",'
        last = '"content":"Saved żółw\\n"}'
        for provider in ("openrouter", "claude"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                if provider == "claude":
                    events = claude_events([
                        ({"type": "thinking", "thinking": "", "signature": "unchanged"}, [{"type": "thinking_delta", "thinking": first}]),
                        ({"type": "tool_use", "id": "write", "name": "write_file", "input": {}},
                         [{"type": "input_json_delta", "partial_json": part} for part in (first, last)])], "tool_use")
                else:
                    events = [chunk({"role": "assistant", "reasoning_details": [{"type": "reasoning.text", "text": first}],
                        "tool_calls": [{"index": 0, "id": "write", "type": "function", "function": {"name": "write_file", "arguments": first}}]}),
                        chunk({"tool_calls": [{"index": 0, "function": {"arguments": last}}]}, "tool_calls"), "[DONE]"]
                with MockAPI([StreamReply([sse(event) for event in events]), reply(provider, "Done.")]) as api:
                    result = self.run_agent(api, provider, folder, extra=("--approve",))
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(Path(folder, "note.txt").read_text(), "Saved żółw\n")
                    message = next(m for m in api.requests[-1][1]["messages"] if m["role"] == "assistant")
                    if provider == "claude":
                        self.assertEqual(message["content"][0]["thinking"], first)
                        self.assertEqual(message["content"][0]["signature"], "unchanged")
                    else:
                        self.assertEqual(message["reasoning_details"][0]["text"], first)

    @unittest.skipUnless(os.environ.get("ZERO_TEST_SLOW") == "1", "set ZERO_TEST_SLOW=1 for the 105-second timeout regression")
    def test_provider_reply_after_100_seconds_still_writes_and_edits(self):
        release = threading.Event()
        waited = []

        def delayed_write():
            yield b": waiting for the provider\r\n\r\n"
            started = time.monotonic()
            if release.wait(105):
                return
            waited.append(time.monotonic() - started)
            yield sse(chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "delayed_write", "type": "function",
                "function": {"name": "write_file", "arguments": json.dumps({"path": "README.md", "content": "Translated README\n"})}}]}, "tool_calls"))
            yield sse("[DONE]")

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "README.md")
            original = "Original README line.\n" * 1100
            path.write_text(original)
            reads = [reply("openrouter", calls=[("read_file", {"path": "README.md", "offset": offset, "limit": 8192})])
                     for offset in range(0, len(original.encode()), 8192)]
            responses = [*reads, StreamReply(delayed_write()),
                reply("openrouter", calls=[("edit_file", {"path": "README.md", "old_text": "Translated", "new_text": "Edited"})]),
                reply("openrouter", "Completed after the delayed response.")]
            with MockAPI(responses) as api:
                try:
                    result = self.run_agent(api, directory=folder, extra=("--approve",), max_turns=8, timeout=130)
                finally:
                    release.set()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(waited), 1)
                self.assertGreaterEqual(waited[0], 105)
                self.assertEqual(path.read_text(), "Edited README\n")
                self.assertEqual(len(api.requests), len(reads) + 3)
                self.assertNotIn("timed out", result.stdout)
                self.assertIn("Completed after the delayed response.", result.stdout)

    def test_headless_text_is_flushed_before_stream_finishes(self):
        release = threading.Event()

        def chunks():
            yield sse(chunk({"role": "assistant", "content": "Early headless output"}))
            release.wait(8)
            yield sse(chunk({"content": " finished."}, "stop"))
            yield sse("[DONE]")

        with tempfile.TemporaryDirectory() as folder, MockAPI([StreamReply(chunks())]) as api:
            process = subprocess.Popen([str(agent.EXE), "--cwd", folder, "--prompt", "stream a response"],
                                       env=environment(api.url), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output = bytearray()
            try:
                deadline = time.monotonic() + 6
                while b"Early headless output" not in output and time.monotonic() < deadline:
                    ready, _, _ = select.select([process.stdout], [], [], 0.1)
                    if ready:
                        data = os.read(process.stdout.fileno(), 4096)
                        if not data:
                            break
                        output.extend(data)
                self.assertIn(b"Early headless output", output)
                self.assertIsNone(process.poll())
                release.set()
                tail, errors = process.communicate(timeout=8)
                self.assertEqual(process.returncode, 0, errors)
                self.assertIn(b"Early headless output finished.", output + tail)
            finally:
                release.set()
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=3)

    def test_long_claude_text_and_json_fallback(self):
        text = "żółw 🐢 " * 2500
        deltas = [{"type": "text_delta", "text": text[i:i + 400]} for i in range(0, len(text), 400)]
        events = claude_events([({"type": "text", "text": ""}, deltas)])
        for response in (StreamReply([sse(e) for e in events]), reply("claude", text)):
            with self.subTest(streaming=isinstance(response, StreamReply)), MockAPI([response]) as api:
                result = self.run_agent(api, "claude")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(text, result.stdout)
                self.assertNotIn("Empty response", result.stdout)

    def test_text_is_visible_before_stream_finishes(self):
        for provider in ("openrouter", "openai", "gemini", "claude"):
            with self.subTest(provider=provider):
                release = threading.Event()

                def chunks():
                    if provider == "claude":
                        yield sse({"type": "message_start", "message": {"role": "assistant", "content": [], "usage": {"input_tokens": 1}}})
                        yield sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
                        yield sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Early żółw"}})
                    else:
                        yield sse(chunk({"role": "assistant", "content": "Early żółw"}))
                    if not release.wait(8):
                        return
                    if provider == "claude":
                        yield sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " finished."}})
                        yield sse({"type": "content_block_stop", "index": 0})
                        yield sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
                        yield sse({"type": "message_stop"})
                    else:
                        yield sse(chunk({"content": " finished."}, "stop"))
                        yield sse("[DONE]")

                with MockAPI([StreamReply(chunks())]) as api:
                    terminal = Terminal(["--provider", provider], environment(api.url))
                    try:
                        terminal.wait_for("Your terminal.")
                        terminal.send("stream a response\r")
                        terminal.wait_for("Early żółw", timeout=6)
                        self.assertNotIn(b"finished.", terminal.output)
                        self.assertTrue(api.requests[0][1]["stream"])
                        after = len(terminal.output)
                        release.set()
                        terminal.wait_for("Early żółw finished.")
                        terminal.wait_for("ready", after=after)
                    finally:
                        release.set()
                        restored = terminal.close()
                    self.assertEqual(restored, terminal.original)

    def test_parallel_tool_deltas_and_metadata_round_trip(self):
        for provider in ("openrouter", "openai", "gemini"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                Path(folder, "one.txt").write_text("first file")
                Path(folder, "two.txt").write_text("second file")
                events = [chunk({"role": "assistant", "content": "Reading żółw. ", "reasoning_details": [{"index": 0, "type": "reasoning.text", "text": "Preserved "}],
                                  "tool_calls": [{"index": 1, "id": "second", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\":"}}]}),
                          chunk({"tool_calls": [{"index": 0, "id": "first", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\":"}}]}),
                          chunk({"reasoning_details": [{"index": 0, "text": "metadata."}], "tool_calls": [
                              {"index": 0, "function": {"arguments": "\"one.txt\"}"}, "extra_content": {"google": {"thought_signature": "keep-this-signature"}}},
                              {"index": 1, "function": {"arguments": "\"two.txt\"}"}}]}, "tool_calls"),
                          {"choices": [], "usage": {"total_tokens": 20}}, "[DONE]"]
                with MockAPI([StreamReply(fragmented(events)), reply(provider, "Finished streaming tools.")]) as api:
                    result = self.run_agent(api, provider, folder)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    message = next(m for m in api.requests[1][1]["messages"] if m["role"] == "assistant")
                    self.assertEqual([c["id"] for c in message["tool_calls"]], ["first", "second"])
                    self.assertTrue(all("index" not in c for c in message["tool_calls"]))
                    self.assertEqual(message["reasoning_details"][0]["text"], "Preserved metadata.")
                    self.assertEqual(message["tool_calls"][0]["extra_content"]["google"]["thought_signature"], "keep-this-signature")
                    self.assertEqual(result.stdout.count("Reading żółw."), 1)

    def test_claude_tool_json_and_thinking_signature(self):
        blocks = [({"type": "thinking", "thinking": "", "signature": ""}, [
                       {"type": "thinking_delta", "thinking": "Provider metadata"},
                       {"type": "signature_delta", "signature": "signed-thought"}]),
                  ({"type": "tool_use", "id": "tool_1", "name": "read_file", "input": {}}, [
                       {"type": "input_json_delta", "partial_json": "{\"path\":"},
                       {"type": "input_json_delta", "partial_json": "\"one.txt\"}"}])]
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "one.txt").write_text("Claude reads the file")
            with MockAPI([StreamReply(fragmented(claude_events(blocks, "tool_use"), 7)), reply("claude", "Done.")]) as api:
                result = self.run_agent(api, "claude", folder)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                content = api.requests[1][1]["messages"][1]["content"]
                self.assertEqual(content[0]["signature"], "signed-thought")
                self.assertEqual(content[1]["input"], {"path": "one.txt"})
                self.assertNotIn("_input_json", json.dumps(content))

    def test_failed_stream_never_executes_partial_tools(self):
        cases = [
            [chunk({"tool_calls": [{"index": 0, "id": "bad", "type": "function", "function": {
                "name": "write_file", "arguments": json.dumps({"path": "must-not-exist.txt", "content": "bad"})}}]})],
            [chunk({"content": "Starting"}), {"error": {"message": "stream failed"}}],
            ["not JSON", "[DONE]"],
            [chunk({"content": "Incomplete"}), "[DONE]"],
        ]
        for events in cases:
            with self.subTest(events=events), tempfile.TemporaryDirectory() as folder, MockAPI([StreamReply(fragmented(events))]) as api:
                result = self.run_agent(api, directory=folder, extra=("--approve",))
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(Path(folder, "must-not-exist.txt").exists())
                self.assertEqual(len(api.requests), 1)

    def test_stream_can_be_cancelled_then_next_turn_runs(self):
        release = threading.Event()

        def chunks():
            yield sse(chunk({"content": "Still streaming"}))
            release.wait(8)

        with MockAPI([StreamReply(chunks()), reply("openrouter", "Next task works.")]) as api:
            terminal = Terminal([], environment(api.url))
            try:
                terminal.wait_for("Your terminal.")
                terminal.send("start\r")
                terminal.wait_for("Still streaming")
                terminal.send(b"\x1b")
                terminal.wait_for("Cancelled.")
                release.set()
                terminal.send("new task\r")
                terminal.wait_for("Next task works.")
                self.assertNotIn("Still streaming", json.dumps(api.requests[-1][1]))
            finally:
                release.set()
                self.assertEqual(terminal.close(), terminal.original)
