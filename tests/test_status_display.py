"""Presentation regressions against the real worker pipes and terminal renderer."""

import json
import os
from pathlib import Path
import re
import select
import tempfile
import threading
import time
import unittest

from tests import test_agent as agent
from tests.test_agent import MockAPI, Terminal, environment, reply
from tests.test_parallel import latest_user_text, results


def pump(terminal, duration=0.25):
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        readable, _, _ = select.select([terminal.master], [], [], 0.03)
        if readable:
            terminal.output.extend(os.read(terminal.master, 65536))


def screens(terminal, rows=38, columns=110):
    """Interpret the CSI subset used by Zero, including incremental row erases."""
    # A PTY read may end partway through a frame after wait_for saw its text.
    pump(terminal, 0.1)
    cells = [[" "] * columns for _ in range(rows)]
    row = col = 0
    text = terminal.output.decode("utf-8", errors="replace")
    at = 0
    snapshots = []
    while at < len(text):
        if text[at] == "\x1b":
            match = re.match(r"\x1b\[([0-9;?]*)([@-~])", text[at:])
            if not match:
                at += 1
                continue
            params, command = match.groups()
            if command == "H":
                position = [int(value or 1) for value in (params or "1;1").split(";")]
                row, col = position[0] - 1, position[-1] - 1
            elif command == "J" and params == "2":
                cells = [[" "] * columns for _ in range(rows)]
            elif command == "K" and params == "2" and 0 <= row < rows:
                cells[row] = [" "] * columns
            elif command == "l" and params == "?25":
                snapshots.append(["".join(line).rstrip() for line in cells])
            at += len(match.group())
        else:
            char = text[at]
            if char == "\n":
                row += 1
            elif char == "\r":
                col = 0
            elif ord(char) >= 32:
                if 0 <= row < rows and 0 <= col < columns:
                    cells[row][col] = char
                col += 1
            at += 1
    return snapshots


