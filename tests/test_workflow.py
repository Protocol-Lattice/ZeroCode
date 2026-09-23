"""Black-box workflow gates using a local provider; no paid model requests."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.test_agent import EXE, MockAPI, Terminal, environment, function_tools, reply


def step(action, summary="Requirements, implementation and edge cases checked.", **fields):
    return ("workflow", {"action": action, "summary": summary, **fields})


def plan(verification="command", check="test -f result.txt"):
    return step("plan", "Create result.txt with the requested content; inspect it and verify it exists.",
                verification=verification, check=check)


def results(body):
    found = []
    for message in body["messages"]:
        if message["role"] == "tool":
            found.append(message["content"])
        elif isinstance(message.get("content"), list):
            found.extend(block["content"] for block in message["content"]
                         if block.get("type") == "tool_result")
    return found


def system(body):
    return body.get("system") or body["messages"][0]["content"]


class WorkflowTests(unittest.TestCase):
    def run_agent(self, api, folder, provider="openrouter", extra=(), max_turns=20):
        return subprocess.run(
            [str(EXE), "--cwd", folder, "--provider", provider, "--workflow", "agentic",
             "--no-learning", "--no-memory", "--no-skills", "--max-turns", str(max_turns),
             "--prompt", "Create result.txt containing verified, then review and check it.", *extra],
            env=environment(api.url), text=True, capture_output=True, timeout=25)

    def responses(self, provider="openrouter", verification="command"):
        check = "test -f result.txt" if verification == "command" else "Inspect the complete requested document."
        verify = ("run_command", {"command": check}) if verification == "command" else ("read_file", {"path": "result.txt"})
        return [
            reply(provider, "", [("list_files", {})]),
            reply(provider, "", [plan(verification, check), step("implement"),
                                  ("write_file", {"path": "result.txt", "content": "verified"})]),
            reply(provider, "", [step("review"), ("read_file", {"path": "result.txt"})]),
            reply(provider, "", [step("verify"), verify]),
            reply(provider, "", [("finish_task", {"summary": "Created, reviewed and verified result.txt."})]),
        ]

    def test_command_workflow_for_all_providers(self):
        for provider in ("openrouter", "openai", "claude", "gemini"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                with MockAPI(self.responses(provider)) as api:
                    run = self.run_agent(api, folder, provider, ("--approve",))
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                    self.assertEqual(len(api.requests), 5)
                    self.assertEqual(Path(folder, "result.txt").read_text(), "verified")
                    self.assertIn("TASK COMPLETE", run.stdout)
                    names = [t["name"] for t in function_tools(api.requests[0][1], provider)]
                    self.assertIn("workflow", names)
                    last = system(api.requests[-1][1])
                    self.assertIn("Stage: verify", last)
                    self.assertIn("Verification passed: yes", last)
                    self.assertIn("test -f result.txt", last)
                    self.assertIn("Create result.txt containing verified", last)

    def test_inspection_workflow_needs_no_command(self):
        with tempfile.TemporaryDirectory() as folder, MockAPI(self.responses(verification="inspection")) as api:
            run = self.run_agent(api, folder, extra=("--approve",))
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertNotIn("PROPOSED COMMAND", run.stdout)
            self.assertIn("Verification passed: yes", system(api.requests[-1][1]))

    def test_early_tools_and_parallel_mutations_are_rejected(self):
        calls = [plan(), step("implement"),
                 ("write_file", {"path": "one.txt", "content": "unsafe"}),
                 ("write_file", {"path": "two.txt", "content": "unsafe"}),
                 ("delegate_tasks", {"tasks": [{"mode": "write", "paths": ["three.txt"], "prompt": "Write it"}]}),
                 ("finish_task", {"summary": "Premature completion"})]
        with tempfile.TemporaryDirectory() as folder, MockAPI([
            reply("openrouter", "", calls),
            reply("openrouter", "", [step("blocked", "Need to inspect the workspace first.")]),
        ]) as api:
            run = self.run_agent(api, folder, extra=("--approve",))
            self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
            self.assertEqual(len(api.requests), 2)
            self.assertTrue(all(not Path(folder, name).exists() for name in ("one.txt", "two.txt", "three.txt")))
            self.assertEqual(len([r for r in results(api.requests[1][1]) if r.startswith("Error:")]), len(calls))
            self.assertNotIn("TASK COMPLETE", run.stdout)
            self.assertIn("WORKFLOW BLOCKED", run.stdout)

    def test_plain_text_cannot_complete_and_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder, MockAPI([reply("openrouter", "Everything is done.")]) as api:
            run = self.run_agent(api, folder)
            self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
            self.assertEqual(len(api.requests), 3)
            self.assertIn("Workflow incomplete", run.stdout)
            self.assertNotIn("TASK COMPLETE", run.stdout)

    def test_premature_plan_gets_actionable_recovery_and_then_completes(self):
        responses = self.responses()
        responses[0] = reply("openrouter", "", [plan(), ("list_files", {}), plan()])
        with tempfile.TemporaryDirectory() as folder, MockAPI(responses) as api:
            run = self.run_agent(api, folder, extra=("--approve",))
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            initial = results(api.requests[1][1])
            self.assertIn("Call read_file", initial[0])
            self.assertIn("batched with discovery", initial[2])
            self.assertEqual(len(api.requests), 5)
            self.assertIn("TASK COMPLETE", run.stdout)

    def test_failed_check_returns_to_implementation_and_can_recover(self):
        responses = self.responses()
        responses[1] = reply("openrouter", "", [plan(check="test -f fixed.txt"), step("implement"),
                                               ("write_file", {"path": "result.txt", "content": "verified"})])
        responses[3] = reply("openrouter", "", [step("verify"), ("run_command", {"command": "test -f fixed.txt"})])
        responses[4:] = [
            reply("openrouter", "", [("finish_task", {"summary": "Must be rejected"}),
                                      ("write_file", {"path": "fixed.txt", "content": "fixed"})]),
            reply("openrouter", "", [step("review"), ("read_file", {"path": "fixed.txt"})]),
            reply("openrouter", "", [step("verify"), ("run_command", {"command": "test -f fixed.txt"})]),
            reply("openrouter", "", [("finish_task", {"summary": "Repaired and verified."})]),
        ]
        with tempfile.TemporaryDirectory() as folder, MockAPI(responses) as api:
            run = self.run_agent(api, folder, extra=("--approve",))
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("Stage: implement", system(api.requests[4][1]))
            self.assertIn("Verification passed: no", system(api.requests[4][1]))
            self.assertIn("[exit 1]", system(api.requests[4][1]))
            self.assertIn("Error: workflow", "\n".join(results(api.requests[5][1])))
            self.assertIn("Repaired and verified.", run.stdout)

    def test_review_and_verification_require_separate_fresh_evidence(self):
        responses = self.responses(verification="inspection")
        responses[2:4] = [
            reply("openrouter", "", [step("review"), step("verify"), ("read_file", {"path": "result.txt"})]),
            reply("openrouter", "", [step("verify"), ("finish_task", {"summary": "No fresh inspection yet"}),
                                      ("read_file", {"path": "result.txt"})]),
        ]
        with tempfile.TemporaryDirectory() as folder, MockAPI(responses) as api:
            run = self.run_agent(api, folder, extra=("--approve",))
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("Error: workflow", "\n".join(results(api.requests[3][1])))
            self.assertIn("Error: workflow", "\n".join(results(api.requests[4][1])))

    def test_verification_rejects_unplanned_command_and_late_writes(self):
        responses = self.responses()
        responses[3] = reply("openrouter", "", [step("verify"),
                            ("run_command", {"command": "touch should-not-exist"}),
                            ("run_command", {"command": "test -f result.txt"})])
        responses[4] = reply("openrouter", "", [
            ("write_file", {"path": "result.txt", "content": "late"}),
            ("write_file", {"path": "late.txt", "content": "late"}),
            ("finish_task", {"summary": "Verified original content."})])
        with tempfile.TemporaryDirectory() as folder, MockAPI(responses) as api:
            run = self.run_agent(api, folder, extra=("--approve",))
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertFalse(Path(folder, "should-not-exist").exists())
            self.assertFalse(Path(folder, "late.txt").exists())
            self.assertEqual(Path(folder, "result.txt").read_text(), "verified")

    def test_repair_invalidates_passed_verification(self):
        responses = self.responses()
        responses[4:] = [
            reply("openrouter", "", [step("repair", "Review found missing content."),
                                      ("write_file", {"path": "result.txt", "content": "repaired"}),
                                      ("finish_task", {"summary": "Stale verification must not count"})]),
            reply("openrouter", "", [step("blocked", "Need another review and verification.")]),
        ]
        with tempfile.TemporaryDirectory() as folder, MockAPI(responses) as api:
            run = self.run_agent(api, folder, extra=("--approve",))
            self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
            self.assertIn("Stage: implement", system(api.requests[5][1]))
            self.assertIn("Verification passed: no", system(api.requests[5][1]))
            self.assertEqual(Path(folder, "result.txt").read_text(), "repaired")

    def test_verification_result_must_reach_model_before_completion(self):
        for provider in ("openrouter", "claude"):
            responses = self.responses(provider)
            premature = ("finish_task", {"summary": "Cannot assess an unreceived result"})
            responses[3] = reply(provider, "", [step("verify"),
                                 ("run_command", {"command": "test -f result.txt"}), premature])
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder, MockAPI(responses) as api:
                run = self.run_agent(api, folder, provider, ("--approve",))
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertEqual(len(api.requests), 5)
                self.assertTrue(results(api.requests[4][1])[-1].startswith("Error: workflow completion"))

    def test_invalid_plan_does_not_replace_previous_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder, MockAPI([
            reply("openrouter", "", [("list_files", {})]),
            reply("openrouter", "", [plan(), plan(check=""), plan(check="x" * 2001),
                                      step("plan", "x" * 4001, verification="command", check="true"),
                                      step("status")]),
            reply("openrouter", "", [step("blocked", "Argument validation checked.")]),
        ]) as api:
            run = self.run_agent(api, folder)
            self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
            current = system(api.requests[2][1])
            self.assertIn("Stage: plan", current)
            self.assertIn("Check: test -f result.txt", current)
            self.assertIn("Plan: Create result.txt", current)
            self.assertEqual(sum(r.startswith("Error:") for r in results(api.requests[2][1])), 3)

    def test_checkpoint_survives_history_compaction_and_continuation(self):
        for provider in ("openrouter", "claude"):
            responses = self.responses(provider)
            reads = [("read_file", {"path": "large.txt", "offset": offset, "limit": 12000})
                     for offset in range(0, 108000, 12000)]
            responses[2:2] = [reply(provider, "", reads), reply(provider, "Still working.")]
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder, MockAPI(responses) as api:
                Path(folder, "large.txt").write_text("A line for compaction.\n" * 7000)
                run = self.run_agent(api, folder, provider, ("--approve",))
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                self.assertIn("CONTEXT COMPACTED", run.stdout)
                self.assertIn("Original task: Create result.txt containing verified", system(api.requests[-1][1]))
                self.assertIn("Plan: Create result.txt", system(api.requests[-1][1]))
                from tests.test_scaling import assert_tool_pairs
                for _, body in api.requests:
                    assert_tool_pairs(self, body["messages"], provider)

    def test_denied_check_cannot_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "result.txt").write_text("already here")
            responses = self.responses()
            responses[1] = reply("openrouter", "", [plan(), step("implement")])
            responses[4:] = [reply("openrouter", "", [("finish_task", {"summary": "Denied check"})]),
                             reply("openrouter", "", [step("blocked", "The check needs approval.")])]
            with MockAPI(responses) as api:
                run = self.run_agent(api, folder)
                self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
                self.assertNotIn("TASK COMPLETE", run.stdout)
                self.assertIn("Verification passed: no", system(api.requests[4][1]))

    def test_request_budget_stops_incomplete_work_and_keeps_changes(self):
        with tempfile.TemporaryDirectory() as folder, MockAPI(self.responses()) as api:
            run = self.run_agent(api, folder, extra=("--approve",), max_turns=2)
            self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
            self.assertEqual(len(api.requests), 2)
            self.assertEqual(Path(folder, "result.txt").read_text(), "verified")
            self.assertIn("request limit", run.stdout)
            self.assertNotIn("TASK COMPLETE", run.stdout)

    def test_cancel_stops_command_and_next_task_has_fresh_state(self):
        responses = [
            reply("openrouter", "", [("list_files", {})]),
            reply("openrouter", "", [plan(), step("implement"),
                                      ("run_command", {"command": "sleep 30; printf late > cancelled.txt"})]),
            reply("openrouter", "", [step("blocked", "Fresh task inspected.")]),
        ]
        with tempfile.TemporaryDirectory() as folder, MockAPI(responses) as api:
            terminal = Terminal(["--cwd", folder, "--provider", "openrouter", "--workflow", "agentic",
                                 "--approve", "--no-learning", "--no-memory"], environment(api.url))
            try:
                terminal.wait_for("Your terminal.")
                terminal.send("Start the cancellable task.\r")
                terminal.wait_for("PROPOSED COMMAND")
                terminal.send(b"\x1b")
                terminal.wait_for("Cancelled.")
                terminal.send("A fresh task after cancellation.\r")
                terminal.wait_for("WORKFLOW BLOCKED")
                self.assertIn("Stage: discover", system(api.requests[2][1]))
                self.assertNotIn("Start the cancellable task.", system(api.requests[2][1]))
                self.assertFalse(Path(folder, "cancelled.txt").exists())
            finally:
                terminal.close()

    def test_mode_options_and_local_status_without_key(self):
        with tempfile.TemporaryDirectory() as folder:
            for options in (("--workflow", "unknown"), ("--workflow",),
                            ("--workflow", "agentic", "--chat-only")):
                with self.subTest(options=options):
                    run = subprocess.run([str(EXE), "--cwd", folder, *options], env=environment(keys=False),
                                         text=True, capture_output=True, timeout=5)
                    self.assertEqual(run.returncode, 1)
                    self.assertIn("Invalid options", run.stdout)
            run = subprocess.run([str(EXE), "--cwd", folder, "--workflow", "agentic", "--prompt", "/workflow"],
                                 env=environment(keys=False), text=True, capture_output=True, timeout=5)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("Stage: discover", run.stdout)

    def test_new_task_resets_workflow_and_off_restores_direct_mode(self):
        responses = self.responses() + [
            reply("openrouter", "", [step("blocked", "New task starts from discovery.")]),
            reply("openrouter", "Direct answer."),
        ]
        with tempfile.TemporaryDirectory() as folder, MockAPI(responses) as api:
            terminal = Terminal(["--cwd", folder, "--provider", "openrouter", "--workflow", "agentic",
                                 "--approve", "--no-learning"], environment(api.url))
            try:
                terminal.wait_for("Your terminal.")
                terminal.send("Create result.txt.\r")
                terminal.wait_for("TASK COMPLETE")
                terminal.send("A separate task.\r")
                terminal.wait_for("WORKFLOW BLOCKED")
                self.assertIn("Stage: discover", system(api.requests[5][1]))
                self.assertNotIn("Create result.txt with the requested content", system(api.requests[5][1]))
                terminal.send("/workflow off\r")
                terminal.wait_for("Workflow disabled")
                terminal.send("A plain answer please.\r")
                terminal.wait_for("Direct answer.")
                self.assertNotIn("workflow", [t["name"] for t in function_tools(api.requests[6][1], "openrouter")])
            finally:
                terminal.close()


if __name__ == "__main__":
    unittest.main()
