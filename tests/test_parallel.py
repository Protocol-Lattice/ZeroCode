"""Parallel file tools and bounded model delegation through the native binary."""

import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from tests import test_agent as agent
from tests.test_agent import MockAPI, StreamReply, Terminal, environment, reply
from tests.test_scaling import assert_tool_pairs
from tests.test_streaming import chunk, sse


def latest_user_text(body):
    """Tool-result messages are user messages too in the Claude protocol."""
    for message in reversed(body["messages"]):
        if message["role"] == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def results(body, provider):
    if provider == "claude":
        return [(block["tool_use_id"], block["content"])
                for message in body["messages"]
                if isinstance(message.get("content"), list)
                for block in message["content"] if block.get("type") == "tool_result"]
    return [(message["tool_call_id"], message["content"])
            for message in body["messages"] if message["role"] == "tool"]


def advertised_tools(body, provider):
    return {tool["name"] if provider == "claude" else tool["function"]["name"]
            for tool in body.get("tools", [])}


def start_agent(api, folder, provider="openrouter", extra=(), prompt="ROOT_PARALLEL_TASK"):
    return subprocess.Popen(
        [str(agent.EXE), "--provider", provider, "--model", "any/custom-model-id",
         "--cwd", str(folder), "--max-turns", "6", "--prompt", prompt, *extra],
        env=environment(api.url), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def stop_process(process):
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=4)


