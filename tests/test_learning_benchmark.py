"""Independent oracles, frozen evaluation and replay admission verification."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts.learning_benchmark import Case, Replay, Transport, cases, compare, graph, run_case
from tests.test_agent import EXE, MockAPI, reply
from tests import test_learning
from tests.test_learning import nodes, run, store


class BenchmarkOracleTests(unittest.TestCase):
    def test_reproducible_disjoint_balanced_workloads(self):
        training = cases(100, 17, "online")
        heldout = cases(40, 17, "holdout")
        self.assertEqual(training, cases(100, 17, "online"))
        self.assertFalse({c.prompt for c in training} & {c.prompt for c in heldout})
        self.assertFalse({c.content for c in training} & {c.content for c in heldout})
        for family in {c.family for c in training}:
            self.assertTrue(any(c.family == family for c in heldout))

    def test_success_requires_exact_contiguous_bytes_and_exit_zero(self):
        replay = Replay(Case("check", "ascii_read", "abcdef"))
        replay.observe({"tool_call_id": "1", "content": json.dumps({"content": "abc", "offset": 0,
                        "next_offset": 3, "total_bytes": 6, "truncated": True})})
        self.assertFalse(replay.success(0), "A successful exit cannot make a partial read succeed")
        replay.observe({"tool_call_id": "2", "content": json.dumps({"content": "def", "offset": 3,
                        "next_offset": 6, "total_bytes": 6, "truncated": False})})
        self.assertTrue(replay.success(0))
        self.assertFalse(replay.success(1))
        bad = Replay(Case("check", "ascii_read", "abcdef"))
        bad.observe({"tool_call_id": "1", "content": "abcdeg"})
        self.assertFalse(bad.success(0))

    def test_failures_stay_in_denominator_and_spend(self):
        row = {"success": False, "tools": 6, "requests": 6, "input_tokens": 10,
               "output_tokens": 2, "cost_units": 18, "elapsed_ms": 5}
        learned = {**row, "success": True, "tools": 5, "cost_units": 15}
        report = compare([{"baseline": row, "adaptive": learned}])
        self.assertEqual(report["baseline"]["cost_units"], 18)
        self.assertIsNone(report["baseline"]["cost_units_per_success"])
        self.assertEqual(report["success_rate_delta"], 1)
        self.assertEqual(report["wins"], 1)

    def test_transport_follows_observed_offsets_and_prices_all_context(self):
        replay = Replay(Case("example", "ascii_read", "abcdef"))
        messages = [{"role": "user", "content": replay.case.prompt}]
        first = replay.respond({"messages": messages})
        call = first["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(json.loads(call["function"]["arguments"]), {"path": "example.txt"})
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps({
            "content": "abc", "offset": 0, "next_offset": 3, "total_bytes": 6, "truncated": True})})
        second = replay.respond({"messages": messages})
        call = second["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(json.loads(call["function"]["arguments"])["offset"], 3)
        self.assertGreater(second["usage"]["prompt_tokens"], first["usage"]["prompt_tokens"])
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps({
            "content": "def", "offset": 3, "next_offset": 6, "total_bytes": 6, "truncated": False})})
        final = replay.respond({"messages": messages})
        self.assertEqual(final["choices"][0]["finish_reason"], "stop")
        self.assertTrue(replay.success(0))


class LearningMeasurementTests(unittest.TestCase):
    def test_split_usage_missing_usage_and_zero_usage(self):
        for usage, known in (({"total_tokens": 20}, False),
                             ({"prompt_tokens": 10, "completion_tokens": 5}, True),
                             ({"input_tokens": 0, "output_tokens": 0}, True),
                             ({"input_tokens": 10}, False)):
            with self.subTest(usage=usage), tempfile.TemporaryDirectory() as root:
                response = reply("openrouter")
                response["usage"] = usage
                with MockAPI([response]) as api:
                    self.assertEqual(run(root, extra=("--learning-frozen",), endpoint=api.url).returncode, 0)
                metrics = nodes(root, "experience")[-1]["metrics"]
                self.assertEqual(metrics["usage_complete"], known)
                self.assertEqual(metrics["usage_reports"], int(known))
                self.assertEqual(metrics["input_tokens"], usage.get("prompt_tokens", usage.get("input_tokens")) if known else None)
                self.assertEqual(metrics["output_tokens"], usage.get("completion_tokens", usage.get("output_tokens")) if known else None)

    def test_frozen_base_captures_without_candidates_and_learned_snapshot_is_reused(self):
        helper = test_learning.LearningTests()
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "book.txt").write_text("x" * 55000)
            self.assertEqual(helper.read_book(root, extra=("--learning-frozen",))[0], 7)
            self.assertEqual(json.loads(store(root).read_text())["head"], "base")
            self.assertEqual(nodes(root, "improvement_candidate"), [])
            self.assertEqual(nodes(root, "experience")[-1]["mode"], "frozen")
            helper.read_book(root)
            head = json.loads(store(root).read_text())["head"]
            candidates = len(nodes(root, "improvement_candidate"))
            self.assertEqual(helper.read_book(root, extra=("--learning-frozen",))[0], 5)
            self.assertEqual(json.loads(store(root).read_text())["head"], head)
            self.assertEqual(len(nodes(root, "improvement_candidate")), candidates)

    def test_admission_suite_predictions_match_real_tools_and_budget_outcomes(self):
        # Train through production experience/promotion, never inject a policy.
        with tempfile.TemporaryDirectory() as directory, Transport() as transport:
            base, learned = Path(directory) / "base", Path(directory) / "learned"
            base.mkdir()
            learned.mkdir()
            for index in range(3):
                run_case(EXE, learned, Case(f"training-{index}", "ascii_read", "x" * 55000), transport, False)
            document = graph(learned)
            evaluation = [n for n in document["nodes"] if n["kind"] == "evaluation" and n["accepted"]][-1]
            self.assertEqual(evaluation["evaluator"], "deterministic-policy-replay-v2")
            for arm, root in (("baseline", base), ("candidate", learned)):
                totals = {"tools": 0, "request_units": 0, "successes": 0}
                for index, size in enumerate((0, 12000, 17000, 33000, 55000, 65000, 95000)):
                    row = run_case(EXE, root, Case(f"unseen-{index}", "ascii_read", "z" * size, 6), transport, True)
                    totals["tools"] += row["tools"]
                    totals["request_units"] += row["requests"]
                    totals["successes"] += row["success"]
                # The remaining two admission cases are permanent/transient
                # repeated failures, already exercised by test_learning.
                totals["tools"] += 6
                totals["request_units"] += 6
                self.assertEqual(totals, evaluation["benchmark"][arm])
            self.assertEqual(graph(learned)["head"], document["head"])

    def test_tampered_admission_metrics_fail_closed_and_legacy_remains_readable(self):
        helper = test_learning.LearningTests()
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "book.txt").write_text("x" * 55000)
            helper.read_book(root)
            document = json.loads(store(root).read_text())
            evaluation = next(n for n in document["nodes"] if n["kind"] == "evaluation")
            evaluation["benchmark"]["candidate"]["successes"] = 9
            store(root).write_text(json.dumps(document))
            before = store(root).read_bytes()
            self.assertEqual(helper.read_book(root, extra=("--learning-frozen",))[0], 7)
            self.assertEqual(store(root).read_bytes(), before)
            evaluation["evaluator"] = "deterministic-policy-replay-v1"
            for key in ("benchmark", "baseline_policy", "candidate_policy"):
                del evaluation[key]
            store(root).write_text(json.dumps(document))
            self.assertEqual(helper.read_book(root, extra=("--learning-frozen",))[0], 5)

    def test_frozen_experiences_do_not_count_toward_promotion(self):
        helper = test_learning.LearningTests()
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "book.txt").write_text("x" * 55000)
            for index in range(3):
                helper.read_book(root, f"held out {index}", ("--learning-frozen",))
            helper.read_book(root, "training one")
            rules = nodes(root, "strategy_version")[-1]["rules"]
            self.assertTrue(all(rule["scope"] == "task" for rule in rules))
            self.assertEqual(helper.read_book(root, "unseen evaluation", ("--learning-frozen",))[0], 7)


if __name__ == "__main__":
    unittest.main()
