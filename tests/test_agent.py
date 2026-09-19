"""Black-box tests of the compiled Zero executable. No paid API calls."""

import contextlib
import fcntl
import http.server
import json
import os
from pathlib import Path
import pty
import select
import stat
import struct
import subprocess
import tempfile
import termios
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "zero-coding"
KEYS = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


def reply(provider, text="Done.", calls=None):
    if provider == "claude":
        content = [{"type": "text", "text": text}] if text else []
        for index, (name, args) in enumerate(calls or []):
            content.append({"type": "tool_use", "id": f"call_{index}", "name": name, "input": args})
        return {"id": "msg_test", "type": "message", "role": "assistant", "content": content,
                "stop_reason": "tool_use" if calls else "end_turn", "usage": {"input_tokens": 12, "output_tokens": 8}}
    message = {"role": "assistant", "content": text or None}
    if calls:
        message["tool_calls"] = [{"id": f"call_{index}", "type": "function", "function": {
            "name": name, "arguments": json.dumps(args)},
            "extra_content": {"google": {"thought_signature": "signature-must-survive"}}}
            for index, (name, args) in enumerate(calls)]
        message["reasoning_details"] = [{"type": "reasoning.text", "text": "Provider metadata."}]
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}],
            "usage": {"total_tokens": 20}}