class StatusDisplayTests(unittest.TestCase):
    run_agent = agent.AgentTests.run_agent

    def test_read_summary_keeps_full_model_and_journal_result(self):
        content = "PRIVATE_FILE_CONTENT żółw\n" * 400 + "last line"
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "README.md").write_text(content)
            with MockAPI([reply("openrouter", calls=[("read_file", {"path": "README.md"})]),
                          reply("openrouter", "Read complete.")]) as api:
                result = self.run_agent(api, directory=folder)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("TOOL\nread_file README.md", result.stdout)
                size = len(content.encode())
                self.assertIn(f"✓ README.md · {size // 1000}.{size % 1000 // 100} KB · 401 lines", result.stdout)
                self.assertNotIn("PRIVATE_FILE_CONTENT", result.stdout)
                self.assertEqual(results(api.requests[-1][1], "openrouter")[-1][1], content)
                journal = next(Path(folder, ".zero-agent/sessions").glob("*/events.jsonl"))
                records = [json.loads(line) for line in journal.read_text().splitlines()]
                raw = "".join(item["text"] for item in records if item["title"] == "TOOL RESULT")
                self.assertEqual(raw, content)

    def test_small_empty_write_list_and_error_summaries(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "empty.txt").write_text("")
            Path(folder, "small.txt").write_text("żółw\nlast")
            calls = [("read_file", {"path": "empty.txt"}),
                     ("read_file", {"path": "small.txt"}),
                     ("write_file", {"path": "written.txt", "content": "żółw"}),
                     ("list_files", {}),
                     ("read_file", {"path": "missing.txt"})]
            with MockAPI([reply("openrouter", calls=calls), reply("openrouter", "Checked.")]) as api:
                result = self.run_agent(api, directory=folder, extra=("--parallel", "1", "--approve"))
                self.assertEqual(result.returncode, 0, result.stderr)
                for expected in ("✓ empty.txt · 0 B · 0 lines", "✓ small.txt · 12 B · 2 lines",
                                 "✓ wrote 7 B", "✓ 3 files", "✗ Error: file is missing or unreadable."):
                    self.assertIn(expected, result.stdout)
                self.assertEqual(results(api.requests[-1][1], "openrouter")[1][1], "żółw\nlast")

    def test_parallel_read_and_failure_have_one_compact_summary(self):
        content = "HIDDEN_PARALLEL_PAYLOAD\n" * 500
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "large.txt").write_text(content)
            with MockAPI([reply("openrouter", calls=[
                ("read_file", {"path": "large.txt"}), ("read_file", {"path": "missing.txt"})]),
                    reply("openrouter", "Finished both.")]) as api:
                result = self.run_agent(api, directory=folder)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.count("PARALLEL WORKERS"), 1)
                self.assertEqual(result.stdout.count("worker-1"), 1)
                self.assertEqual(result.stdout.count("worker-2"), 1)
                self.assertIn("worker-1   done   1 tools", result.stdout)
                self.assertIn("worker-2   error   read_file", result.stdout)
                self.assertIn("file is missing or unreadable", result.stdout)
                self.assertNotIn("HIDDEN_PARALLEL_PAYLOAD", result.stdout)
                self.assertNotIn("TOOL RESULT", result.stdout)
                self.assertEqual(results(api.requests[-1][1], "openrouter")[0][1], content)
                journal = next(Path(folder, ".zero-agent/sessions").glob("*/events.jsonl"))
                records = [json.loads(line) for line in journal.read_text().splitlines()]
                self.assertIn(content, "".join(item["text"] for item in records if item["event"] == "worker_delta"))

    def test_live_workers_keep_order_scroll_input_and_completed_timing(self):
        gates = {name: threading.Event() for name in ("first_tools", "first_done", "second_fail")}
        first_read = threading.Event()
        both_running = threading.Event()
        started = set()
        lock = threading.Lock()
        root_requests = []
        markers = ("STATUS_ONE", "STATUS_TWO", "STATUS_THREE")

        def route(body):
            prompt = latest_user_text(body)
            marker = next((value for value in markers if value in prompt), None)
            if marker is None:
                root_requests.append(body)
                if "draft survives" in prompt:
                    return reply("openrouter", "Draft sent successfully.")
                if not results(body, "openrouter"):
                    return reply("openrouter", "\n".join(f"History line {i:02}" for i in range(50)), [
                        ("delegate_tasks", {"tasks": [
                            {"mode": "read", "paths": ["one.txt"], "prompt": name} for name in markers]})])
                return reply("openrouter", "Parallel run finished.")
            with lock:
                started.add(marker)
                if set(markers[:2]) <= started:
                    both_running.set()
            if marker == "STATUS_ONE":
                if not results(body, "openrouter"):
                    gates["first_tools"].wait(15)
                    return reply("openrouter", calls=[
                        ("read_file", {"path": "one.txt"}),
                        ("read_file", {"path": "one.txt", "offset": 1, "limit": 8})])
                first_read.set()
                gates["first_done"].wait(15)
                return reply("openrouter", "Worker one complete.")
            if marker == "STATUS_TWO":
                gates["second_fail"].wait(15)
                return 500, {"error": {"message": "permission denied"}}
            return reply("openrouter", "Worker three complete.")

        with tempfile.TemporaryDirectory() as folder:
            raw_content = "HIDDEN_WORKER_CONTENT\x1b[31m żółw\n" * 50
            Path(folder, "one.txt").write_text(raw_content)
            with MockAPI([route]) as api:
                terminal = Terminal(["--cwd", folder, "--parallel", "2"], environment(api.url), rows=38, columns=110)
                try:
                    terminal.wait_for("Your terminal.")
                    terminal.send("Run workers\r")
                    self.assertTrue(both_running.wait(6))
                    terminal.wait_for("worker-3   queued")
                    screen = "\n".join(screens(terminal)[-1])
                    self.assertIn("worker-1   running", screen)
                    self.assertIn("worker-2   running", screen)
                    terminal.send("draft survives\r" + "\x1b[A" * 12)
                    pump(terminal)
                    previous = screens(terminal)[-1]
                    self.assertIn("draft survives", previous[33])
                    self.assertEqual(len(root_requests), 1)
                    gates["first_tools"].set()
                    self.assertTrue(first_read.wait(6))
                    pump(terminal)
                    current = screens(terminal)[-1]
                    self.assertEqual(current[4:20], previous[4:20], "Worker events moved the historical viewport")
                    self.assertIn("draft survives", current[33])
                    terminal.send("\x1b[B" * 12)
                    pump(terminal)
                    gates["first_done"].set()
                    terminal.wait_for("worker-1   done   2 tools")
                    terminal.wait_for("worker-3   done   0 tools")
                    done = next(line for line in screens(terminal)[-1] if "worker-1" in line)
                    pump(terminal, 1.1)
                    self.assertEqual(next(line for line in screens(terminal)[-1] if "worker-1" in line), done)
                    gates["second_fail"].set()
                    terminal.wait_for("Parallel run finished.")
                    screen = "\n".join(screens(terminal)[-1])
                    self.assertIn("worker-2   error", screen)
                    self.assertIn("permission denied", screen)
                    for frame in screens(terminal):
                        ids = re.findall(r"worker-[123]", "\n".join(frame))
                        self.assertEqual(ids, sorted(set(ids)), "Duplicate or reordered worker rows")
                    self.assertNotIn(b"HIDDEN_WORKER_CONTENT", terminal.output)
                    journal = next(Path(folder, ".zero-agent/sessions").glob("*/events.jsonl"))
                    records = [json.loads(line) for line in journal.read_text().splitlines()]
                    self.assertIn(raw_content, "".join(item["text"] for item in records if item["event"] == "worker_delta"))
                    self.assertNotIn(b"PARALLEL WORKER\r", terminal.output)
                    self.assertEqual(terminal.output.count(b"\x1b[2J"), 1, "Worker updates cleared the full screen")
                    before = len(terminal.output)
                    pump(terminal)
                    self.assertEqual(len(terminal.output), before, "Unchanged idle frames were redrawn")
                    terminal.send("\r")
                    terminal.wait_for("Draft sent successfully.")
                    self.assertEqual(latest_user_text(root_requests[-1]), "draft survives")
                finally:
                    for gate in gates.values():
                        gate.set()
                    self.assertEqual(terminal.close(), terminal.original)

    def test_waiting_worker_changes_tool_without_duplicate_rows_and_cancels(self):
        def route(body):
            if "EDIT_CHILD" in latest_user_text(body):
                if not results(body, "openrouter"):
                    return reply("openrouter", calls=[("read_file", {"path": "one.txt"}),
                        ("write_file", {"path": "one.txt", "content": "replacement"})])
                return reply("openrouter", "Child done.")
            return reply("openrouter", calls=[("delegate_tasks", {"tasks": [
                {"mode": "write", "paths": ["one.txt"], "prompt": "EDIT_CHILD"},
                {"mode": "write", "paths": ["one.txt"], "prompt": "QUEUED_CHILD"}]})])

        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "one.txt").write_text("original")
            with MockAPI([route]) as api:
                terminal = Terminal(["--cwd", folder], environment(api.url), rows=38, columns=110)
                try:
                    terminal.wait_for("Your terminal.")
                    terminal.send("Review worker edits\r")
                    terminal.wait_for("worker-1   waiting   write_file")
                    terminal.wait_for("worker-2   queued")
                    frame = "\n".join(screens(terminal)[-1])
                    self.assertEqual(frame.count("worker-1"), 1)
                    self.assertIn("one.txt", frame)
                    terminal.send(b"\x1b")
                    terminal.wait_for("Cancelled.")
                    frame = "\n".join(screens(terminal)[-1])
                    self.assertIn("worker-1   cancelled   1 tools", frame)
                    self.assertIn("worker-2   cancelled   0 tools", frame)
                    self.assertEqual(Path(folder, "one.txt").read_text(), "original")
                finally:
                    self.assertEqual(terminal.close(), terminal.original)


if __name__ == "__main__":
    unittest.main()