class ParallelTests(unittest.TestCase):
    run_agent = agent.AgentTests.run_agent

    def test_delegated_read_write_edit_overlap_and_round_trip(self):
        for provider in ("openrouter", "claude"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                Path(folder, "read.txt").write_text("Reader found a żółw.")
                Path(folder, "edit.txt").write_text("before editing")
                markers = ("CHILD_READER", "CHILD_WRITER", "CHILD_EDITOR")
                tasks = [
                    {"mode": "read", "paths": ["read.txt"], "prompt": markers[0]},
                    {"mode": "write", "paths": ["written.txt"], "prompt": markers[1]},
                    {"mode": "edit", "paths": ["edit.txt"], "prompt": markers[2]},
                ]
                calls = {
                    markers[0]: ("read_file", {"path": "read.txt"}),
                    markers[1]: ("write_file", {"path": "written.txt", "content": "Written by the child.\n"}),
                    markers[2]: ("edit_file", {"path": "edit.txt", "old_text": "before", "new_text": "after"}),
                }
                started = set()
                all_started = threading.Event()
                release = threading.Event()
                lock = threading.Lock()
                child_requests = {marker: [] for marker in markers}
                root_requests = []

                def route(body):
                    marker = next((item for item in markers if item in latest_user_text(body)), None)
                    if marker is None:
                        root_requests.append(body)
                        if not results(body, provider):
                            return reply(provider, calls=[("delegate_tasks", {"tasks": tasks})])
                        return reply(provider, "Delegated work complete.")
                    with lock:
                        child_requests[marker].append(body)
                        first = len(child_requests[marker]) == 1
                        if first:
                            started.add(marker)
                            if len(started) == len(markers):
                                all_started.set()
                    if first:
                        release.wait(8)
                        return reply(provider, calls=[calls[marker]])
                    return reply(provider, "Completed " + marker)

                with MockAPI([route]) as api:
                    process = start_agent(api, folder, provider, extra=("--approve",))
                    try:
                        self.assertTrue(all_started.wait(6), f"Only these model children started concurrently: {started}")
                        self.assertIsNone(process.poll())
                        release.set()
                        output, errors = process.communicate(timeout=15)
                        self.assertEqual(process.returncode, 0, output + errors)
                    finally:
                        release.set()
                        stop_process(process)
                    self.assertFalse(api.errors, api.errors)
                    self.assertEqual(Path(folder, "written.txt").read_text(), "Written by the child.\n")
                    self.assertEqual(Path(folder, "edit.txt").read_text(), "after editing")
                    self.assertEqual(len(root_requests), 2)
                    outcomes = json.loads(results(root_requests[-1], provider)[-1][1])["tasks"]
                    self.assertEqual([task["index"] for task in outcomes], [0, 1, 2])
                    self.assertEqual([task["status"] for task in outcomes], ["completed"] * 3)
                    for marker, outcome in zip(markers, outcomes):
                        self.assertIn(marker, outcome["summary"])
                        self.assertLessEqual(len(outcome["summary"].encode()), 6144)
                        self.assertEqual(len(child_requests[marker]), 2)
                        self.assertTrue(results(child_requests[marker][-1], provider))
                        self.assertIn(calls[marker][0], advertised_tools(child_requests[marker][0], provider))
                    self.assertIn("Reader found a żółw.", results(child_requests[markers[0]][-1], provider)[-1][1])
                    self.assertIn("Saved", results(child_requests[markers[1]][-1], provider)[-1][1])
                    self.assertIn("Saved", results(child_requests[markers[2]][-1], provider)[-1][1])
                    for headers, body in api.requests:
                        self.assertEqual(body["model"], "any/custom-model-id")
                        lowered = {key.lower(): value for key, value in headers.items()}
                        key_header = "x-api-key" if provider == "claude" else "authorization"
                        self.assertIn("test-key-never-render-me", lowered[key_header])
                        assert_tool_pairs(self, body["messages"], provider)
                    self.assertNotIn("test-key-never-render-me", output + errors)

    def test_parallel_limit_bounds_active_model_children_and_refills_slots(self):
        for limit in (1, 2):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as folder:
                markers = [f"LIMIT_CHILD_{index}" for index in range(4)]
                tasks = [{"mode": "read", "paths": [f"file{index}.txt"], "prompt": marker}
                         for index, marker in enumerate(markers)]
                for index in range(4):
                    Path(folder, f"file{index}.txt").write_text(str(index))
                releases = {marker: threading.Event() for marker in markers}
                first_wave = threading.Event()
                refilled = threading.Event()
                overflow = threading.Event()
                lock = threading.Lock()
                started = []
                active = 0
                peak = 0

                def route(body):
                    nonlocal active, peak
                    marker = next((item for item in markers if item in latest_user_text(body)), None)
                    if marker is None:
                        if not results(body, "openrouter"):
                            return reply("openrouter", calls=[("delegate_tasks", {"tasks": tasks})])
                        return reply("openrouter", "Limited delegation complete.")
                    with lock:
                        active += 1
                        peak = max(active, peak)
                        started.append(marker)
                        if active > limit:
                            overflow.set()
                        if len(started) == limit:
                            first_wave.set()
                        if len(started) > limit:
                            refilled.set()
                    try:
                        releases[marker].wait(8)
                        return reply("openrouter", "Completed " + marker)
                    finally:
                        with lock:
                            active -= 1

                with MockAPI([route]) as api:
                    process = start_agent(api, folder, extra=("--parallel", str(limit)))
                    try:
                        self.assertTrue(first_wave.wait(6), started)
                        self.assertFalse(overflow.wait(0.3), f"More than {limit} children made simultaneous requests")
                        releases[started[0]].set()
                        self.assertTrue(refilled.wait(5), "A completed child did not free a slot for the next task")
                        for release in releases.values():
                            release.set()
                        output, errors = process.communicate(timeout=12)
                        self.assertEqual(process.returncode, 0, output + errors)
                        self.assertEqual(peak, limit)
                        self.assertEqual(sorted(started), sorted(markers))
                    finally:
                        for release in releases.values():
                            release.set()
                        stop_process(process)

    def test_delegated_model_request_budget_reports_failure_to_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "file.txt").write_text("abcdefghijklmnopqrstuv")
            child_requests = []
            root_requests = []

            def route(body):
                if "BUDGET_CHILD" in latest_user_text(body):
                    child_requests.append(body)
                    return reply("openrouter", calls=[("read_file", {
                        "path": "file.txt", "offset": len(child_requests) - 1, "limit": 4})])
                root_requests.append(body)
                if not results(body, "openrouter"):
                    return reply("openrouter", calls=[("delegate_tasks", {"tasks": [
                        {"mode": "read", "paths": ["file.txt"], "prompt": "BUDGET_CHILD"}]})])
                return reply("openrouter", "The bounded child stopped.")

            with MockAPI([route]) as api:
                result = self.run_agent(api, directory=folder)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertGreater(len(child_requests), 0)
                self.assertLessEqual(len(child_requests), 4)
                self.assertEqual(len(root_requests), 2)
                outcome = json.loads(results(root_requests[-1], "openrouter")[-1][1])["tasks"][0]
                self.assertEqual(outcome["status"], "failed")
                self.assertTrue(outcome["summary"])

    def test_delegates_enforce_modes_paths_and_coordinator_only_graph_writes(self):
        for provider in ("openrouter", "claude"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                originals = {
                    "read.txt": "Allowed read contents.", "edit.txt": "Edit mode original.",
                    "write.txt": "Write mode original.", "outside.txt": "OUTSIDE_SECRET_MUST_NOT_REACH_MODEL",
                    "zero.graph": "Root graph original.", "nested/zero.graph": "Nested graph original.",
                }
                for path, content in originals.items():
                    file = Path(folder, path)
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_text(content)
                tasks = [
                    {"mode": "read", "paths": ["read.txt"], "prompt": "RESTRICT_READ"},
                    {"mode": "edit", "paths": ["edit.txt"], "prompt": "RESTRICT_EDIT"},
                    {"mode": "write", "paths": ["write.txt"], "prompt": "RESTRICT_WRITE"},
                    {"mode": "read", "paths": ["zero.graph", "nested/zero.graph"], "prompt": "RESTRICT_GRAPH"},
                ]
                calls = {
                    "RESTRICT_READ": [
                        ("read_file", {"path": "outside.txt"}),
                        ("write_file", {"path": "read.txt", "content": "forbidden overwrite"}),
                        ("edit_file", {"path": "read.txt", "old_text": "Allowed", "new_text": "forbidden"}),
                        ("read_file", {"path": "read.txt"}),
                    ],
                    "RESTRICT_EDIT": [
                        ("write_file", {"path": "edit.txt", "content": "forbidden overwrite"}),
                        ("edit_file", {"path": "outside.txt", "old_text": "SECRET", "new_text": "modified"}),
                        ("read_file", {"path": "edit.txt"}),
                    ],
                    "RESTRICT_WRITE": [
                        ("write_file", {"path": "outside.txt", "content": "forbidden overwrite"}),
                        ("run_command", {"command": "printf escaped > escaped.txt"}),
                        ("delegate_tasks", {"tasks": [{"mode": "read", "paths": ["outside.txt"], "prompt": "NESTED_CHILD_MUST_NOT_START"}]}),
                        ("load_skill", {"name": "arbitrary-skill"}),
                        ("mcp__fake__tool", {}),
                    ],
                    "RESTRICT_GRAPH": [
                        ("write_file", {"path": "zero.graph", "content": "forbidden graph"}),
                        ("edit_file", {"path": "nested/zero.graph", "old_text": "original", "new_text": "modified"}),
                        ("write_file", {"path": "ZERO.GRAPH", "content": "forbidden graph alias"}),
                        ("edit_file", {"path": "nested/ZERO.GRAPH", "old_text": "original", "new_text": "modified"}),
                    ],
                }
                child_requests = {task["prompt"]: [] for task in tasks}
                root_requests = []

                def route(body):
                    marker = next((item for item in calls if item in latest_user_text(body)), None)
                    if marker is None:
                        root_requests.append(body)
                        if not results(body, provider):
                            return reply(provider, calls=[("delegate_tasks", {"tasks": tasks})])
                        return reply(provider, "Restrictions checked.")
                    child_requests[marker].append(body)
                    if not results(body, provider):
                        return reply(provider, calls=calls[marker])
                    return reply(provider, "Restrictions checked for " + marker)

                with MockAPI([route]) as api:
                    result = self.run_agent(api, provider, folder, extra=("--approve",))
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    for path, content in originals.items():
                        self.assertEqual(Path(folder, path).read_text(), content, path)
                    for path in ("ZERO.GRAPH", "nested/ZERO.GRAPH"):
                        if Path(folder, path).exists():
                            self.assertEqual(Path(folder, path).read_text(), originals[path.lower()])
                    self.assertFalse(Path(folder, "escaped.txt").exists())
                    self.assertNotIn("OUTSIDE_SECRET_MUST_NOT_REACH_MODEL", json.dumps(api.requests) + result.stdout)
                    self.assertFalse(any("NESTED_CHILD_MUST_NOT_START" in latest_user_text(body)
                                         for _, body in api.requests))
                    for task in tasks[:3]:
                        requests = child_requests[task["prompt"]]
                        self.assertEqual(len(requests), 2, task["prompt"])
                        allowed = {"read_file", "finish_task"}
                        if task["mode"] != "read":
                            allowed.add("edit_file")
                        if task["mode"] == "write":
                            allowed.add("write_file")
                        self.assertLessEqual(advertised_tools(requests[0], provider), allowed)
                        self.assertIn("Error", json.dumps(results(requests[-1], provider)))
                    self.assertEqual(len(root_requests), 2)
                    outcomes = json.loads(results(root_requests[-1], provider)[-1][1])["tasks"]
                    self.assertEqual([task["index"] for task in outcomes], list(range(4)))
                    self.assertTrue(all(task["status"] in ("completed", "failed") for task in outcomes))

                for mode, path in (("write", "zero.graph"), ("edit", "nested/zero.graph"),
                                   ("write", "ZERO.GRAPH"), ("edit", "nested/ZERO.GRAPH")):
                    existed = Path(folder, path).exists()
                    with self.subTest(graph_mode=mode, graph_path=path), MockAPI([
                        reply(provider, calls=[("delegate_tasks", {"tasks": [
                            {"mode": mode, "paths": [path], "prompt": "GRAPH_OWNER_MUST_NOT_START"}]})]),
                        reply(provider, "Graph ownership remains with the coordinator."),
                    ]) as api:
                        result = self.run_agent(api, provider, folder, extra=("--approve",))
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(len(api.requests), 2)
                        self.assertFalse(any("GRAPH_OWNER_MUST_NOT_START" in latest_user_text(body)
                                             for _, body in api.requests))
                        self.assertEqual(Path(folder, path.lower()).read_text(), originals[path.lower()])
                        self.assertEqual(Path(folder, path).exists(), existed)
                        self.assertIn("Error", json.dumps(results(api.requests[-1][1], provider)))

    def test_native_file_batches_preserve_order_and_tool_barriers(self):
        for provider in ("openrouter", "claude"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                Path(folder, "shared.txt").write_text("initial")
                alias = "SHARED.txt" if Path(folder, "SHARED.txt").exists() else "shared.txt"
                calls = [
                    ("write_file", {"path": "shared.txt", "content": "first"}),
                    ("write_file", {"path": "independent.txt", "content": "independent"}),
                    ("read_file", {"path": alias}),
                    ("edit_file", {"path": "shared.txt", "old_text": "first", "new_text": "second"}),
                    ("read_file", {"path": "shared.txt"}),
                    ("run_command", {"command": "printf barrier > shared.txt"}),
                    ("read_file", {"path": "shared.txt"}),
                ]
                with MockAPI([reply(provider, calls=calls), reply(provider, "Ordered file batch complete.")]) as api:
                    result = self.run_agent(api, provider, folder, extra=("--approve", "--parallel", "4"))
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    final_request = api.requests[-1][1]
                    outcomes = results(final_request, provider)
                    self.assertEqual([call_id for call_id, _ in outcomes], [f"call_{index}" for index in range(len(calls))])
                    self.assertEqual([outcomes[index][1] for index in (2, 4, 6)], ["first", "second", "barrier"])
                    self.assertEqual(Path(folder, "shared.txt").read_text(), "barrier")
                    self.assertEqual(Path(folder, "independent.txt").read_text(), "independent")
                    assert_tool_pairs(self, final_request["messages"], provider)
                with MockAPI([reply(provider, calls=[
                    ("write_file", {"path": "before-finish.txt", "content": "before"}),
                    ("write_file", {"path": "also-before.txt", "content": "before"}),
                    ("finish_task", {"summary": "The requested work is complete."}),
                    ("write_file", {"path": "after-finish.txt", "content": "must not run"}),
                ])]) as api:
                    result = self.run_agent(api, provider, folder, extra=("--approve",))
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(len(api.requests), 1)
                    self.assertTrue(Path(folder, "before-finish.txt").exists())
                    self.assertTrue(Path(folder, "also-before.txt").exists())
                    self.assertFalse(Path(folder, "after-finish.txt").exists())

    def test_headless_parallel_and_delegated_writes_require_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            child_requests = []

            def route(body):
                if "DENIED_CHILD" in latest_user_text(body):
                    child_requests.append(body)
                    if not results(body, "openrouter"):
                        return reply("openrouter", calls=[("write_file", {"path": "child.txt", "content": "denied"})])
                    return reply("openrouter", "The child write was denied.")
                if not results(body, "openrouter"):
                    return reply("openrouter", calls=[
                        ("write_file", {"path": "first.txt", "content": "denied"}),
                        ("write_file", {"path": "second.txt", "content": "denied"}),
                        ("delegate_tasks", {"tasks": [{"mode": "write", "paths": ["child.txt"], "prompt": "DENIED_CHILD"}]}),
                    ])
                return reply("openrouter", "All writes required approval.")

            with MockAPI([route]) as api:
                result = self.run_agent(api, directory=folder)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for path in ("first.txt", "second.txt", "child.txt"):
                    self.assertFalse(Path(folder, path).exists(), path)
                self.assertEqual(len(child_requests), 2)
                self.assertIn("denied", json.dumps(results(child_requests[-1], "openrouter")).lower())

    def test_repeated_native_batch_stops_before_model_or_command_barrier(self):
        for provider in ("openrouter", "claude"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                Path(folder, "repeat.txt").write_text("The same result each time.")
                calls = [("read_file", {"path": "repeat.txt"}) for _ in range(3)]
                calls.append(("write_file", {"path": "in-flight.txt", "content": "Already scheduled work."}))
                calls.append(("run_command", {"command": "printf forbidden > after-repeat.txt"}))
                with MockAPI([reply(provider, calls=calls), reply(provider, "Unexpected model continuation.")]) as api:
                    result = self.run_agent(api, provider, folder, extra=("--approve",))
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertEqual(len(api.requests), 1)
                    self.assertIn("Repeated tool calls stopped", result.stdout)
                    self.assertNotIn("Unexpected model continuation.", result.stdout)
                    self.assertFalse(Path(folder, "after-repeat.txt").exists())
                    self.assertEqual(Path(folder, "in-flight.txt").read_text(), "Already scheduled work.")
                    self.assertIn("Saved in-flight.txt", result.stdout)

    def test_oversized_native_write_arguments_preserve_following_read_pairing(self):
        for provider in ("openrouter", "claude"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                Path(folder, "oversized.txt").write_text("Original file survives.")
                Path(folder, "read.txt").write_text("The valid read follows the rejected write.")
                calls = [
                    ("write_file", {"path": "oversized.txt", "content": "x" * 16500}),
                    ("read_file", {"path": "read.txt"}),
                ]
                with MockAPI([reply(provider, calls=calls), reply(provider, "Oversized arguments handled.")]) as api:
                    result = self.run_agent(api, provider, folder, extra=("--approve",))
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(len(api.requests), 2)
                    self.assertEqual(Path(folder, "oversized.txt").read_text(), "Original file survives.")
                    body = api.requests[-1][1]
                    outcomes = results(body, provider)
                    self.assertEqual([call_id for call_id, _ in outcomes], ["call_0", "call_1"])
                    self.assertTrue(outcomes[0][1].startswith("Error:"), outcomes[0][1])
                    self.assertEqual(outcomes[1][1], "The valid read follows the rejected write.")
                    assert_tool_pairs(self, body["messages"], provider)

    def test_native_read_batch_preserves_full_control_character_payloads(self):
        for provider in ("openrouter", "claude"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                content = "\n" * 12000
                Path(folder, "newlines.txt").write_text(content)
                Path(folder, "normal.txt").write_text("A normal result from the other worker.")
                # Keep the large result last: appending another result would intentionally
                # compact older history, obscuring the worker transport assertion.
                calls = [("read_file", {"path": path}) for path in ("normal.txt", "newlines.txt")]
                with MockAPI([reply(provider, calls=calls), reply(provider, "Full read results received.")]) as api:
                    result = self.run_agent(api, provider, folder)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(len(api.requests), 2)
                    body = api.requests[-1][1]
                    self.assertEqual(results(body, provider), [
                        ("call_0", "A normal result from the other worker."),
                        ("call_1", content),
                    ])
                    assert_tool_pairs(self, body["messages"], provider)

    def test_native_writer_serializes_unicode_case_and_normalization_aliases(self):
        aliases = (("Unicode case", "żółw.txt", "ŻÓŁW.txt"),
                   ("Unicode normalization", "café.txt", "cafe\u0301.txt"))
        for provider in ("openrouter", "claude"):
            for label, canonical, alias in aliases:
                with self.subTest(provider=provider, alias=label), tempfile.TemporaryDirectory() as folder:
                    file = Path(folder, canonical)
                    alias_file = Path(folder, alias)
                    file.write_text("Initial contents before the write.")
                    if not alias_file.exists() or not file.samefile(alias_file):
                        self.skipTest("This filesystem treats the two Unicode paths as distinct files")
                    calls = [
                        ("write_file", {"path": canonical, "content": "Written before the aliased read."}),
                        ("read_file", {"path": alias}),
                    ]
                    with MockAPI([reply(provider, calls=calls), reply(provider, "Aliased operations complete.")]) as api:
                        result = self.run_agent(api, provider, folder, extra=("--approve", "--parallel", "4"))
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(len(api.requests), 2)
                        body = api.requests[-1][1]
                        outcomes = results(body, provider)
                        self.assertEqual([call_id for call_id, _ in outcomes], ["call_0", "call_1"])
                        self.assertIn("Saved", outcomes[0][1])
                        self.assertEqual(outcomes[1][1], "Written before the aliased read.")
                        self.assertEqual(file.read_text(), "Written before the aliased read.")
                        assert_tool_pairs(self, body["messages"], provider)

    def test_tui_approvals_are_independent_and_other_children_keep_running(self):
        with tempfile.TemporaryDirectory() as folder:
            with MockAPI([reply("openrouter", calls=[
                    ("write_file", {"path": "first.txt", "content": "approved"}),
                    ("write_file", {"path": "second.txt", "content": "approved"})]),
                    reply("openrouter", "Both decisions recorded.")]) as api:
                terminal = Terminal(["--cwd", folder], environment(api.url))
                try:
                    terminal.wait_for("Your terminal.")
                    terminal.send("Review both writes.\r")
                    terminal.wait_for("Approve this action?")
                    self.assertFalse(Path(folder, "first.txt").exists())
                    self.assertFalse(Path(folder, "second.txt").exists())
                    after = len(terminal.output)
                    terminal.send("n")
                    terminal.wait_for("Approve this action?", after=after)
                    terminal.send("y")
                    terminal.wait_for("Both decisions recorded.")
                    saved = [path for path in (Path(folder, "first.txt"), Path(folder, "second.txt")) if path.exists()]
                    self.assertEqual(len(saved), 1)
                    self.assertEqual(saved[0].read_text(), "approved")
                    outcomes = results(api.requests[-1][1], "openrouter")
                    self.assertEqual(sum("denied" in content.lower() for _, content in outcomes), 1)
                    self.assertEqual(sum("Saved" in content for _, content in outcomes), 1)
                finally:
                    self.assertEqual(terminal.close(), terminal.original)

            edited = Path(folder, "edit.txt")
            edited.write_text("before")
            Path(folder, "read.txt").write_text("independent read")
            reader_finished = threading.Event()
            root_requests = []
            edit_requests = []

            def route(body):
                prompt = latest_user_text(body)
                if "APPROVAL_EDITOR" in prompt:
                    edit_requests.append(body)
                    if not results(body, "openrouter"):
                        return reply("openrouter", calls=[("edit_file", {"path": "edit.txt", "old_text": "before", "new_text": "after"})])
                    return reply("openrouter", "Edit result checked.")
                if "INDEPENDENT_READER" in prompt:
                    if not results(body, "openrouter"):
                        return reply("openrouter", calls=[("read_file", {"path": "read.txt"})])
                    reader_finished.set()
                    return reply("openrouter", "Independent reader complete.")
                root_requests.append(body)
                if not results(body, "openrouter"):
                    return reply("openrouter", calls=[("delegate_tasks", {"tasks": [
                        {"mode": "edit", "paths": ["edit.txt"], "prompt": "APPROVAL_EDITOR"},
                        {"mode": "read", "paths": ["read.txt"], "prompt": "INDEPENDENT_READER"}]})])
                return reply("openrouter", "Delegated preview checked.")

            with MockAPI([route]) as api:
                terminal = Terminal(["--cwd", folder], environment(api.url))
                try:
                    terminal.wait_for("Your terminal.")
                    terminal.send("Delegate the edit and read.\r")
                    terminal.wait_for("Approve this action?")
                    self.assertTrue(reader_finished.wait(6), "The reader stopped making progress while the editor waited for approval")
                    edited.write_text("user changed this after the preview")
                    terminal.send("y")
                    terminal.wait_for("Delegated preview checked.")
                    self.assertEqual(edited.read_text(), "user changed this after the preview")
                    self.assertIn("changed since the preview", json.dumps(results(edit_requests[-1], "openrouter")))
                    assert_tool_pairs(self, root_requests[-1]["messages"], "openrouter")
                finally:
                    self.assertEqual(terminal.close(), terminal.original)

    def test_cancelled_delegates_cannot_write_and_next_task_has_clean_history(self):
        with tempfile.TemporaryDirectory() as folder:
            markers = ("CANCEL_CHILD_ONE", "CANCEL_CHILD_TWO")
            release = threading.Event()
            all_started = threading.Event()
            lock = threading.Lock()
            started = set()
            root_requests = []

            def delayed_stream(marker):
                with lock:
                    started.add(marker)
                    if len(started) == len(markers):
                        all_started.set()
                yield sse(chunk({"role": "assistant", "content": "Preparing " + marker,
                                 "tool_calls": [{"index": 0, "id": "late_write", "type": "function", "function": {
                                     "name": "write_file", "arguments": json.dumps({"path": marker + ".txt", "content": "must not run"})}}]}))
                release.wait(8)
                yield sse(chunk(finish="tool_calls"))
                yield sse("[DONE]")

            def route(body):
                prompt = latest_user_text(body)
                marker = next((item for item in markers if item in prompt), None)
                if marker is not None:
                    return StreamReply(delayed_stream(marker))
                root_requests.append(body)
                if "NEXT_PARALLEL_TASK" in prompt:
                    return reply("openrouter", "Recovered after cancellation.")
                return reply("openrouter", calls=[("delegate_tasks", {"tasks": [
                    {"mode": "write", "paths": [marker + ".txt"], "prompt": marker} for marker in markers]})])

            with MockAPI([route]) as api:
                terminal = Terminal(["--cwd", folder, "--approve"], environment(api.url))
                try:
                    terminal.wait_for("Your terminal.")
                    terminal.send("Start cancellable delegation.\r")
                    self.assertTrue(all_started.wait(6), started)
                    terminal.send(b"\x1b")
                    terminal.wait_for("Cancelled.")
                    release.set()
                    terminal.send("NEXT_PARALLEL_TASK\r")
                    terminal.wait_for("Recovered after cancellation.")
                    for marker in markers:
                        self.assertFalse(Path(folder, marker + ".txt").exists())
                        self.assertNotIn(marker, json.dumps(root_requests[-1]))
                    assert_tool_pairs(self, root_requests[-1]["messages"], "openrouter")
                finally:
                    release.set()
                    self.assertEqual(terminal.close(), terminal.original)


if __name__ == "__main__":
    unittest.main()
