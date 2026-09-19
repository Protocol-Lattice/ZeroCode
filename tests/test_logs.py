"""Automatic JSONL journals, live persistence, redaction and opt-out behavior."""

import json
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
import time
import unittest

from tests import test_agent as agent
from tests.test_agent import EXE, MockAPI, StreamReply, Terminal, environment, reply
from tests.test_streaming import chunk, sse
from tests.test_parallel import latest_user_text, results, start_agent


def journals(root):
    return sorted(Path(root, ".zero-agent/sessions").glob("*/events.jsonl"))


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def logged_text(path, event=None, title=None):
    return "".join(record["text"] for record in records(path)
                   if (event is None or record["event"] == event)
                   and (title is None or record["title"] == title))


class SessionLogTests(unittest.TestCase):
    run_agent = agent.AgentTests.run_agent

    def command(self, folder, prompt="/logs", extra=()):
        return subprocess.run([str(EXE), "--cwd", str(folder), "--no-skills", "--prompt", prompt, *extra],
                              env=environment(keys=False), capture_output=True, text=True, timeout=10)

    def test_default_logs_all_providers_and_tools_with_private_permissions(self):
        for provider in ("openrouter", "openai", "claude", "gemini"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory(prefix="zero logs ") as folder:
                with MockAPI([reply(provider, "Running a check.", calls=[("run_command", {"command": "printf 'tool output'"})]),
                              reply(provider, "Check complete.")]) as api:
                    result = self.run_agent(api, provider, folder, extra=("--approve",), prompt="Run my check.")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(journals(folder)), 1)
                path = journals(folder)[0]
                entries = records(path)
                self.assertEqual(entries[0]["event"], "session_start")
                self.assertEqual(entries[-1]["event"], "session_end")
                self.assertEqual(entries[-1]["text"], "closed")
                self.assertTrue(all(entry["time"] > 0 and entry["version"] == 1 for entry in entries))
                self.assertEqual([entry["seq"] for entry in entries], sorted(entry["seq"] for entry in entries))
                for expected in ("Run my check.", "Running a check.", "tool output", "Check complete."):
                    self.assertIn(expected, logged_text(path))
                self.assertTrue(any(entry["event"] == "approval" for entry in entries))
                self.assertNotIn("test-key-never-render-me", path.read_text())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                self.assertNotIn("Run my check.", Path(folder, ".zero-agent/memory.json").read_text())

    def test_stream_is_on_disk_before_completion_and_survives_process_kill(self):
        release = threading.Event()

        def chunks():
            yield sse(chunk({"role": "assistant", "content": "Early żółw output."}))
            release.wait(10)
            yield sse(chunk({"content": " Later."}, "stop"))
            yield sse("[DONE]")

        with tempfile.TemporaryDirectory() as folder, MockAPI([StreamReply(chunks())]) as api:
            process = subprocess.Popen([str(EXE), "--cwd", folder, "--prompt", "Save partial work."],
                                       env=environment(api.url), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 7
                while time.monotonic() < deadline:
                    paths = journals(folder)
                    if paths and "Early żółw output." in logged_text(paths[0]):
                        break
                    time.sleep(0.03)
                else:
                    self.fail("Stream text was not persisted while the response was still in progress")
                self.assertIsNone(process.poll())
                path = paths[0]
                process.kill()
                process.communicate(timeout=5)
                self.assertIn("Early żółw output.", logged_text(path, "assistant_delta"))
                self.assertNotIn("session_end", [record["event"] for record in records(path)])
                self.assertFalse(Path(folder, ".zero-agent/memory.json").exists())
            finally:
                release.set()
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)

    def test_stream_redacts_keys_across_deltas_and_record_boundaries(self):
        key = "test-key-never-render-me"
        text = "żółw 🐢\n\"quoted\" " * 400 + key + " safe ending."
        events = [chunk({"content": text[:6120]}), chunk({"content": text[6120:-18]}),
                  chunk({"content": text[-18:]}, "stop"), "[DONE]"]
        with tempfile.TemporaryDirectory() as folder, MockAPI([StreamReply([sse(e) for e in events])]) as api:
            result = self.run_agent(api, directory=folder, prompt="Please hide " + key)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            path = journals(folder)[0]
            self.assertEqual(logged_text(path, "assistant_delta"), text.replace(key, "[REDACTED]"))
            self.assertNotIn(key, path.read_text())
            self.assertEqual(logged_text(path, "entry", "YOU"), "Please hide [REDACTED]")
            self.assertEqual(logged_text(path, "stream_end"), "completed")

    def test_error_denial_and_cancellation_are_retained(self):
        with tempfile.TemporaryDirectory() as folder, MockAPI([(500, {"error": {"message": "Provider unavailable"}})]) as api:
            result = self.run_agent(api, directory=folder)
            self.assertNotEqual(result.returncode, 0)
            path = journals(folder)[0]
            self.assertIn("Provider unavailable", logged_text(path))
            self.assertEqual(records(path)[-1]["text"], "error")
        with tempfile.TemporaryDirectory() as folder, MockAPI([
                reply("openrouter", calls=[("run_command", {"command": "echo denied"})]),
                reply("openrouter", "Respecting the denial.")]) as api:
            self.assertEqual(self.run_agent(api, directory=folder).returncode, 0)
            self.assertIn("action denied", logged_text(journals(folder)[0]))
        release = threading.Event()

        def chunks():
            yield sse(chunk({"content": "Partial reply before cancellation."}))
            release.wait(10)

        with tempfile.TemporaryDirectory() as folder, MockAPI([StreamReply(chunks())]) as api:
            terminal = Terminal(["--cwd", folder], environment(api.url))
            try:
                terminal.wait_for("Your terminal.")
                terminal.send("Start a task\r")
                terminal.wait_for("Partial reply before cancellation.")
                terminal.send(b"\x1b")
                terminal.wait_for("Cancelled.")
                path = journals(folder)[0]
                self.assertIn("Cancelled.", logged_text(path))
                self.assertEqual(logged_text(path, "stream_end"), "interrupted")
            finally:
                release.set()
                terminal.close()

    def test_clear_keeps_logs_and_restart_creates_a_new_file(self):
        with tempfile.TemporaryDirectory() as folder, MockAPI([reply("openrouter", "First completed reply.")]) as api:
            terminal = Terminal(["--cwd", folder], environment(api.url))
            try:
                terminal.wait_for("Your terminal.")
                terminal.send("Original user task\r")
                terminal.wait_for("First completed reply.")
                terminal.send("/clear\r/logs\r")
                terminal.wait_for("events.jsonl")
                path = journals(folder)[0]
                self.assertIn("Original user task", logged_text(path))
                self.assertIn("First completed reply.", logged_text(path))
                self.assertTrue(any(record["event"] == "reset" for record in records(path)))
            finally:
                terminal.close()
            before = path.read_bytes()
            result = self.command(folder)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(len(journals(folder)), 2)
            self.assertEqual(path.read_bytes(), before)

    def test_logs_and_memory_have_independent_switches(self):
        with tempfile.TemporaryDirectory() as folder, MockAPI([reply("openrouter", "Remember this recap.")]) as api:
            self.assertEqual(self.run_agent(api, directory=folder, extra=("--no-session-logs",)).returncode, 0)
            self.assertEqual(journals(folder), [])
            self.assertTrue(Path(folder, ".zero-agent/memory.json").exists())
            self.assertIn("disabled", self.command(folder, extra=("--no-session-logs",)).stdout)
        with tempfile.TemporaryDirectory() as folder, MockAPI([reply("openrouter", "Only a log.")]) as api:
            self.assertEqual(self.run_agent(api, directory=folder, extra=("--no-memory",)).returncode, 0)
            self.assertEqual(len(journals(folder)), 1)
            self.assertFalse(Path(folder, ".zero-agent/memory.json").exists())
        for args in (("--demo", "--snapshot"), ("--snapshot",), ("--self-test",), ("--help",)):
            with self.subTest(args=args), tempfile.TemporaryDirectory() as folder:
                subprocess.run([str(EXE), "--cwd", folder, *args], env=environment(keys=False), capture_output=True, timeout=10)
                self.assertEqual(journals(folder), [])

    def test_unsafe_storage_and_write_failure_do_not_stop_tasks(self):
        for target in (".zero-agent", ".zero-agent/sessions"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
                link = Path(folder, target)
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(outside, target_is_directory=True)
                result = self.command(folder)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("SESSION LOG UNAVAILABLE", result.stdout)
                self.assertEqual(list(Path(outside).iterdir()), [])
        with tempfile.TemporaryDirectory() as folder:
            terminal = Terminal(["--cwd", folder], environment(keys=False))
            try:
                terminal.wait_for("Your terminal.")
                path = journals(folder)[0]
                path.unlink()
                path.mkdir()
                terminal.send("/status\r")
                terminal.wait_for("SESSION LOG UNAVAILABLE")
                terminal.send("/logs\r")
                terminal.wait_for("Saving failed")
                self.assertIsNone(terminal.process.poll())
            finally:
                terminal.close()

    def test_keys_entered_in_the_dialog_are_not_logged(self):
        key = "a-manually-entered-secret"
        with tempfile.TemporaryDirectory() as folder, MockAPI([reply("openrouter", "Echo: " + key)]) as api:
            terminal = Terminal(["--cwd", folder], environment(api.url, keys=False))
            try:
                terminal.wait_for("Your terminal.")
                terminal.send("/key\r")
                terminal.wait_for("API key")
                terminal.send(key + "\r")
                terminal.wait_for("CONNECTED")
                terminal.send("Reply once\r")
                terminal.wait_for("Echo:")
            finally:
                terminal.close()
            path = journals(folder)[0]
            self.assertNotIn(key, path.read_text())
            self.assertIn("Echo: [REDACTED]", logged_text(path))

    def test_long_entries_survive_display_limits_and_concurrent_sessions(self):
        text = "Preserve this log line.\n" * 3000
        with tempfile.TemporaryDirectory() as folder, MockAPI([reply("openrouter", text)]) as api:
            result = self.run_agent(api, directory=folder)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            path = journals(folder)[0]
            self.assertEqual(logged_text(path, "entry", "ZERO"), text)
            parts = [entry for entry in records(path) if entry["title"] == "ZERO"]
            self.assertGreater(len(parts), 1)
            self.assertEqual([part["part"] for part in parts], list(range(len(parts))))
            self.assertTrue(parts[-1]["last"])
            processes = [subprocess.Popen([str(EXE), "--cwd", folder, "--prompt", "/logs"],
                                          env=environment(keys=False), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                         for _ in range(2)]
            for process in processes:
                output, error = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, (output, error))
            self.assertEqual(len(journals(folder)), 3)
            self.assertEqual(logged_text(path, "entry", "ZERO"), text)

    def test_worker_streams_share_journal_and_redact_split_keys(self):
        release = threading.Event()

        def chunks():
            yield sse(chunk({"content": "Worker starting test-key-"}))
            release.wait(10)
            yield sse(chunk({"content": "never-render-me finished t"}, "stop"))
            yield sse("[DONE]")

        def route(body):
            if "LOG_CHILD" in latest_user_text(body):
                return StreamReply(chunks())
            if not results(body, "openrouter"):
                return reply("openrouter", calls=[("delegate_tasks", {"tasks": [
                    {"mode": "read", "paths": ["file.txt"], "prompt": "LOG_CHILD"}]})])
            return reply("openrouter", "Delegated task finished.")

        with tempfile.TemporaryDirectory() as folder, MockAPI([route]) as api:
            Path(folder, "file.txt").write_text("Read this file.")
            process = start_agent(api, folder)
            try:
                deadline = time.monotonic() + 7
                while time.monotonic() < deadline:
                    paths = journals(folder)
                    if paths and "Worker starting " in logged_text(paths[0], "worker_delta"):
                        break
                    time.sleep(0.03)
                else:
                    self.fail("Worker activity was not saved incrementally")
                self.assertNotIn("test-key-", logged_text(paths[0], "worker_delta"))
                release.set()
                output, error = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, (output, error))
                self.assertEqual(len(journals(folder)), 1)
                self.assertIn("Worker starting [REDACTED] finished t", logged_text(paths[0], "worker_delta"))
                self.assertNotIn("test-key-never-render-me", paths[0].read_text())
            finally:
                release.set()
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
