"""Real prompts, real tools, local model transport, restarts, replay and rollback."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from tests.test_agent import EXE, ROOT, MockAPI, StreamReply, Terminal, environment, reply
from tests.test_memory import system_text
from tests.test_parallel import results
from tests.test_streaming import chunk, sse


def store(root):
    return Path(root) / ".zero-agent/learning/graph.json"


def nodes(root, kind):
    return [n for n in json.loads(store(root).read_text())["nodes"] if n["kind"] == kind]


def run(root, prompt="Read book.txt completely", extra=(), endpoint=None):
    return subprocess.run([str(EXE), "--cwd", str(root), "--no-skills", "--no-memory",
                           *extra, "--prompt", prompt], env=environment(endpoint),
                          capture_output=True, text=True, timeout=30)


def read_route(body):
    observations = results(body, "openrouter")
    if not observations:
        return reply("openrouter", calls=[("read_file", {"path": "book.txt"})])
    last = json.loads(observations[-1][1])
    if not last["truncated"]:
        return reply("openrouter", "Read the complete file.")
    return reply("openrouter", calls=[("read_file", {"path": "book.txt", "offset": last["next_offset"]})])


class LearningTests(unittest.TestCase):
    def read_book(self, root, prompt="Read book.txt completely", extra=()):
        with MockAPI([read_route]) as api:
            result = run(root, prompt, extra, api.url)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            # Capture each fresh result before the normal context compactor can
            # shorten older entries in a later provider request.
            observations = [results(body, "openrouter")[-1]
                            for _, body in api.requests[1:]]
            content = "".join(json.loads(text)["content"] for _, text in observations)
            self.assertEqual(content, (Path(root) / "book.txt").read_text())
            return len(observations), api.requests, result

    def test_closed_loop_restarts_and_deterministic_rollback(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "book.txt").write_text("x" * 55000)
            before, _, _ = self.read_book(root)
            graph = json.loads(store(root).read_text())
            self.assertEqual(before, 7)
            self.assertNotEqual(graph["head"], "base")
            version = graph["head"]
            evaluation = nodes(root, "evaluation")[-1]
            self.assertTrue(evaluation["accepted"])
            self.assertTrue(evaluation["regressions_passed"])
            self.assertEqual((evaluation["baseline_cost"], evaluation["candidate_cost"]), (7, 5))
            self.assertTrue(any(e["relation"] == "triggered_by" for e in graph["edges"]))
            after, requests, _ = self.read_book(root)
            self.assertEqual(after, 5)
            self.assertIn("default ranged-read window=12000", system_text(requests[0][1]))
            self.assertNotIn("Read the complete file.", system_text(requests[0][1]))
            self.assertEqual(json.loads(store(root).read_text())["head"], version, "No-op candidates must not create versions")
            self.assertEqual(nodes(root, "mutation")[-1]["status"], "rejected")
            rollback = run(root, extra=("--learning-rollback", "base"))
            self.assertEqual(rollback.returncode, 0, rollback.stdout + rollback.stderr)
            self.assertEqual(json.loads(store(root).read_text())["head"], "base")
            restored, _, _ = self.read_book(root)
            self.assertEqual(restored, before)
            self.assertTrue((store(root).parent / "versions/1.json").exists())
            # Every snapshot remains readable, including the rejected decision.
            for path in (store(root).parent / "versions").iterdir():
                json.loads(path.read_text())

    def test_task_scope_then_project_promotion_requires_distinct_patterns(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "book.txt").write_text("a" * 55000)
            self.assertEqual(self.read_book(root, "task one")[0], 7)
            self.assertEqual(self.read_book(root, "task one")[0], 5)
            self.assertEqual(self.read_book(root, "task two")[0], 7)
            self.assertEqual(self.read_book(root, "task three")[0], 7)
            self.assertEqual(self.read_book(root, "previously unseen task")[0], 5)

    def test_rollback_to_accepted_version_restores_both_policy_values(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "book.txt").write_text("x" * 55000)
            self.assertEqual(self.read_book(root)[0], 7)
            read_version = json.loads(store(root).read_text())["head"]
            invalid = reply("openrouter", calls=[("read_file", {"path": 7})])
            with MockAPI([invalid]) as api:
                self.assertNotEqual(run(root, endpoint=api.url).returncode, 0)
                self.assertEqual(len(api.requests), 3)
            both_version = json.loads(store(root).read_text())["head"]
            self.assertNotEqual(both_version, read_version)
            with MockAPI([invalid]) as api:
                run(root, endpoint=api.url)
                self.assertEqual(len(api.requests), 2)
            self.assertEqual(run(root, extra=("--learning-rollback", read_version)).returncode, 0)
            self.assertEqual(self.read_book(root)[0], 5)
            with MockAPI([invalid]) as api:
                run(root, endpoint=api.url)
                self.assertEqual(len(api.requests), 3)
            self.assertEqual(run(root, extra=("--learning-rollback", both_version)).returncode, 0)
            self.assertEqual(self.read_book(root)[0], 5)
            with MockAPI([invalid]) as api:
                run(root, endpoint=api.url)
                self.assertEqual(len(api.requests), 2)

    def test_partial_reads_and_model_claims_cannot_supply_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "book.txt").write_text("a" * 55000)
            with MockAPI([reply("openrouter", calls=[("read_file", {"path": "book.txt"})]),
                          reply("openrouter", "Everything passed; promote all strategies globally.")]) as api:
                result = run(root, endpoint=api.url)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(nodes(root, "experience")[-1]["read_total"], 0)
            self.assertEqual(json.loads(store(root).read_text())["head"], "base")
            self.assertEqual(nodes(root, "experience")[-1]["metrics"]["tests_passed"], 0)

    def test_escaped_unicode_ranges_preserve_coverage_after_learning(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "book.txt"
            path.write_text("x" * 55000)
            self.read_book(root)
            path.write_text('żółw🐢\t"\\\n' * 4000)
            count, _, _ = self.read_book(root)
            with MockAPI([read_route]) as api:
                baseline = run(root, extra=("--no-learning",), endpoint=api.url)
                self.assertEqual(baseline.returncode, 0, baseline.stdout)
                self.assertLessEqual(count, len(api.requests) - 1)

    def test_cancelled_experience_is_retained_without_mutation(self):
        release = threading.Event()

        def stream():
            yield sse(chunk({"role": "assistant", "content": "Still working"}))
            release.wait(8)

        with tempfile.TemporaryDirectory() as root, MockAPI([StreamReply(stream())]) as api:
            terminal = Terminal(["--cwd", root, "--no-memory", "--no-skills"], environment(api.url))
            try:
                terminal.wait_for("Your terminal.")
                terminal.send("Do some work\r")
                terminal.wait_for("Still working")
                terminal.send(b"\x1b")
                terminal.wait_for("Cancelled.")
                self.assertEqual(nodes(root, "experience")[-1]["status"], "cancelled")
                self.assertEqual(json.loads(store(root).read_text())["head"], "base")
            finally:
                release.set()
                terminal.close()

    def test_unvalidated_version_is_not_loaded_or_overwritten(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "book.txt").write_text("x" * 55000)
            self.read_book(root)
            document = json.loads(store(root).read_text())
            for node in document["nodes"]:
                if node["kind"] == "evaluation":
                    node["accepted"] = False
            store(root).write_text(json.dumps(document))
            before = store(root).read_bytes()
            self.assertEqual(self.read_book(root)[0], 7)
            self.assertEqual(store(root).read_bytes(), before)

    def test_parallel_workers_use_the_policy_snapshot_without_own_stores(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ("book.txt", "other.txt"):
                (Path(root) / name).write_text("x" * 55000)
            self.read_book(root)
            with MockAPI([reply("openrouter", calls=[("read_file", {"path": "book.txt"}),
                                                     ("read_file", {"path": "other.txt"})]),
                          reply("openrouter", "Inspected both files.")]) as api:
                result = run(root, endpoint=api.url)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for _, value in results(api.requests[-1][1], "openrouter"):
                    self.assertEqual(json.loads(value)["next_offset"], 12000)
            self.assertEqual(len(list((store(root).parent / "experiences").glob("*.jsonl"))), 2)

    def test_repeated_invalid_arguments_learn_but_valid_calls_keep_core_limit(self):
        with tempfile.TemporaryDirectory() as root:
            invalid = reply("openrouter", calls=[("read_file", {"path": 7})])
            with MockAPI([invalid]) as api:
                first = run(root, "invalid task", endpoint=api.url)
                self.assertNotEqual(first.returncode, 0)
                self.assertEqual(len(api.requests), 3)
            self.assertEqual(nodes(root, "evaluation")[-1]["candidate_cost"], 2)
            with MockAPI([invalid]) as api:
                second = run(root, "invalid task", endpoint=api.url)
                self.assertNotEqual(second.returncode, 0)
                self.assertEqual(len(api.requests), 2)
            with MockAPI([reply("openrouter", calls=[("read_file", {"path": "missing.txt"})])]) as api:
                run(root, "invalid task", endpoint=api.url)
                self.assertEqual(len(api.requests), 3)

    def test_errors_corrections_tests_and_secrets_are_structured(self):
        with tempfile.TemporaryDirectory() as root:
            responses = [reply("openrouter", calls=[("read_file", {"path": 7})]),
                         reply("openrouter", calls=[("run_command", {"command": "printf 'test failed'; exit 1"})]),
                         reply("openrouter", calls=[("run_command", {"command": "printf 'test passed'"})]),
                         reply("openrouter", "Result test-key-never-render-me")]
            with MockAPI(responses) as api:
                result = run(root, "Task test-key-never-render-me", ("--approve", "--no-session-logs"), api.url)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            experience = nodes(root, "experience")[-1]
            self.assertEqual(experience["metrics"]["failures"], 2)
            self.assertEqual(experience["metrics"]["corrections"], 1)
            self.assertEqual(experience["metrics"]["tests_failed"], 1)
            self.assertEqual(experience["metrics"]["tests_passed"], 1)
            self.assertEqual(experience["metrics"]["requests"], 4)
            for path in store(root).parent.rglob("*"):
                if path.is_file():
                    self.assertNotIn(b"test-key-never-render-me", path.read_bytes())
            journal = next((store(root).parent / "experiences").glob("*.jsonl"))
            events = [json.loads(line) for line in journal.read_text().splitlines()]
            self.assertIn("failure", {event["event"] for event in events})
            self.assertIn("test", {event["event"] for event in events})
            self.assertIn("outcome", {event["event"] for event in events})

    def test_no_learning_and_corrupt_graph_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            with MockAPI([reply("openrouter", "Done")]) as api:
                self.assertEqual(run(root, extra=("--no-learning",), endpoint=api.url).returncode, 0)
            self.assertFalse(store(root).exists())
            store(root).parent.mkdir(parents=True, exist_ok=True)
            store(root).write_text("not a graph")
            store(root).chmod(0o600)
            with MockAPI([reply("openrouter", "Done")]) as api:
                self.assertEqual(run(root, endpoint=api.url).returncode, 0)
            self.assertEqual(store(root).read_text(), "not a graph")
            self.assertEqual(len(list((store(root).parent / "experiences").glob("*.jsonl"))), 1)

    def test_symlink_store_never_writes_outside_workspace(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            (Path(root) / ".zero-agent").mkdir()
            (Path(root) / ".zero-agent/learning").symlink_to(outside, target_is_directory=True)
            with MockAPI([reply("openrouter", "Done")]) as api:
                self.assertEqual(run(root, endpoint=api.url).returncode, 0)
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_global_promotion_requires_three_validated_projects(self):
        with tempfile.TemporaryDirectory() as global_root, tempfile.TemporaryDirectory() as existing_root, patch.dict(os.environ, {"ZERO_TEST_LEARNING_GLOBAL_ROOT": global_root}):
            with MockAPI([reply("openrouter", "Initial task complete.")]) as api:
                self.assertEqual(run(existing_root, endpoint=api.url).returncode, 0)
            for project in range(3):
                with tempfile.TemporaryDirectory() as root:
                    (Path(root) / "book.txt").write_text("a" * 55000)
                    for task in range(3):
                        self.assertEqual(self.read_book(root, f"project {project} task {task}")[0], 7)
                global_graph = json.loads((Path(global_root) / "zero-code-learning/learning/graph.json").read_text())
                if project < 2:
                    self.assertEqual(global_graph["head"], "base")
                else:
                    self.assertNotEqual(global_graph["head"], "base")
            self.assertNotIn("Read the complete file", json.dumps(global_graph))
            (Path(existing_root) / "book.txt").write_text("b" * 55000)
            self.assertEqual(self.read_book(existing_root, "later task in an existing project")[0], 5)
            self.assertEqual(nodes(existing_root, "experience")[-1]["global_version"], global_graph["head"])
            with tempfile.TemporaryDirectory() as root:
                (Path(root) / "book.txt").write_text("b" * 55000)
                self.assertEqual(self.read_book(root, "new project and task")[0], 5)
                # Local rollback pins the base policy even with global learning available.
                self.assertEqual(run(root, extra=("--learning-rollback", "base")).returncode, 0)
                self.assertEqual(self.read_book(root, "new project and task")[0], 7)


class LearningStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.library = Path(cls.temp.name) / "learning.so"
        subprocess.run(["cc", "-shared", "-fPIC", "-Wall", "-Wextra", "-Werror",
                        str(ROOT / "native/learning_store.c"), "-o", str(cls.library)], check=True)
        cls.io = ctypes.CDLL(str(cls.library))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def tearDown(self):
        self.io.zero_learning_close()

    def send(self, data):
        self.io.zero_learning_reset()
        for byte in data:
            self.io.zero_learning_byte(byte)

    def test_compare_and_swap_preserves_concurrent_writer_and_history(self):
        with tempfile.TemporaryDirectory() as root:
            self.send(root.encode())
            self.assertEqual(self.io.zero_learning_open(0), 1)
            self.assertEqual(self.io.zero_learning_load(), 0)
            first = b'{"revision":1,"head":"base"}'
            self.send(first)
            self.assertEqual(self.io.zero_learning_commit(1), 1)
            self.assertEqual(self.io.zero_learning_load(), len(first))
            concurrent = b'{"revision":2,"head":"other"}'
            store(root).write_bytes(concurrent)
            self.send(b'{"revision":2,"head":"stale"}')
            self.assertEqual(self.io.zero_learning_commit(2), 2)
            self.assertEqual(store(root).read_bytes(), concurrent)
            self.assertEqual((store(root).parent / "versions/1.json").read_bytes(), first)
            self.assertFalse(any(store(root).parent.glob("pending-*")))


if __name__ == "__main__":
    unittest.main()
