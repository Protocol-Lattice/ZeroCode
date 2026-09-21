"""Executable graph evolution: production compiler, real tools, offline oracle."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.learning_program import (ProgramStore, Rejected, clean_environment,
                                      copy_package, replay_gate, validate_patches)
from tests.test_agent import ROOT, MockAPI, environment, reply
from tests.test_learning import read_route
from tests.test_parallel import results


class ProgramAdmissionTests(unittest.TestCase):
    def test_evidence_qualifies_outcomes_and_pins_the_open_journal_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = "e-" + "a" * 32 + ".jsonl"
            journal = root / ".zero-agent/program-learning/experiences" / identity
            journal.parent.mkdir(parents=True)
            outcome = {"complete": True, "mode": "adaptive", "status": "completed",
                       "program_version": "base", "pattern": "b" * 64,
                       "read_total": 55000, "invalid_repeats": 0, "metrics": {"tests_failed": 0}}
            request = {"parent": "base", "workspace": str(root), "experience": identity,
                       "patches": [{"function": "learningWindowValue", "body": "return 12000"}]}
            store = object.__new__(ProgramStore)

            def write(changes):
                journal.write_text(json.dumps({"schema": 1, "seq": 0, "experience": identity,
                                               "event": "outcome", "data": {**outcome, **changes}}) + "\n")

            write({})
            evidence = store.evidence(request)
            with journal.open("a") as output:
                output.write('{"event":"program_evaluation"}\n')
            self.assertEqual(hashlib.sha256(journal.read_bytes()[:evidence["bytes"]]).hexdigest(), evidence["sha256"])
            for changes in ({"complete": False}, {"mode": "frozen"}, {"status": "cancelled"},
                            {"program_version": "stale"}, {"metrics": {"tests_failed": 1}}, {"read_total": 0}):
                with self.subTest(changes=changes):
                    write(changes)
                    with self.assertRaises(Rejected):
                        store.evidence(request)

    def test_function_bodies_cannot_change_the_validator_or_acquire_capabilities(self):
        self.assertEqual(validate_patches([{"function": "learningWindowValue",
                                          "body": "if policy == 0 { return 11000 }\nreturn 12000"}])[0]["function"],
                         "learningWindowValue")
        for name, body in (("learningReplayGate", "return true"),
                           ("learningWindowValue", "process.exit(0)\nreturn 12000"),
                           ("learningWindowValue", "while true { }\nreturn 12000"),
                           ("learningWindowValue", "return learningWindowValue(policy)"),
                           ("learningRetryValue", 'return "2"')):
            with self.subTest(name=name, body=body), self.assertRaises(Rejected):
                validate_patches([{"function": name, "body": body}])

    def test_validation_environment_excludes_credentials_and_process_hooks(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "private", "LD_PRELOAD": "hook",
                                     "PYTHONPATH": "hook", "HTTPS_PROXY": "proxy",
                                     "ZERO_LEARNING_PROGRAM_ROOT": "recursive"}):
            env = clean_environment(Path("/tmp/validation"))
        for key in ("OPENAI_API_KEY", "LD_PRELOAD", "PYTHONPATH", "HTTPS_PROXY",
                    "ZERO_LEARNING_PROGRAM_ROOT"):
            self.assertNotIn(key, env)

    def test_no_op_regression_and_incorrect_coverage_cannot_pass_replay(self):
        row = {"success": True, "tools": 7, "requests": 8, "cost_units": 100,
               "oracle_errors": []}
        for candidate in (row, {**row, "tools": 8}, {**row, "success": False},
                          {**row, "tools": 5, "cost_units": 90, "oracle_errors": ["lost bytes"]}):
            with self.subTest(candidate=candidate), tempfile.TemporaryDirectory() as directory:
                with patch("scripts.learning_program.validation_cases", return_value=[type("Case", (), {
                        "identity": "one", "family": "read"})()]), patch(
                        "scripts.learning_program.run_case", side_effect=[row, candidate]):
                    with self.assertRaises(Rejected):
                        replay_gate(Path("before"), Path("after"), Path(directory), {})

    def test_snapshot_copy_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "native").mkdir()
            (root / "zero.toml").write_text("manifest")
            (root / "zero.graph").write_text("graph")
            (root / "src/escape.0").symlink_to(ROOT / "src/main.0")
            with self.assertRaises(Rejected):
                copy_package(root, root / "copy")

    def test_store_refuses_a_symlink_before_creating_external_state(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as external:
            root = Path(temporary)
            (root / ".zero-agent").symlink_to(external, target_is_directory=True)
            with self.assertRaises(Rejected):
                ProgramStore(root)
            self.assertEqual(list(Path(external).iterdir()), [])


class ProgramEvolutionTests(unittest.TestCase):
    def test_global_install_graph_rewrite_restart_rejection_and_rollback(self):
        with tempfile.TemporaryDirectory(prefix="zero-program-") as temporary:
            temporary = Path(temporary)
            root = temporary / "checkout"
            copy_package(ROOT, root)
            (root / "scripts").mkdir()
            for name in ("learning_program.py", "learning_benchmark.py", "build.sh"):
                shutil.copyfile(ROOT / "scripts" / name, root / "scripts" / name)
            (root / ".tools/bin").mkdir(parents=True)
            shutil.copyfile(ROOT / ".tools/bin/zero", root / ".tools/bin/zero")
            (root / ".tools/bin/zero").chmod(0o700)
            (root / ".tools/compiler-frame-limit").write_text("16777216\n")
            shutil.copyfile(ROOT / "install.sh", root / "install.sh")
            checkout = root
            prefix = temporary / "prefix with 'quotes' $literal `literal`"

            def install():
                return subprocess.run(["sh", str(checkout / "install.sh"), "--prefix", str(prefix),
                                       "--no-modify-path"], env=environment(), capture_output=True,
                                      text=True, timeout=180)

            installed = install()
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            bundles = prefix / "libexec/zero-code/programs"
            root, = bundles.iterdir()
            workspace = temporary / "workspace"
            workspace.mkdir()
            content = "x" * 55000
            (workspace / "book.txt").write_text(content)
            source_graph = (root / "zero.graph").read_bytes()
            launcher = [str(prefix / "bin/zero-code"), "--self-evolve"]

            def launch(arguments, endpoint=None, timeout=900):
                return subprocess.run([*launcher, *arguments], env=environment(endpoint),
                                      capture_output=True, text=True, timeout=timeout)

            def read(extra=()):
                with MockAPI([read_route]) as api:
                    result = launch(["--cwd", str(workspace), "--parallel", "1",
                                     "--no-memory", "--no-skills", "--no-session-logs", *extra,
                                     "--prompt", "Read book.txt completely"], api.url)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    observations = [results(body, "openrouter")[-1][1] for _, body in api.requests[1:]]
                    self.assertEqual("".join(json.loads(value)["content"] for value in observations), content)
                    return len(observations), result

            reads, first_result = read()
            self.assertEqual(reads, 7)
            store = ProgramStore(root)
            first = store.state()["head"]
            self.assertNotEqual(first, "base", first_result.stdout + first_result.stderr + json.dumps(store.history()))
            generation = store.generation(first)
            self.assertNotEqual((generation / "zero.graph").read_bytes(), source_graph)
            self.assertEqual((root / "zero.graph").read_bytes(), source_graph)
            evaluation = json.loads((generation / "evaluation.json").read_text())
            self.assertTrue(evaluation["accepted"])
            self.assertIn("return 12000", evaluation["changes"][0]["after"])
            totals = evaluation["replay"]["totals"]
            self.assertLess(totals["candidate"]["tools"], totals["baseline"]["tools"])
            self.assertLess(totals["candidate"]["cost_units"], totals["baseline"]["cost_units"])
            self.assertGreaterEqual(totals["candidate"]["success"], totals["baseline"]["success"])
            reinstalled = install()
            self.assertEqual(reinstalled.returncode, 0, reinstalled.stdout + reinstalled.stderr)
            self.assertEqual(list(bundles.iterdir()), [root])
            self.assertEqual(store.state()["head"], first)
            shutil.rmtree(checkout)
            self.assertEqual(read()[0], 5)
            self.assertEqual(read(("--no-learning",))[0], 7)
            self.assertEqual(store.state()["head"], first)

            # A real, well-typed executable regression fails the independent
            # oracle and cannot move HEAD, even with valid task evidence.
            journal_root = workspace / ".zero-agent/program-learning/experiences"
            journals = [path for path in journal_root.iterdir()
                        if any(row.get("event") == "outcome" and row["data"].get("program_version") == first
                               for row in map(json.loads, path.read_text().splitlines()))]
            self.assertTrue(journals)
            request = {"action": "evolve", "parent": first, "workspace": str(workspace),
                       "experience": journals[-1].name,
                       "patches": [{"function": "learningWindowValue", "body": "if policy % 2 == 1 { return 12000 }\nreturn 8192"}]}
            response = subprocess.run(["python3", str(root / "scripts/learning_program.py"),
                                       "--root", str(root), "--request", json.dumps(request)],
                                      capture_output=True, text=True, timeout=600)
            self.assertFalse(json.loads(response.stdout)["accepted"], response.stderr)
            self.assertEqual(store.state()["head"], first)
            self.assertEqual(len(list(store.generations.iterdir())), 2)

            stale = {**request, "parent": "base"}
            with store.locked(), self.assertRaisesRegex(Rejected, "Stale"):
                store.evolve(stale)
            invalid = reply("openrouter", calls=[("read_file", {"path": 7})])

            def invalid_task(extra=()):
                with MockAPI([invalid]) as api:
                    result = launch(["--cwd", str(workspace), "--no-memory", "--no-skills",
                                     *extra, "--prompt", "Invalid argument task"], api.url)
                    self.assertNotEqual(result.returncode, 0)
                    return len(api.requests), result

            count, retry_result = invalid_task()
            self.assertEqual(count, 3)
            second = store.state()["head"]
            self.assertNotEqual(second, first, retry_result.stdout + retry_result.stderr)
            self.assertEqual(invalid_task(("--learning-frozen",))[0], 2)
            rollback = launch(["--learning-rollback", "base"])
            self.assertEqual(rollback.returncode, 0, rollback.stdout + rollback.stderr)
            self.assertEqual(read(("--learning-frozen",))[0], 7)
            self.assertEqual(store.state()["head"], "base")
            restored = launch(["--learning-rollback", first])
            self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
            self.assertEqual(read(("--learning-frozen",))[0], 5)
            self.assertEqual(invalid_task(("--learning-frozen",))[0], 3)
            self.assertTrue(store.generation(first).is_dir())

            # HEAD never executes artifacts changed after the acceptance gate.
            with (generation / "zero-code").open("ab") as binary:
                binary.write(b"tampered")
            corrupt = launch(["--version"])
            self.assertNotEqual(corrupt.returncode, 0)
            self.assertIn("changed after validation", corrupt.stdout)
            self.assertEqual(launch(["--learning-rollback", "base"]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
