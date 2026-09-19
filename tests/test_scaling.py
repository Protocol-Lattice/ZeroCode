"""Large files and bounded conversation memory through the compiled binary."""
import json
from pathlib import Path
import stat
import tempfile
import unittest

from tests import test_agent as agent
from tests.test_agent import MockAPI, Terminal, environment, reply


def assert_tool_pairs(test, messages, provider):
    pending = []
    for message in messages:
        if provider == "claude":
            content = message.get("content", [])
            if isinstance(content, str):
                test.assertFalse(pending)
                continue
            for block in content:
                if block.get("type") == "tool_use":
                    pending.append(block["id"])
                if block.get("type") == "tool_result":
                    test.assertIn(block["tool_use_id"], pending)
                    pending.remove(block["tool_use_id"])
        else:
            if message["role"] == "assistant":
                test.assertFalse(pending)
                pending = [call["id"] for call in message.get("tool_calls", [])]
            elif message["role"] == "tool":
                test.assertIn(message["tool_call_id"], pending)
                pending.remove(message["tool_call_id"])
            else:
                test.assertFalse(pending)
    test.assertFalse(pending)


class ScalingTests(unittest.TestCase):
    run_agent = agent.AgentTests.run_agent

    def test_large_file_paging_preserves_unicode_and_boundaries(self):
        content = ("line: żółw 🐢\n" * 10000).encode()
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "large.txt").write_bytes(content)
            calls = [("read_file", {"path": "large.txt", "offset": offset, "limit": 101})
                     for offset in (0, 7, len(content) - 20, len(content), len(content) + 1)]
            with MockAPI([reply("openrouter", calls=calls), reply("openrouter", "Read ranges.")]) as api:
                result = self.run_agent(api, directory=folder)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("line: żółw 🐢\n", result.stdout)
                self.assertIn("PARALLEL WORKERS", result.stdout)
                self.assertNotIn(r"\u000a", result.stdout)
                pages = [msg["content"] for msg in api.requests[1][1]["messages"] if msg["role"] == "tool"]
                for raw in pages[:-1]:
                    page = json.loads(raw)
                    self.assertEqual(page["total_bytes"], len(content))
                    self.assertEqual(page["content"].encode(), content[page["offset"]:page["next_offset"]])
                    self.assertEqual(page["truncated"], page["next_offset"] < len(content))
                self.assertIn("past the end", pages[-1])

    def test_paged_read_displays_summary_without_changing_provider_payload(self):
        content = 'First line\nSecond line with "quotes" and żółw 🐢\nTabbed\tvalue\n'
        for provider in ("openrouter", "openai", "claude", "gemini"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                Path(folder, "notes.txt").write_text(content)
                with MockAPI([reply(provider, calls=[("read_file", {"path": "notes.txt", "offset": 0, "limit": 12000})]),
                              reply(provider, "Read finished.")]) as api:
                    result = self.run_agent(api, provider, folder)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn("First line", result.stdout)
                    self.assertIn(f"✓ notes.txt · {len(content.encode())} B · 3 lines", result.stdout)
                    for escape in (r"\u000a", r"\u0009", r'\"', '"next_offset":'):
                        self.assertNotIn(escape, result.stdout)
                    if provider == "claude":
                        raw = next(block["content"] for message in api.requests[1][1]["messages"]
                                   for block in message.get("content", []) if isinstance(block, dict) and block.get("type") == "tool_result")
                    else:
                        raw = next(message["content"] for message in api.requests[1][1]["messages"] if message["role"] == "tool")
                    page = json.loads(raw)
                    self.assertEqual(page["content"], content)
                    self.assertEqual(page["next_offset"], len(content.encode()))

    def test_literal_backslash_sequences_in_source_are_preserved(self):
        content = r'Literal source: \u000a and \n and "quotes".'
        for extra_args in ({}, {"offset": 0, "limit": 12000}):
            with self.subTest(args=extra_args), tempfile.TemporaryDirectory() as folder:
                Path(folder, "source.txt").write_text(content)
                with MockAPI([reply("openrouter", calls=[("read_file", {"path": "source.txt", **extra_args})]),
                              reply("openrouter", "Done.")]) as api:
                    result = self.run_agent(api, directory=folder)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn(content, result.stdout)
                    raw = next(msg["content"] for msg in api.requests[1][1]["messages"] if msg["role"] == "tool")
                    self.assertEqual(json.loads(raw)["content"] if extra_args else raw, content)

    def test_tui_large_file_read_renders_compact_range_metadata(self):
        content = 'First readable line\nSecond readable line\n' + 'More documentation.\n' * 2000
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "large.md").write_text(content)
            with MockAPI([reply("openrouter", calls=[("read_file", {"path": "large.md", "offset": 0, "limit": 90})]),
                          reply("openrouter", "Read finished.")]) as api:
                terminal = Terminal(["--cwd", folder], environment(api.url))
                try:
                    terminal.wait_for("Your terminal.")
                    terminal.send("Read the file\r")
                    terminal.wait_for("Read finished.")
                    self.assertNotIn(b"First readable line", terminal.output)
                    self.assertIn("✓ large.md · 40.0 KB · 5 lines in range (bytes 0–90)".encode(), terminal.output)
                    self.assertNotIn(b"Second readable line", terminal.output)
                    self.assertNotIn(b"\\u000a", terminal.output)
                finally:
                    terminal.close()

    def test_large_edit_crosses_chunk_boundary_and_preserves_mode(self):
        original = b"a" * 32760 + "unique żółw marker".encode() + b"b" * (2 * 1024 * 1024)
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder, "large.txt")
            file.write_bytes(original)
            file.chmod(0o755)
            with MockAPI([reply("openrouter", calls=[("edit_file", {"path": "large.txt", "old_text": "unique żółw marker", "new_text": "replacement 🐢"})]),
                          reply("openrouter", "Large edit complete.")]) as api:
                result = self.run_agent(api, directory=folder, extra=("--approve",))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(file.read_bytes(), original.replace("unique żółw marker".encode(), "replacement 🐢".encode()))
                self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o755)
                self.assertEqual(list(Path(folder).glob("*.zero-edit.*")), [])

    def test_large_edit_rejects_changes_outside_preview_and_ambiguous_text(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder, "large.txt")
            file.write_text("before" + "x" * 100000)
            with MockAPI([reply("openrouter", calls=[("edit_file", {"path": "large.txt", "old_text": "before", "new_text": "after"})]),
                          reply("openrouter", "Checked stale edit.")]) as api:
                terminal = Terminal(["--cwd", folder], environment(api.url))
                try:
                    terminal.wait_for("Your terminal.")
                    terminal.send("edit the file\r")
                    terminal.wait_for("Approve this action?")
                    changed = file.read_text() + "user appended this"
                    file.write_text(changed)
                    terminal.send("y")
                    terminal.wait_for("Checked stale edit.")
                    self.assertEqual(file.read_text(), changed)
                    self.assertIn("changed since the preview", json.dumps(api.requests))
                    self.assertEqual(list(Path(folder).glob("*.zero-edit.*")), [])
                finally:
                    terminal.close()
            file.write_text("duplicate" + "x" * 100000 + "duplicate")
            with MockAPI([reply("openrouter", calls=[("edit_file", {"path": "large.txt", "old_text": "duplicate", "new_text": "after"})]),
                          reply("openrouter", "Ambiguous edit rejected.")]) as api:
                result = self.run_agent(api, directory=folder, extra=("--approve",))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(file.read_text().count("duplicate"), 2)
                self.assertIn("exactly once", json.dumps(api.requests))

    def test_long_tool_history_compacts_without_losing_task_or_tool_pairs(self):
        for provider in ("openrouter", "claude"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                memory = Path(folder, ".zero-agent/memory.json")
                memory.parent.mkdir()
                memory.write_text(json.dumps({"version": 1, "entries": [
                    {"key": "architecture", "content": "PERSISTENT_FACT_MUST_SURVIVE"}]}))
                responses = []
                for index in range(12):
                    path = f"file{index}.txt"
                    Path(folder, path).write_text(f"file {index}: " + "x" * 16000)
                    responses.append(reply(provider, calls=[("read_file", {"path": path})]))
                responses.append(reply(provider, "Long task complete."))
                with MockAPI(responses) as api:
                    result = self.run_agent(api, provider, folder, prompt="CURRENT_TASK_MUST_SURVIVE: inspect all files.", max_turns=20)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(len(api.requests), 13)
                    self.assertIn("CONTEXT COMPACTED", result.stdout)
                    for _, request in api.requests:
                        self.assertIn("CURRENT_TASK_MUST_SURVIVE", json.dumps(request["messages"]))
                        system = request["system"] if provider == "claude" else request["messages"][0]["content"]
                        self.assertIn("PERSISTENT_FACT_MUST_SURVIVE", system)
                        assert_tool_pairs(self, request["messages"], provider)

    def test_large_tool_batch_stays_balanced_during_compaction(self):
        for provider in ("openrouter", "claude"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                Path(folder, "file.txt").write_text("payload " + "x" * 15000)
                # Distinct bounded ranges avoid the repeated-result guard while
                # growing a single batch beyond the uncompressed history buffer.
                calls = [("read_file", {"path": "file.txt", "offset": i, "limit": 12000}) for i in range(12)]
                with MockAPI([reply(provider, calls=calls), reply(provider, "Batch complete.")]) as api:
                    result = self.run_agent(api, provider, folder)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(len(api.requests), 2)
                    self.assertIn("CONTEXT COMPACTED", result.stdout)
                    assert_tool_pairs(self, api.requests[1][1]["messages"], provider)

    def test_old_exchanges_are_summarized_when_arguments_fill_context(self):
        for provider in ("openrouter", "claude"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                responses = [reply(provider, calls=[("write_file", {"path": f"file{i}.txt", "content": f"file {i}:" + "x" * 10000})]) for i in range(16)]
                responses.append(reply(provider, "All files complete."))
                with MockAPI(responses) as api:
                    result = self.run_agent(api, provider, folder, extra=("--approve",), prompt="KEEP_USER_INSTRUCTIONS: create the requested files.", max_turns=20)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(len(api.requests), 17)
                    self.assertEqual(len(list(Path(folder).glob("file*.txt"))), 16)
                    self.assertIn("Client-generated digest", json.dumps(api.requests[-1][1]))
                    self.assertIn("Saved file", json.dumps(api.requests[-1][1]))
                    summaries = [m["content"] for _, request in api.requests for m in request["messages"]
                                 if isinstance(m.get("content"), str) and m["content"].startswith("Client-generated digest")]
                    self.assertTrue(any("Saved file0.txt" in summary for summary in summaries), summaries)
                    for _, request in api.requests:
                        self.assertIn("KEEP_USER_INSTRUCTIONS", json.dumps(request))
                        assert_tool_pairs(self, request["messages"], provider)
