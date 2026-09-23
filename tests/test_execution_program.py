"""Request-count contracts; native integration runs when dist/zero-code exists."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "scripts" / "execution_program.py"


def load_driver():
    spec = importlib.util.spec_from_file_location("execution_program", DRIVER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def digest(value):
    return "missing" if value is None else hashlib.sha256(value.encode()).hexdigest()


def program(snapshots, replacements):
    return {"summary": "Update the requested files", "changes": [
        {"id": f"change-{i}", "path": path, "before_sha256": digest(snapshots[path]),
         "content": content, "depends_on": []}
        for i, (path, content) in enumerate(replacements.items())]}


class FileTools:
    """Real temp-file operations, injected in place of the native boundary."""
    def __init__(self, root):
        self.root = root
        self.writes = []
        self.calls = 0
        self.fail_on = None

    def read(self, path):
        self.calls += 1
        target = self.root / path
        return target.read_text() if target.exists() else None

    def write(self, path, content, before):
        self.calls += 1
        if path == self.fail_on:
            raise RuntimeError("injected tool failure")
        actual = self.read(path)
        if actual != before:
            raise RuntimeError("stale file")
        (self.root / path).parent.mkdir(parents=True, exist_ok=True)
        (self.root / path).write_text(content)
        self.writes.append(path)


class TestExecutionProgram(unittest.TestCase):
    def setUp(self):
        self.assertTrue(DRIVER.is_file(), "execution-program controller is not implemented")
        self.ep = load_driver()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tools = FileTools(self.root)

    def seed(self, files):
        for path, content in files.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    def runner(self, planner, **kwargs):
        return self.ep.Runner(self.tools, planner, ["verification"],
                              command=lambda command: {"command": command, "exit_code": 0,
                                                       "output": "passed", "truncated": False},
                              approve=lambda proposal: True, **kwargs)

    def test_ten_independent_edits_use_one_request(self):
        before = {f"file-{i}.txt": "old\n" for i in range(10)}
        after = {path: "new\n" for path in before}
        self.seed(before)
        prompts = []
        def planner(context):
            prompts.append(context)
            return program(before, after)
        result = self.runner(planner).run("Update ten files", list(before))
        self.assertEqual("checks_passed", result["status"])
        self.assertEqual(1, result["model_requests"])
        self.assertEqual(1, len(prompts))
        self.assertEqual(10, len(result["files_changed"]))
        self.assertTrue(all((self.root / path).read_text() == text for path, text in after.items()))

    def test_repair_is_bounded_and_uses_current_files_not_history(self):
        self.seed({"app.py": "initial", "context.txt": "unchanged context"})
        prompts = []
        def planner(context):
            prompts.append(context)
            current = context["files"]["app.py"]["content"]
            return program({"app.py": current}, {"app.py": "bad" if len(prompts) == 1 else "good"})
        runner = self.runner(planner)
        runner.command = lambda cmd: {"command": cmd, "exit_code": int((self.root / "app.py").read_text() != "good"), "output": "assert good", "truncated": False}
        result = runner.run("Fix app", ["app.py", "context.txt"])
        self.assertEqual("checks_passed", result["status"])
        self.assertEqual(2, result["model_requests"])
        self.assertEqual("bad", prompts[1]["files"]["app.py"]["content"])
        self.assertNotIn("context.txt", prompts[1]["files"])
        self.assertNotIn("history", prompts[1])

    def test_budget_exhaustion_is_not_completion(self):
        self.seed({"app.py": "old"})
        def planner(context):
            before = context["files"]["app.py"]["content"]
            return program({"app.py": before}, {"app.py": before + "x"})
        runner = self.runner(planner, budget=1)
        runner.command = lambda cmd: {"command": cmd, "exit_code": 1, "output": "failed", "truncated": False}
        result = runner.run("Fix app", ["app.py"])
        self.assertEqual("budget_exhausted", result["status"])
        self.assertEqual(1, result["model_requests"])
        self.assertEqual(["app.py"], result["files_changed"])

    def test_denied_program_does_not_mutate_or_retry(self):
        self.seed({"a.txt": "old"})
        runner = self.runner(lambda _: program({"a.txt": "old"}, {"a.txt": "new"}))
        runner.approve = lambda proposal: False
        result = runner.run("Edit", ["a.txt"])
        self.assertEqual("denied", result["status"])
        self.assertEqual("old", (self.root / "a.txt").read_text())
        self.assertEqual([], self.tools.writes)
        self.assertEqual(1, result["model_requests"])
        self.assertEqual([], result["checks"])

    def test_network_failure_is_counted_without_retry(self):
        self.seed({"a.txt": "old"})
        def fail(_):
            raise OSError("provider unavailable")
        result = self.runner(fail).run("Edit", ["a.txt"])
        self.assertEqual("failed", result["status"])
        self.assertEqual(1, result["model_requests"])
        self.assertEqual([], self.tools.writes)

    def test_stale_file_at_approval_is_terminal(self):
        self.seed({"a.txt": "old"})
        runner = self.runner(lambda _: program({"a.txt": "old"}, {"a.txt": "new"}))
        def approve(_):
            (self.root / "a.txt").write_text("user edit")
            return True
        runner.approve = approve
        result = runner.run("Edit", ["a.txt"])
        self.assertEqual("failed", result["status"])
        self.assertEqual("user edit", (self.root / "a.txt").read_text())
        self.assertEqual(1, result["model_requests"])

    def test_mid_batch_failure_keeps_partial_result_and_stops(self):
        self.seed({"a": "old", "b": "old", "c": "old"})
        self.tools.fail_on = "b"
        result = self.runner(lambda _: program({"a": "old", "b": "old", "c": "old"},
                                               {"a": "new", "b": "new", "c": "new"})).run("Edit", ["a", "b", "c"])
        self.assertEqual("failed", result["status"])
        self.assertEqual(["a"], result["files_changed"])
        self.assertEqual("old", (self.root / "c").read_text())
        self.assertEqual([], result["checks"])
        self.assertEqual(1, result["model_requests"])

    def test_creation_requires_explicit_scope(self):
        result = self.runner(lambda _: program({"new.txt": None}, {"new.txt": "created"})).run("Create", [], ["new.txt"])
        self.assertEqual("checks_passed", result["status"])
        self.assertEqual("created", (self.root / "new.txt").read_text())

    def test_dependencies_are_topologically_ordered(self):
        data = program({"a": "old", "b": "old"}, {"a": "new", "b": "new"})
        data["changes"][0]["depends_on"] = ["change-1"]
        result = self.ep.validate_program(data, {"a": "old", "b": "old"})
        self.assertEqual(["b", "a"], [change["path"] for change in result])

    def test_rejects_bad_programs_before_execution(self):
        baseline = program({"a": "old", "b": "old"}, {"a": "new", "b": "new"})
        cases = []
        unknown = copy.deepcopy(baseline); unknown["command"] = "rm -rf ."; cases.append(unknown)
        outside = copy.deepcopy(baseline); outside["changes"][0]["path"] = "other"; cases.append(outside)
        stale = copy.deepcopy(baseline); stale["changes"][0]["before_sha256"] = "wrong"; cases.append(stale)
        duplicate = copy.deepcopy(baseline); duplicate["changes"][1]["id"] = "change-0"; cases.append(duplicate)
        cycle = copy.deepcopy(baseline); cycle["changes"][0]["depends_on"] = ["change-1"]; cycle["changes"][1]["depends_on"] = ["change-0"]; cases.append(cycle)
        missing = copy.deepcopy(baseline); missing["changes"][0]["depends_on"] = ["absent"]; cases.append(missing)
        noop = copy.deepcopy(baseline); noop["changes"][0]["content"] = "old"; cases.append(noop)
        cases += [{"summary": "", "changes": []}, {"summary": "ok", "changes": "wrong"}]
        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(self.ep.Rejected):
                    self.ep.validate_program(data, {"a": "old", "b": "old"})

    def test_strict_json_rejects_duplicate_keys_and_nan(self):
        for text in ['{"summary":"a","summary":"b"}', '{"value":NaN}', '{"value":Infinity}']:
            with self.assertRaises(self.ep.Rejected):
                self.ep.parse_json(text)

    def test_paths_reject_secrets_traversal_graph_and_symlinks(self):
        tools = self.ep.NativeTools(ROOT / "dist" / "zero-code", self.root)
        for path in ["../secret", "/absolute", ".env", "x/.env.local", "zero.graph", "ZERO.GRAPH", "x\\y", "a/../b", "a\nb", "a/.git/config", ".zero-agent/state", "key.pem"]:
            with self.subTest(path=path):
                with self.assertRaises(self.ep.Rejected):
                    tools.path(path)
        (self.root / "link").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(self.ep.Rejected):
            tools.path("link/file")

    def test_command_output_is_bounded(self):
        cmd = shlex.join([sys.executable, "-c", "print('x' * 100000)"])
        result = self.ep.run_command(cmd, self.root, timeout=5)
        self.assertEqual(0, result["exit_code"])
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["output"].encode()), 16384)

    def test_command_timeout_is_not_success(self):
        cmd = shlex.join([sys.executable, "-c", "import time; time.sleep(10)"])
        result = self.ep.run_command(cmd, self.root, timeout=0.1)
        self.assertNotEqual(0, result["exit_code"])
        self.assertTrue(result["timed_out"])

    def test_endpoint_policy(self):
        for url in ["http://example.com/v1", "https://user:pass@example.com/v1", "https://example.com/x#fragment", "https://example.com/\n"]:
            with self.subTest(url=url):
                with self.assertRaises(self.ep.Rejected):
                    self.ep.validate_endpoint(url)
        self.ep.validate_endpoint("https://example.com/v1")
        self.ep.validate_endpoint("http://127.0.0.1:4321/v1")


    def test_timeout_kills_descendants_after_parent_exits(self):
        code = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)']); print(p.pid,flush=True)"
        result = self.ep.run_command(shlex.join([sys.executable, "-c", code]), self.root, timeout=1.0)
        pid = int(result["output"].strip())
        try:
            # SIGKILL delivery/reaping is asynchronous, especially on loaded CI.
            deadline = time.monotonic() + 2
            while True:
                state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
                if not state or state.startswith("Z") or time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
            self.assertTrue(not state or state.startswith("Z"), f"descendant still running: {state}")
        finally:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass

    def test_invalid_budget_and_empty_verification(self):
        for budget in [0, 5, True]:
            with self.assertRaises(self.ep.Rejected):
                self.runner(lambda _: None, budget=budget)
        with self.assertRaises(self.ep.Rejected):
            self.ep.Runner(self.tools, lambda _: None, [])

    def test_formatter_failure_does_not_run_verification(self):
        self.seed({"a": "old"})
        invoked = []
        runner = self.runner(lambda _: program({"a": "old"}, {"a": "new"}), budget=1, formatters=["formatter"])
        def command(cmd):
            invoked.append(cmd)
            return {"command": cmd, "exit_code": 1, "output": "format failed", "truncated": False}
        runner.command = command
        result = runner.run("Edit", ["a"])
        self.assertEqual("budget_exhausted", result["status"])
        self.assertEqual(["formatter"], invoked)

    def test_all_initial_snapshots_preflight_before_mutation(self):
        self.seed({"a": "old", "b": "old"})
        runner = self.runner(lambda _: program({"a": "old", "b": "old"}, {"a": "new", "b": "new"}))
        def approve(_):
            (self.root / "b").write_text("user edit")
            return True
        runner.approve = approve
        result = runner.run("Edit", ["a", "b"])
        self.assertEqual("failed", result["status"])
        self.assertEqual([], self.tools.writes)
        self.assertEqual("old", (self.root / "a").read_text())


    def test_checkout_launcher_dispatches_opt_in_and_preserves_default(self):
        checkout = self.root / "checkout"
        (checkout / "dist").mkdir(parents=True)
        (checkout / "scripts").mkdir()
        launcher = checkout / "zero-code"
        launcher.write_text((ROOT / "zero-code").read_text())
        launcher.chmod(0o755)
        binary = checkout / "dist" / "zero-code"
        binary.write_text("#!/bin/sh\nprintf 'native:%s\\n' \"$*\"\n")
        binary.chmod(0o755)
        (checkout / "scripts" / "execution_program.py").write_text("import json,sys; print(json.dumps(sys.argv[1:]))\n")
        normal = subprocess.run([str(launcher), "--version"], capture_output=True, text=True, check=True)
        self.assertEqual("native:--version\n", normal.stdout)
        result = subprocess.run([str(launcher), "--execution-program", "--prompt", "a task"], capture_output=True, text=True, check=True)
        args = json.loads(result.stdout)
        self.assertEqual(["--binary", str(binary), "--prompt", "a task"], args)

    def test_worker_protocol_and_approval_use_no_provider_key(self):
        stub = self.root / "worker"
        stub.write_text("#!" + sys.executable + "\n" + r'''import json,os,sys
from pathlib import Path
assert "OPENROUTER_API_KEY" not in os.environ
size = int(sys.argv[2])
config = json.loads(sys.stdin.buffer.read(size))
assert config["kind"] == "tool" and not config["approve"] and config["key"] == ""
call = config["call"]; ident = call["id"]
fn = call["function"]; args = json.loads(fn["arguments"])
path = Path(args["path"])
assert config["paths"] == [args["path"]]
if fn["name"] == "read_file":
    data = path.read_bytes(); offset = args["offset"]; limit = args["limit"]
    chunk = data[offset:offset+limit]
    text = json.dumps({"offset":offset,"next_offset":offset+len(chunk),"total_bytes":len(data),
                       "truncated":offset+len(chunk)<len(data),"content":chunk.decode()})
else:
    original = path.read_bytes() if path.exists() else None
    print(json.dumps({"type":"approval","id":ident}),flush=True)
    answer = json.loads(sys.stdin.buffer.readline())
    assert answer == {"id":ident,"action":"approve"}
    assert (path.read_bytes() if path.exists() else None) == original
    path.write_text(args["content"])
    text = "File saved"
print(json.dumps({"type":"result","id":ident,"tokens":0,"failed":False,"text":text}),flush=True)
''')
        stub.chmod(0o755)
        (self.root / "a.txt").write_text("old\n")
        tools = self.ep.NativeTools(stub, self.root, timeout=5)
        self.assertEqual("old\n", tools.read("a.txt"))
        tools.write("a.txt", "new\n", "old\n")
        self.assertEqual("new\n", (self.root / "a.txt").read_text())
        tools.write("new.txt", "created\n", None)
        self.assertEqual("created\n", (self.root / "new.txt").read_text())


    def test_uncertain_write_is_reported_not_silently_omitted(self):
        self.seed({"a": "old"})
        def write_then_fail(path, content, before):
            (self.root / path).write_text(content)
            raise RuntimeError("lost the tool result after writing")
        self.tools.write = write_then_fail
        result = self.runner(lambda _: program({"a": "old"}, {"a": "new"})).run("Edit", ["a"])
        self.assertEqual("failed", result["status"])
        self.assertEqual(["a"], result["unconfirmed_changes"])
        self.assertEqual("new", (self.root / "a").read_text())
        self.assertEqual(1, result["model_requests"])

    def test_blocked_worker_stdin_obeys_timeout(self):
        pid_file = self.root / "child.pid"
        child_command = "echo $$ > " + shlex.quote(str(pid_file)) + "; sleep 10"
        probe = ("import json,runpy; ep=runpy.run_path(" + repr(str(DRIVER)) + "); "
                 "print(json.dumps(ep['process'](['/bin/sh','-c'," + repr(child_command) + "], "
                 + repr(str(self.root)) + ", 0.2, initial=b'x'*200000)))")
        try:
            try:
                result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=3)
            except subprocess.TimeoutExpired:
                self.fail("Writing initial worker input blocked beyond its timeout")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(json.loads(result.stdout)["timed_out"])
        finally:
            if pid_file.exists():
                try:
                    os.killpg(int(pid_file.read_text()), 9)
                except ProcessLookupError:
                    pass


class TestExecutionProgramHTTP(unittest.TestCase):
    def setUp(self):
        import http.server
        import threading
        self.ep = load_driver()
        self.bodies = []
        self.code = 200
        self.answer = {}
        self.location = None
        case = self
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                case.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(case.code)
                if case.location:
                    self.send_header("Location", case.location)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(case.answer).encode())
            def log_message(self, *_):
                pass
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_openai_compatible_providers_send_one_program_request(self):
        data = program({"a": "old"}, {"a": "new"})
        self.answer = {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{"id": "one", "function": {
            "name": "submit_execution_program", "arguments": json.dumps(data)}}]}}], "usage": {"total_tokens": 7}}
        for provider in ["openrouter", "openai", "gemini"]:
            planner = self.ep.Planner(provider, "test-model", self.endpoint, "test-key", timeout=2)
            self.assertEqual(data, planner({"files": {}}))
            self.assertEqual(1, planner.requests)
            self.assertEqual([{"total_tokens": 7}], planner.usage)
            body = self.bodies[-1]
            self.assertFalse(body["stream"])
            self.assertEqual(1, len(body["tools"]))
        self.assertEqual(3, len(self.bodies))

    def test_claude_uses_native_tool_envelope(self):
        data = program({"a": "old"}, {"a": "new"})
        self.answer = {"stop_reason": "tool_use", "content": [{"type": "tool_use", "name": "submit_execution_program", "input": data}]}
        planner = self.ep.Planner("claude", "test-model", self.endpoint, "test-key", timeout=2)
        self.assertEqual(data, planner({"files": {}}))
        self.assertEqual(1, len(self.bodies))
        self.assertIn("input_schema", self.bodies[0]["tools"][0])
        self.assertEqual(1, planner.requests)

    def test_429_is_not_retried(self):
        self.code = 429
        planner = self.ep.Planner("openrouter", "test-model", self.endpoint, "test-key", timeout=2)
        with self.assertRaises(self.ep.Rejected):
            planner({})
        self.assertEqual(1, len(self.bodies))
        self.assertEqual(1, planner.requests)

    def test_redirect_does_not_forward_credentials_or_send_second_request(self):
        self.code = 307
        self.location = self.endpoint + "/redirected"
        planner = self.ep.Planner("openrouter", "test-model", self.endpoint, "test-key", timeout=2)
        with self.assertRaises(self.ep.Rejected):
            planner({})
        self.assertEqual(1, len(self.bodies))

    def test_truncated_tool_call_is_not_executed(self):
        self.answer = {"choices": [{"finish_reason": "length", "message": {"tool_calls": []}}]}
        planner = self.ep.Planner("openrouter", "test-model", self.endpoint, "test-key", timeout=2)
        with self.assertRaises(self.ep.Rejected):
            planner({})
        self.assertEqual(1, len(self.bodies))


@unittest.skipUnless((ROOT / "dist" / "zero-code").is_file(), "native binary not built")
class TestExecutionProgramNative(unittest.TestCase):
    def setUp(self):
        self.ep = load_driver()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.tools = self.ep.NativeTools(ROOT / "dist" / "zero-code", self.root)

    def test_native_read_and_approved_write_preserve_worker_guards(self):
        (self.root / "a.txt").write_text("old\n")
        self.assertEqual("old\n", self.tools.read("a.txt"))
        self.tools.write("a.txt", "new\n", "old\n")
        self.assertEqual("new\n", (self.root / "a.txt").read_text())

    def test_native_ignored_file_is_denied(self):
        (self.root / ".gitignore").write_text("ignored.txt\n")
        (self.root / "ignored.txt").write_text("private\n")
        with self.assertRaises(self.ep.Rejected):
            self.tools.read("ignored.txt")

    def test_native_stale_file_is_not_overwritten(self):
        (self.root / "a.txt").write_text("user edit\n")
        with self.assertRaises(self.ep.Rejected):
            self.tools.write("a.txt", "model edit\n", "old snapshot\n")
        self.assertEqual("user edit\n", (self.root / "a.txt").read_text())

    def test_native_create(self):
        self.tools.write("new.txt", "created\n", None)
        self.assertEqual("created\n", (self.root / "new.txt").read_text())


if __name__ == "__main__":
    unittest.main()
