"""Project memory through the compiled binary, local APIs and real restarts."""

import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading
import unittest

from tests import test_agent as agent
from tests.test_agent import EXE, MockAPI, StreamReply, Terminal, environment, reply
from tests.test_parallel import advertised_tools, latest_user_text, results
from tests.test_streaming import chunk, sse


def memory_file(root):
    return Path(root) / ".zero-agent/memory.json"


def seed_memory(root, entries):
    path = memory_file(root)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"version": 1, "entries": entries}, ensure_ascii=False))
    return path


def system_text(body, provider="openrouter"):
    return body["system"] if provider == "claude" else body["messages"][0]["content"]


class MemoryTests(unittest.TestCase):
    run_agent = agent.AgentTests.run_agent

    def command(self, root, text, extra=()):
        return subprocess.run([str(EXE), "--cwd", str(root), "--no-skills", "--prompt", text, *extra],
                              env=environment(keys=False), text=True, capture_output=True, timeout=10)

    def test_save_reload_and_recall_for_all_providers(self):
        note = 'Run make test. Project name: żółw 🐢.\nKeep "examples" accurate.'
        for provider in ("openrouter", "openai", "claude", "gemini"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory(prefix="zero memory ") as folder:
                with MockAPI([
                    reply(provider, calls=[("memory", {"action": "set", "key": "testing", "content": note})]),
                    reply(provider, "Remembered.")]) as api:
                    result = self.run_agent(api, provider, folder, extra=("--approve",))
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("Project memory saved", result.stdout)
                    self.assertIn("memory", advertised_tools(api.requests[0][1], provider))
                    self.assertIn("testing", system_text(api.requests[1][1], provider))
                path = memory_file(folder)
                self.assertEqual(json.loads(path.read_text())["entries"], [{"key": "testing", "content": note}])
                self.assertEqual(json.loads(path.read_text())["recent"], ["Remembered."])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                self.assertEqual([item.name for item in path.parent.iterdir()], ["memory.json"])
                self.assertNotIn("test-key-never-render-me", path.read_text())
                with MockAPI([reply(provider, calls=[("memory", {"action": "list"})]),
                              reply(provider, "Recalled.")]) as api:
                    result = self.run_agent(api, provider, folder)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    system = system_text(api.requests[0][1], provider)
                    self.assertIn("<project_memory>", system)
                    self.assertIn("untrusted reference material", system)
                    self.assertIn("Remembered.", system)
                    self.assertEqual(json.loads(results(api.requests[1][1], provider)[-1][1])["entries"],
                                     [{"key": "testing", "content": note}])

    def test_commands_update_delete_clear_and_preserve_workspace_boundaries(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as other:
            result = self.command(folder, "/memory")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(memory_file(folder).parent.exists())
            for text in ("/memory set build make build", "/memory set tests make test", "/memory set build make setup build"):
                result = self.command(folder, text)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            stored = json.loads(memory_file(folder).read_text())["entries"]
            self.assertEqual({entry["key"]: entry["content"] for entry in stored},
                             {"build": "make setup build", "tests": "make test"})
            result = self.command(other, "/memory")
            self.assertNotIn("make setup build", result.stdout)
            self.assertFalse(memory_file(other).exists())
            self.assertEqual(self.command(folder, "/memory delete build").returncode, 0)
            self.assertEqual(json.loads(memory_file(folder).read_text())["entries"],
                             [{"key": "tests", "content": "make test"}])
            self.assertEqual(self.command(folder, "/memory clear").returncode, 0)
            self.assertEqual(json.loads(memory_file(folder).read_text())["entries"], [])

    def test_model_mutations_are_denied_without_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            path = seed_memory(folder, [{"key": "keep", "content": "Keep this fact."}])
            before = path.read_bytes()
            calls = [("memory", {"action": "set", "key": "new", "content": "Do not save."}),
                     ("memory", {"action": "delete", "key": "keep"}),
                     ("memory", {"action": "clear"})]
            with MockAPI([reply("openrouter", calls=calls), reply("openrouter", "Denied.")]) as api:
                result = self.run_agent(api, directory=folder)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(results(api.requests[1][1], "openrouter")), 3)
                self.assertTrue(all("denied" in text for _, text in results(api.requests[1][1], "openrouter")))
            self.assertEqual(path.read_bytes(), before)

    def test_no_memory_disables_read_tools_and_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = seed_memory(folder, [{"key": "private", "content": "NEVER_IN_DISABLED_CONTEXT"}])
            before = path.read_bytes()
            with MockAPI([reply("openrouter", calls=[("memory", {"action": "clear"})]),
                          reply("openrouter", "Disabled.")]) as api:
                result = self.run_agent(api, directory=folder, extra=("--no-memory", "--approve"))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("memory", advertised_tools(api.requests[0][1], "openrouter"))
                self.assertNotIn("NEVER_IN_DISABLED_CONTEXT", json.dumps(api.requests))
                self.assertIn("disabled", result.stdout)
            self.assertNotEqual(self.command(folder, "/memory clear", ("--no-memory",)).returncode, 0)
            self.assertEqual(path.read_bytes(), before)

    def test_plain_chat_saves_final_recap_without_persisting_raw_conversations(self):
        with tempfile.TemporaryDirectory() as folder:
            path = seed_memory(folder, [{"key": "stack", "content": "Use Zero."}])
            with MockAPI([reply("openrouter", "A transient answer.")]) as api:
                result = self.run_agent(api, directory=folder, extra=("--chat-only",), prompt="TRANSIENT_PROMPT")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("Use Zero.", system_text(api.requests[0][1]))
                self.assertNotIn("tools", api.requests[0][1])
            stored = json.loads(path.read_text())
            self.assertEqual(stored["entries"], [{"key": "stack", "content": "Use Zero."}])
            self.assertEqual(stored["recent"], ["A transient answer."])
            self.assertNotIn("TRANSIENT_PROMPT", path.read_text())
            self.assertNotIn("test-key-never-render-me", path.read_text())
        with tempfile.TemporaryDirectory() as folder, MockAPI([reply("openrouter", "No notes.")]) as api:
            self.assertEqual(self.run_agent(api, directory=folder).returncode, 0)
            self.assertEqual(json.loads(memory_file(folder).read_text())["recent"], ["No notes."])

    def test_invalid_inputs_do_not_replace_existing_notes(self):
        cases = [
            {"action": "set", "key": "../escape", "content": "bad"},
            {"action": "set", "key": "k" * 65, "content": "bad"},
            {"action": "set", "key": "empty", "content": " \n\t"},
            {"action": "set", "key": "nul", "content": "bad\x00text"},
            {"action": "set", "key": "large", "content": "🐢" * 257},
            {"action": "delete", "key": "unknown"},
            {"action": "unknown"},
            {"action": "set", "key": "missing"},
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = seed_memory(folder, [{"key": "keep", "content": "Keep me."}])
            before = path.read_bytes()
            with MockAPI([reply("openrouter", calls=[("memory", case) for case in cases]),
                          reply("openrouter", "Invalid inputs rejected.")]) as api:
                result = self.run_agent(api, directory=folder, extra=("--approve",))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                outcomes = results(api.requests[1][1], "openrouter")
                self.assertEqual(len(outcomes), len(cases))
                self.assertTrue(all(text.startswith("Error:") for _, text in outcomes), outcomes)
            self.assertEqual(path.read_bytes(), before)

    def test_entry_and_byte_budgets_with_utf8_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(self.command(folder, "/memory set boundary " + "🐢" * 256).returncode, 0)
            before = memory_file(folder).read_bytes()
            self.assertNotEqual(self.command(folder, "/memory set boundary " + "🐢" * 257).returncode, 0)
            self.assertEqual(memory_file(folder).read_bytes(), before)
            path = seed_memory(folder, [{"key": f"key{i}", "content": "fact"} for i in range(32)])
            before = path.read_bytes()
            self.assertNotEqual(self.command(folder, "/memory set overflow fact").returncode, 0)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(self.command(folder, "/memory set key0 updated").returncode, 0)
            self.assertEqual(len(json.loads(path.read_text())["entries"]), 32)
            entries = [{"key": f"key{i}", "content": "x" * 1024} for i in range(7)]
            path = seed_memory(folder, entries)
            before = path.read_bytes()
            self.assertNotEqual(self.command(folder, "/memory set overflow " + "x" * 1024).returncode, 0)
            self.assertEqual(path.read_bytes(), before)

    def test_corrupt_or_unsupported_stores_are_not_loaded_or_overwritten(self):
        invalid = [b"", b"{broken", b"\xff", b"x" * 8193,
                   b'{"version":2,"entries":[]}', b'{"version":1,"entries":[null]}',
                   json.dumps({"version": 1, "entries": [{"key": "k", "content": "BAD_STORE"}] * 2}).encode()]
        for data in invalid:
            with self.subTest(data=data[:60]), tempfile.TemporaryDirectory() as folder:
                path = seed_memory(folder, [])
                path.write_bytes(data)
                with MockAPI([reply("openrouter", calls=[("memory", {"action": "clear"})]),
                              reply("openrouter", "Store unavailable.")]) as api:
                    result = self.run_agent(api, directory=folder, extra=("--approve",))
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("MEMORY UNAVAILABLE", result.stdout)
                    self.assertNotIn("<project_memory>", system_text(api.requests[0][1]))
                self.assertEqual(path.read_bytes(), data)

    def test_symlink_directories_files_and_special_files_are_rejected(self):
        for kind in ("directory", "file", "dangling", "fifo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
                target = Path(outside) / "memory.json"
                target.write_text('{"version":1,"entries":[{"key":"secret","content":"OUTSIDE_MARKER"}]}')
                before = target.read_bytes()
                path = memory_file(folder)
                if kind == "directory":
                    path.parent.symlink_to(outside, target_is_directory=True)
                else:
                    path.parent.mkdir()
                    if kind == "fifo":
                        os.mkfifo(path)
                    else:
                        path.symlink_to(target if kind == "file" else Path(outside) / "missing")
                result = self.command(folder, "/memory set example should fail")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("OUTSIDE_MARKER", result.stdout)
                self.assertEqual(target.read_bytes(), before)

    def test_busy_store_is_not_overwritten_and_temporary_files_are_cleaned(self):
        with tempfile.TemporaryDirectory() as folder:
            path = seed_memory(folder, [{"key": "keep", "content": "existing"}])
            before = path.read_bytes()
            lock = path.parent / "memory.lock"
            lock.mkdir()
            result = self.command(folder, "/memory clear")
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual({item.name for item in path.parent.iterdir()}, {"memory.json", "memory.lock"})
            lock.rmdir()
            self.assertEqual(self.command(folder, "/memory clear").returncode, 0)

    def test_tui_clear_preserves_memory_and_approval_rejects_stale_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            path = seed_memory(folder, [{"key": "build", "content": "make build"}])
            with MockAPI([
                reply("openrouter", calls=[("memory", {"action": "set", "key": "build", "content": "stale update"})]),
                reply("openrouter", "Stale memory update rejected.")]) as api:
                terminal = Terminal(["--cwd", folder, "--no-skills"], environment(api.url), rows=40, columns=140)
                try:
                    terminal.wait_for("Your terminal.")
                    terminal.send("/memory set style Keep functions small.\r")
                    terminal.wait_for("MEMORY SAVED")
                    terminal.send("/clear\r")
                    terminal.send("Change the remembered build command.\r")
                    terminal.wait_for("Approve this action?")
                    self.assertIn("Keep functions small.", system_text(api.requests[0][1]))
                    self.assertEqual(self.command(folder, "/memory set other Concurrent change.").returncode, 0)
                    changed = path.read_bytes()
                    terminal.send("y")
                    terminal.wait_for("Stale memory update rejected.")
                    self.assertIn("changed since preview", results(api.requests[1][1], "openrouter")[-1][1])
                    self.assertIn("Concurrent change.", system_text(api.requests[1][1]))
                    self.assertEqual(path.read_bytes(), changed)
                finally:
                    terminal.close()

    def test_subagents_inherit_memory_but_cannot_mutate_it(self):
        for provider in ("openrouter", "claude"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                path = seed_memory(folder, [{"key": "architecture", "content": "SHARED_PROJECT_FACT"}])
                before = path.read_bytes()
                children = []

                def route(body):
                    if latest_user_text(body) == "MEMORY_CHILD":
                        children.append(body)
                        if not results(body, provider):
                            return reply(provider, calls=[("memory", {"action": "clear"})])
                        return reply(provider, "Read the shared fact.")
                    if not results(body, provider):
                        return reply(provider, calls=[("delegate_tasks", {"tasks": [
                            {"mode": "read", "paths": ["README.md"], "prompt": "MEMORY_CHILD"}]} )])
                    return reply(provider, "Delegation complete.")

                with MockAPI([route]) as api:
                    result = self.run_agent(api, provider, folder, extra=("--approve",))
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(len(children), 2)
                    self.assertIn("SHARED_PROJECT_FACT", system_text(children[0], provider))
                    self.assertNotIn("memory", advertised_tools(children[0], provider))
                    self.assertIn("restriction", results(children[1], provider)[-1][1])
                self.assertEqual(json.loads(path.read_text())["entries"], json.loads(before)["entries"])
                self.assertEqual(json.loads(path.read_text())["recent"], ["Delegation complete."])

    def test_automatic_recaps_rotate_and_finish_task_is_saved_without_approval(self):
        for provider in ("openrouter", "claude"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                path = seed_memory(folder, [{"key": "keep", "content": "Durable project fact."}])
                for index in range(5):
                    summary = f"Completed task {index}."
                    response = reply(provider, calls=[("finish_task", {"summary": summary})]) if index % 2 else reply(provider, summary)
                    with MockAPI([response]) as api:
                        result = self.run_agent(api, provider, folder)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(len(api.requests), 1, "Automatic memory must not make another model request")
                        self.assertIn("Task recap saved automatically", result.stdout)
                stored = json.loads(path.read_text())
                self.assertEqual(stored["entries"], [{"key": "keep", "content": "Durable project fact."}])
                self.assertEqual(stored["recent"], ["Completed task 4.", "Completed task 3.", "Completed task 2."])
                with MockAPI([reply(provider, "Completed task 4.")]) as api:
                    self.assertEqual(self.run_agent(api, provider, folder).returncode, 0)
                    self.assertIn("Completed task 2.", system_text(api.requests[0][1], provider))
                self.assertEqual(json.loads(path.read_text()), stored)

    def test_failed_tasks_and_credential_echo_are_not_saved(self):
        responses = [(500, {"error": {"message": "Unavailable"}}),
                     reply("openrouter", "Echo: test-key-never-render-me"),
                     reply("openrouter", "")]
        for response in responses:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as folder:
                path = seed_memory(folder, [{"key": "keep", "content": "Keep this fact."}])
                before = path.read_bytes()
                with MockAPI([response]) as api:
                    self.run_agent(api, directory=folder)
                self.assertEqual(path.read_bytes(), before)

    def test_auto_recap_utf8_truncation_and_full_store_preserve_facts(self):
        with tempfile.TemporaryDirectory() as folder:
            with MockAPI([reply("openrouter", "🐢" * 1000)]) as api:
                result = self.run_agent(api, directory=folder)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            recap = json.loads(memory_file(folder).read_text())["recent"][0]
            self.assertLessEqual(len(recap.encode()), 1024)
            self.assertTrue(recap.startswith("🐢"))
            self.assertIn("shortened", recap)
            entries = [{"key": f"key{i}", "content": "x" * 980} for i in range(8)]
            path = seed_memory(folder, entries)
            self.assertLessEqual(path.stat().st_size, 8192)
            before = path.read_bytes()
            with MockAPI([reply("openrouter", "A long recap. " * 100)]) as api:
                result = self.run_agent(api, directory=folder)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("Automatic recap skipped", result.stdout)
            self.assertEqual(path.read_bytes(), before)

    def test_explicit_forgetting_is_not_undone_by_automatic_recap(self):
        for action in ("delete", "clear"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as folder:
                path = seed_memory(folder, [{"key": "forget", "content": "DO_NOT_RETAIN"}])
                with MockAPI([reply("openrouter", calls=[("memory", {"action": action, "key": "forget"})]),
                              reply("openrouter", "Forgot DO_NOT_RETAIN.")]) as api:
                    result = self.run_agent(api, directory=folder, extra=("--approve",))
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("DO_NOT_RETAIN", path.read_text())
                self.assertEqual(json.loads(path.read_text()).get("recent", []), [])

    def test_cancelled_task_does_not_save_partial_recap(self):
        release = threading.Event()

        def chunks():
            yield sse(chunk({"role": "assistant", "content": "PARTIAL_RECAP_MUST_NOT_SAVE"}))
            release.wait(8)

        with tempfile.TemporaryDirectory() as folder:
            path = seed_memory(folder, [{"key": "keep", "content": "Existing fact."}])
            before = path.read_bytes()
            with MockAPI([StreamReply(chunks())]) as api:
                terminal = Terminal(["--cwd", folder], environment(api.url), rows=40, columns=140)
                try:
                    terminal.wait_for("Your terminal.")
                    terminal.send("Start a task\r")
                    terminal.wait_for("PARTIAL_RECAP_MUST_NOT_SAVE")
                    terminal.send(b"\x1b")
                    terminal.wait_for("Cancelled.")
                    self.assertEqual(path.read_bytes(), before)
                finally:
                    release.set()
                    terminal.close()


if __name__ == "__main__":
    unittest.main()