class MockAPI:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.errors = []
        mock = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    body = self.rfile.read(int(self.headers["Content-Length"]))
                    mock.requests.append((dict(self.headers), json.loads(body)))
                    index = len(mock.requests) - 1
                    payload = mock.responses[min(index, len(mock.responses) - 1)]
                    if callable(payload):
                        payload = payload(mock.requests[-1][1])
                    status = 200
                    if isinstance(payload, tuple):
                        status, payload = payload
                    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception as exc:
                    mock.errors.append(str(exc))

            def log_message(self, *_args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/messages"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def environment(endpoint=None, keys=True):
    env = os.environ.copy()
    for key in KEYS:
        env.pop(key, None)
        if keys:
            env[key] = "test-key-never-render-me"
    env.pop("ZERO_API_URL", None)
    if endpoint:
        env["ZERO_API_URL"] = endpoint
    env["TERM"] = "xterm-256color"
    return env


class Terminal:
    def __init__(self, args, env, rows=24, columns=80, command=None):
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        self.original = termios.tcgetattr(slave)
        self.slave = slave
        self.process = subprocess.Popen([*(command or [str(EXE)]), *args], stdin=slave, stdout=slave, stderr=slave, env=env, cwd=ROOT)
        self.output = bytearray()

    def send(self, data):
        os.write(self.master, data if isinstance(data, bytes) else data.encode())

    def wait_for(self, text, timeout=10, after=0):
        target = text.encode()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if target in self.output[after:]:
                return
            ready, _, _ = select.select([self.master], [], [], 0.1)
            if ready:
                try:
                    data = os.read(self.master, 65536)
                    if data:
                        self.output.extend(data)
                except OSError:
                    break
            if self.process.poll() is not None:
                break
        raise AssertionError(f"Terminal did not show {text!r}. Exit={self.process.poll()} Output tail={bytes(self.output[-3500:])!r}")

    def close(self):
        if self.process.poll() is None:
            self.send(b"\x04")
            deadline = time.monotonic() + 3
            while self.process.poll() is None and time.monotonic() < deadline:
                ready, _, _ = select.select([self.master], [], [], 0.05)
                if ready:
                    try:
                        self.output.extend(os.read(self.master, 65536))
                    except OSError:
                        break
            try:
                self.process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        restored = termios.tcgetattr(self.slave)
        os.close(self.master)
        os.close(self.slave)
        return restored


class AgentTests(unittest.TestCase):
    def run_agent(self, api, provider="openrouter", directory=None, extra=(), prompt="Please do the task.", max_turns=4):
        return subprocess.run([str(EXE), "--provider", provider, "--model", "any/custom-model-id",
                               "--cwd", str(directory or ROOT), "--max-turns", str(max_turns), "--prompt", prompt, *extra],
                              env=environment(api.url), text=True, capture_output=True, timeout=15)

    def test_native_core(self):
        result = subprocess.run([str(EXE), "--self-test"], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("tests passed", result.stdout)

    def test_make_run_in_standard_terminal(self):
        terminal = Terminal([], environment(keys=False), command=["make", "run"])
        try:
            terminal.wait_for("Your terminal.", timeout=20)
            self.assertIn(b"\x1b[48;5;234m", terminal.output)
            self.assertNotIn(b"u001b[", terminal.output)
            terminal.send("/provider\r")
            terminal.wait_for("Choose your provider")
            terminal.send(b"\x1b[B\r")
            terminal.wait_for("OPENAI_API_KEY")
        finally:
            restored = terminal.close()
        self.assertEqual(terminal.process.returncode, 0)
        self.assertEqual(restored, terminal.original)

    def test_four_provider_protocols(self):
        for provider in ("openrouter", "openai", "claude", "gemini"):
            with self.subTest(provider=provider), MockAPI([reply(provider, "Provider connected.")]) as api:
                result = self.run_agent(api, provider)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("Provider connected.", result.stdout)
                headers, body = api.requests[0]
                self.assertEqual(body["model"], "any/custom-model-id")
                self.assertEqual(len(body["tools"]), 6)
                self.assertNotIn("test-key-never-render-me", result.stdout + result.stderr)
                lowered = {k.lower(): v for k, v in headers.items()}
                if provider == "claude":
                    self.assertEqual(lowered["x-api-key"], "test-key-never-render-me")
                    self.assertEqual(lowered["anthropic-version"], "2023-06-01")
                    self.assertIn("input_schema", body["tools"][0])
                else:
                    self.assertEqual(lowered["authorization"], "Bearer test-key-never-render-me")
                    self.assertIn("function", body["tools"][0])

    def test_read_tool_round_trip_preserves_metadata(self):
        for provider in ("openrouter", "claude", "gemini"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                Path(folder, "hello.txt").write_text('hello "Zero"\nżółw\n')
                responses = [reply(provider, "Reading.", [("read_file", {"path": "hello.txt"})]), reply(provider, "Read complete.")]
                with MockAPI(responses) as api:
                    result = self.run_agent(api, provider, folder)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(len(api.requests), 2)
                    history = api.requests[1][1]["messages"]
                    self.assertIn('hello "Zero"\nżółw\n', json.dumps(history, ensure_ascii=False).replace('\\n', '\n').replace('\\"', '"'))
                    if provider != "claude":
                        self.assertEqual(history[-2]["tool_calls"][0]["extra_content"]["google"]["thought_signature"], "signature-must-survive")
                        self.assertIn("reasoning_details", history[-2])

    def test_large_read_does_not_crash_before_write(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "large.0").write_bytes(b"x" * 70000)
            with MockAPI([
                reply("openrouter", calls=[("list_files", {})]),
                reply("openrouter", calls=[("read_file", {"path": "large.0"})]),
                reply("openrouter", calls=[("write_file", {"path": "README.md", "content": "# Project\n"})]),
                reply("openrouter", "README complete.")]) as api:
                result = self.run_agent(api, directory=folder, extra=("--approve",))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(Path(folder, "README.md").read_text(), "# Project\n")
                self.assertIn("larger", json.dumps(api.requests[2][1]))

    def test_file_read_boundaries_and_failed_large_overwrite(self):
        for size in (16368, 16384, 16385, 3500000):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as folder:
                contents = b"a" * size
                Path(folder, "file.txt").write_bytes(contents)
                with MockAPI([
                    reply("openrouter", calls=[("read_file", {"path": "file.txt"})]),
                    reply("openrouter", "Done.")]) as api:
                    result = self.run_agent(api, directory=folder)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    tool_message = api.requests[1][1]["messages"][-1]["content"]
                    if size <= 16384:
                        self.assertEqual(tool_message, contents.decode())
                    else:
                        self.assertIn("larger", tool_message)
                if size > 16384:
                    with MockAPI([
                        reply("openrouter", calls=[("write_file", {"path": "file.txt", "content": "replacement"})]),
                        reply("openrouter", "Could not overwrite.")]) as api:
                        result = self.run_agent(api, directory=folder, extra=("--approve",))
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(Path(folder, "file.txt").read_bytes(), contents)

    def test_tui_read_large_graph_then_write_readme(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "zero.graph").write_bytes(b"x" * 3500000)
            with MockAPI([
                reply("openrouter", calls=[("list_files", {})]),
                reply("openrouter", calls=[("read_file", {"path": "zero.graph"})]),
                reply("openrouter", calls=[("write_file", {"path": "README.md", "content": "# Zero project\n"})]),
                reply("openrouter", "README complete.")]) as api:
                terminal = Terminal(["--cwd", folder, "--model", "openrouter/free"], environment(api.url), rows=40, columns=146)
                try:
                    terminal.wait_for("Your terminal.")
                    terminal.send("write README.md for this project\r")
                    terminal.wait_for("Approve this action?")
                    terminal.send("y")
                    terminal.wait_for("README complete.")
                    self.assertEqual(Path(folder, "README.md").read_text(), "# Zero project\n")
                    self.assertNotIn(b"trap:", terminal.output)
                finally:
                    restored = terminal.close()
                self.assertEqual(terminal.process.returncode, 0)
                self.assertEqual(restored, terminal.original)

    def test_write_and_edit(self):
        with tempfile.TemporaryDirectory() as folder:
            responses = [reply("openrouter", calls=[("write_file", {"path": "hello.txt", "content": "hello\n"})]),
                         reply("openrouter", calls=[("edit_file", {"path": "hello.txt", "old_text": "hello", "new_text": "world"})]),
                         reply("openrouter", "Saved and edited.")]
            with MockAPI(responses) as api:
                result = self.run_agent(api, directory=folder, extra=("--approve",))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(Path(folder, "hello.txt").read_text(), "world\n")
                self.assertEqual(list(Path(folder).glob("*.zero-edit.*")), [])

    def test_preserves_executable_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder, "run.sh")
            script.write_text("#!/bin/sh\necho before\n")
            script.chmod(0o755)
            with MockAPI([reply("openrouter", calls=[("edit_file", {
                "path": "run.sh", "old_text": "before", "new_text": "after"})]), reply("openrouter", "Updated.")]) as api:
                result = self.run_agent(api, directory=folder, extra=("--approve",))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("echo after", script.read_text())
                self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o755)

    def test_headless_denies_mutation_by_default(self):
        with tempfile.TemporaryDirectory() as folder, MockAPI([
            reply("openrouter", calls=[("write_file", {"path": "blocked.txt", "content": "no"})]), reply("openrouter", "Denied.")]) as api:
            result = self.run_agent(api, directory=folder)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(Path(folder, "blocked.txt").exists())
            self.assertIn("denied", result.stdout)

    def test_path_traversal_and_symlink(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            Path(outside, "private.txt").write_text("not-for-model")
            Path(folder, "link.txt").symlink_to(Path(outside, "private.txt"))
            with MockAPI([reply("openai", calls=[("read_file", {"path": "../private.txt"}), ("read_file", {"path": "link.txt"})]), reply("openai", "Safe.")]) as api:
                result = self.run_agent(api, "openai", folder)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("not-for-model", json.dumps(api.requests))
                self.assertIn("symlink", result.stdout)

    def test_gitignore_listing_and_read_policy(self):
        for layout in ("plain", "repository", "subdirectory"):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as folder:
                repository = Path(folder, "project with spaces")
                repository.mkdir()
                workspace = repository
                if layout == "subdirectory":
                    workspace = repository / "workspace"
                    workspace.mkdir()
                    (repository / ".gitignore").write_text("from-parent.dat\n")
                (workspace / ".gitignore").write_text("ignored/\n*.log\n!keep.log\n/only-root.txt\n")
                (workspace / "src").mkdir()
                (workspace / "src/.gitignore").write_text("*.cache\n*.txt\n!allowed.txt\n!café \"notes\".txt\n!only-root.txt\n")
                included = ["main.0", "keep.log", "src/allowed.txt", 'src/café "notes".txt', "src/only-root.txt"]
                ignored = ["ignored/private.txt", "ignored/deep/file.txt", "hidden.log", "only-root.txt",
                           "src/scratch.cache", "src/private.txt"]
                if layout == "subdirectory":
                    ignored.append("from-parent.dat")
                for path in included + ignored:
                    file = workspace / path
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_text(("IGNORED_CONTENT_MUST_NOT_REACH_MODEL:" if path in ignored else "VISIBLE:") + path)
                index_before = None
                if layout != "plain":
                    subprocess.run(["git", "init", "-q", str(repository)], check=True, capture_output=True)
                    subprocess.run(["git", "-C", str(workspace), "add", "-f", "hidden.log", "main.0"], check=True, capture_output=True)
                    index_before = (repository / ".git/index").read_bytes()
                calls = [("read_file", {"path": path}) for path in ignored + included]
                with MockAPI([reply("openrouter", calls=[("list_files", {})]),
                              reply("openrouter", calls=calls), reply("openrouter", "Checked.")]) as api:
                    result = self.run_agent(api, directory=workspace)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(len(api.requests), 3)
                    listing = api.requests[1][1]["messages"][-1]["content"].splitlines()
                    for path in included:
                        self.assertIn("./" + path, listing)
                    for path in ignored:
                        self.assertNotIn("./" + path, listing)
                    history = json.dumps(api.requests[-1][1]["messages"], ensure_ascii=False)
                    self.assertNotIn("IGNORED_CONTENT_MUST_NOT_REACH_MODEL", history + result.stdout)
                    results = [msg["content"] for msg in api.requests[-1][1]["messages"] if msg["role"] == "tool"][1:]
                    self.assertEqual(len(results), len(calls))
                    for content in results[:len(ignored)]:
                        self.assertIn("ignored", content)
                    for path, content in zip(included, results[len(ignored):]):
                        self.assertIn("VISIBLE:" + path, content)
                if layout == "plain":
                    self.assertFalse((repository / ".git").exists())
                else:
                    self.assertEqual((repository / ".git/index").read_bytes(), index_before)

    def test_gitignored_files_cannot_be_read_through_edit_previews(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".gitignore").write_text("private/\n")
            (root / "private").mkdir()
            file = root / "private/notes.txt"
            file.write_text("IGNORED_PREVIEW_MUST_NOT_REACH_MODEL")
            with MockAPI([reply("openrouter", calls=[
                ("write_file", {"path": "private/notes.txt", "content": "replacement"}),
                ("edit_file", {"path": "private/notes.txt", "old_text": "PREVIEW", "new_text": "EDIT"}),
                ("write_file", {"path": "private/new.txt", "content": "new"})]), reply("openrouter", "Checked.")]) as api:
                result = self.run_agent(api, directory=folder, extra=("--approve",))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(file.read_text(), "IGNORED_PREVIEW_MUST_NOT_REACH_MODEL")
                self.assertFalse((root / "private/new.txt").exists())
                self.assertNotIn("IGNORED_PREVIEW_MUST_NOT_REACH_MODEL", json.dumps(api.requests) + result.stdout)
                self.assertIn("ignored", result.stdout)

    def test_shell_and_stderr(self):
        with tempfile.TemporaryDirectory() as folder, MockAPI([
            reply("claude", calls=[("run_command", {"command": "printf 'out\\n'\nprintf 'err\\n' >&2\nexit 7"})]), reply("claude", "Command failed as expected.")]) as api:
            result = self.run_agent(api, "claude", folder, extra=("--approve",))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("[exit 7]", result.stdout)
            self.assertIn("[stderr]", result.stdout)

    def test_api_errors(self):
        for response in ((401, {"error": {"message": "Invalid API key"}}), b"not json"):
            with self.subTest(response=response), MockAPI([response]) as api:
                result = self.run_agent(api)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Agent stopped", result.stdout)

    def test_missing_key_and_invalid_provider(self):
        for args in (("--prompt", "hello"), ("--provider", "invalid", "--prompt", "hello")):
            result = subprocess.run([str(EXE), *args], env=environment(keys=False), text=True, capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_tui_model_switch_and_chat_mode(self):
        with MockAPI([reply("openrouter", "Switched successfully.")]) as api:
            terminal = Terminal([], environment(api.url))
            try:
                terminal.wait_for("Your terminal.")
                terminal.send("/model vendor/brand-new-model\r")
                terminal.wait_for("brand-new-model")
                terminal.send("/tools off\r")
                terminal.wait_for("Chat mode")
                terminal.send("hello\r")
                terminal.wait_for("Switched successfully.")
                self.assertEqual(api.requests[0][1]["model"], "vendor/brand-new-model")
                self.assertNotIn("tools", api.requests[0][1])
            finally:
                restored = terminal.close()
            self.assertEqual(restored, terminal.original)

    def test_tui_review_and_stale_edit(self):
        with tempfile.TemporaryDirectory() as folder, MockAPI([
            reply("openrouter", calls=[("edit_file", {"path": "file.txt", "old_text": "before", "new_text": "after"})]), reply("openrouter", "Finished.")]) as api:
            file = Path(folder, "file.txt")
            file.write_text("before")
            terminal = Terminal(["--cwd", folder], environment(api.url))
            try:
                terminal.wait_for("Your terminal.")
                terminal.send("change the file\r")
                terminal.wait_for("Approve this action?")
                file.write_text("user changed this")
                terminal.send("y")
                terminal.wait_for("Finished.")
                self.assertEqual(file.read_text(), "user changed this")
                self.assertIn("changed since the preview", json.dumps(api.requests))
            finally:
                terminal.close()

    def test_tui_list_then_create_file(self):
        for provider in ("openrouter", "claude"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder, MockAPI([
                reply(provider, calls=[("list_files", {})]),
                reply(provider, calls=[("write_file", {"path": "docs/hello.txt", "content": "written from the TUI\n"})]),
                reply(provider, "Created the file.")]) as api:
                terminal = Terminal(["--cwd", folder, "--provider", provider], environment(api.url))
                try:
                    terminal.wait_for("Your terminal.")
                    terminal.send("list the files, then create docs/hello.txt\r")
                    terminal.wait_for("Approve this action?")
                    self.assertFalse(Path(folder, "docs").exists())
                    terminal.send("y")
                    terminal.wait_for("Created the file.")
                    self.assertEqual(Path(folder, "docs/hello.txt").read_text(), "written from the TUI\n")
                    self.assertIn("Saved docs/hello.txt", json.dumps(api.requests[-1][1]))
                finally:
                    terminal.close()

    def test_completion_stops_tools_and_preserves_next_turn(self):
        for provider in ("openrouter", "claude", "gemini", "openai"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder, MockAPI([
                reply(provider, calls=[("write_file", {"path": "README.md", "content": "# Finished\n"})]),
                reply(provider, calls=[("finish_task", {"summary": "The README is complete."}),
                                       ("write_file", {"path": "unrequested.txt", "content": "must not run"})]),
                reply(provider, "A new task can start.")]) as api:
                terminal = Terminal(["--cwd", folder, "--provider", provider, "--approve"], environment(api.url))
                try:
                    terminal.wait_for("Your terminal.")
                    terminal.send("Write the README.\r")
                    terminal.wait_for("TASK COMPLETE")
                    terminal.wait_for("The README is complete.")
                    terminal.wait_for("ready", after=len(terminal.output))
                    self.assertEqual(len(api.requests), 2)
                    self.assertEqual(Path(folder, "README.md").read_text(), "# Finished\n")
                    self.assertFalse(Path(folder, "unrequested.txt").exists())
                    terminal.send("Now answer this new question.\r")
                    terminal.wait_for("A new task can start.")
                    self.assertEqual(len(api.requests), 3)
                    history = api.requests[2][1]["messages"]
                    if provider == "claude":
                        results = [block for msg in history if isinstance(msg.get("content"), list)
                                   for block in msg["content"] if block.get("type") == "tool_result"]
                        self.assertEqual([block["tool_use_id"] for block in results], ["call_0", "call_0", "call_1"])
                    else:
                        results = [msg for msg in history if msg["role"] == "tool"]
                        self.assertEqual([msg["tool_call_id"] for msg in results], ["call_0", "call_0", "call_1"])
                    self.assertIn("Skipped", json.dumps(results))
                finally:
                    terminal.close()

    def test_completion_breaks_identical_tool_result_loop(self):
        for provider in ("openrouter", "claude"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder, MockAPI([
                reply(provider, calls=[("list_files", {})])]) as api:
                result = self.run_agent(api, provider, folder, max_turns=20)
                self.assertEqual(len(api.requests), 3)
                self.assertIn("Repeated tool calls stopped", result.stdout)
                self.assertNotIn("request limit", result.stdout)

    def test_completion_allows_changed_results_and_multiple_files(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder, "progress.txt")

            def changed_read(_body):
                file.write_text(str(len(api.requests)))
                return reply("openrouter", calls=[("read_file", {"path": "progress.txt"})])

            with MockAPI([changed_read, changed_read, changed_read,
                          reply("openrouter", calls=[("write_file", {"path": "one.txt", "content": "one"}),
                                                     ("write_file", {"path": "two.txt", "content": "two"})]),
                          reply("openrouter", calls=[("finish_task", {"summary": "Both files are complete."})])]) as api:
                result = self.run_agent(api, directory=folder, extra=("--approve",), max_turns=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(api.requests), 5)
                self.assertEqual(Path(folder, "one.txt").read_text(), "one")
                self.assertEqual(Path(folder, "two.txt").read_text(), "two")
                self.assertIn("TASK COMPLETE", result.stdout)

    def test_completion_plain_final_response_stays_idle(self):
        with MockAPI([reply("openrouter", "The task is done.")]) as api:
            terminal = Terminal([], environment(api.url))
            try:
                terminal.wait_for("Your terminal.")
                terminal.send("Say the task is done.\r")
                terminal.wait_for("The task is done.")
                terminal.wait_for("ready", after=len(terminal.output))
                self.assertEqual(len(api.requests), 1)
            finally:
                terminal.close()


if __name__ == "__main__":
    unittest.main()
