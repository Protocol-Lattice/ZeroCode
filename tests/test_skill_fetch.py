"""Fetch skills through the compiled application using real local Git remotes."""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

from tests.test_agent import EXE, MockAPI, Terminal, environment, reply


class SkillFetchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="zero skill fetch ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repository = self.root / "remote repository"
        self.workspace = self.root / "workspace with spaces"
        self.downloads = self.root / "downloads"
        self.workspace.mkdir()
        self.downloads.mkdir()
        self.env = environment(keys=False)
        self.env["TMPDIR"] = str(self.downloads)
        self.git("init", "-q", "--initial-branch=main", str(self.repository))

    def git(self, *args):
        return subprocess.run(["git", *args], env=self.env, check=True,
                              text=True, capture_output=True, timeout=10)

    def commit(self):
        self.git("-C", str(self.repository), "add", "--all")
        self.git("-C", str(self.repository), "-c", "user.name=Skill Test",
                 "-c", "user.email=skills@example.invalid", "commit", "-qm", "Skills")

    def skill(self, path="skills/review-docs", name="review-docs", marker="REMOTE_INSTRUCTIONS"):
        folder = self.repository / path
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: '{name}'\ndescription: >-\n  Review documentation\n  and examples.\n---\n\n{marker}\n")
        return folder

    def command(self, text, extra=(), env=None):
        return subprocess.run([str(EXE), "--cwd", str(self.workspace), "--no-session-logs",
                               "--prompt", text, *extra], env=env or self.env,
                              capture_output=True, text=True, timeout=30)

    def fetch(self, options="", extra=(), env=None, repository=None):
        remote = repository or self.repository.as_uri()
        return self.command(f'/skills fetch "{remote}" {options}', extra=extra, env=env)

    def assert_clean(self):
        # Apple's Git launcher also writes an unrelated xcrun_db cache here.
        self.assertEqual(list(self.downloads.glob("zero-*")), [])
        self.assertEqual(list(self.workspace.glob(".agents/skills/.zero-fetch.*")), [])

    def test_fetch_collection_preserves_resources_and_loads_after_restart(self):
        source = self.skill()
        self.skill("skills/build-guide", "build-guide")
        (source / "references").mkdir()
        (source / "references/check list.md").write_text("REFERENCE_FROM_REMOTE")
        (source / "assets").mkdir()
        (source / "assets/icon.bin").write_bytes(b"\x00\xff\x80binary asset")
        (source / "empty.txt").touch()
        (source / "check.sh").write_text("#!/bin/sh\ntouch SHOULD_NOT_RUN\n")
        (source / "check.sh").chmod(0o755)
        self.commit()
        result = self.fetch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Fetched 2 skill(s)", result.stdout)
        target = self.workspace / ".agents/skills/review-docs"
        for item in source.rglob("*"):
            if item.is_file():
                copied = target / item.relative_to(source)
                self.assertEqual(copied.read_bytes(), item.read_bytes())
                self.assertEqual(stat.S_IMODE(copied.stat().st_mode), stat.S_IMODE(item.stat().st_mode))
        self.assertFalse((self.workspace / "SHOULD_NOT_RUN").exists())
        self.assertEqual(list(target.rglob(".git")), [])
        catalog = self.command("/skills")
        self.assertEqual(catalog.returncode, 0, catalog.stdout)
        self.assertIn("review-docs", catalog.stdout)
        self.assertIn("build-guide", catalog.stdout)
        with MockAPI([reply("openrouter", calls=[("load_skill", {
                "name": "review-docs", "path": "references/check list.md"})]),
                reply("openrouter", "Applied.")]) as api:
            env = environment(api.url)
            env["TMPDIR"] = str(self.downloads)
            loaded = self.command("Use the fetched skill.", env=env)
            self.assertEqual(loaded.returncode, 0, loaded.stdout + loaded.stderr)
            self.assertIn("REFERENCE_FROM_REMOTE", json.dumps(api.requests[1][1]))
        self.assert_clean()

    def test_selects_subdirectory_and_tag_with_literal_spaces(self):
        skill = self.skill("custom collection/review-docs", marker="TAG_VERSION")
        self.commit()
        self.git("-C", str(self.repository), "tag", "release-one")
        self.skill("custom collection/review-docs", marker="MAIN_VERSION")
        self.commit()
        result = self.fetch('--path "custom collection/review-docs" --ref release-one')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        installed = self.workspace / ".agents/skills/review-docs/SKILL.md"
        self.assertIn("TAG_VERSION", installed.read_text())
        self.assertIn("MAIN_VERSION", (skill / "SKILL.md").read_text())
        self.assert_clean()

    def test_detects_project_collection_and_single_skill_repository(self):
        for path in (".agents/skills/review-docs", "."):
            with self.subTest(path=path):
                self.skill(path)
                self.commit()
                result = self.fetch()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("REMOTE_INSTRUCTIONS", (self.workspace / ".agents/skills/review-docs/SKILL.md").read_text())
                self.assertFalse((self.workspace / ".agents/skills/review-docs/.git").exists())
                shutil.rmtree(self.workspace / ".agents")
                if path != ".":
                    shutil.rmtree(self.repository / ".agents")
        self.assert_clean()

    def test_keeps_existing_skills_and_fetches_new_ones(self):
        self.skill()
        self.skill("skills/new-guide", "new-guide")
        self.commit()
        local = self.workspace / ".agents/skills/review-docs"
        local.mkdir(parents=True)
        (local / "SKILL.md").write_text("KEEP_MY_LOCAL_EDITS")
        result = self.fetch()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Fetched 1 skill(s)", result.stdout)
        self.assertIn("kept 1 existing", result.stdout)
        self.assertEqual((local / "SKILL.md").read_text(), "KEEP_MY_LOCAL_EDITS")
        again = self.fetch()
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("kept 2 existing", again.stdout)
        self.assert_clean()

    def test_github_shorthand_uses_git_and_supports_branch(self):
        self.skill()
        self.commit()
        self.git("-C", str(self.repository), "branch", "test-branch")
        env = self.env.copy()
        # Exercise the exact shorthand URL without depending on Internet access.
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = f"url.{self.repository.as_uri()}.insteadOf"
        env["GIT_CONFIG_VALUE_0"] = "https://github.com/example/skill-library"
        result = self.fetch("--ref test-branch", env=env, repository="example/skill-library")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.workspace / ".agents/skills/review-docs/SKILL.md").is_file())
        self.assert_clean()

    def test_rejects_bad_commands_disabled_skills_and_traversal(self):
        self.skill()
        self.commit()
        for options in ("--path ../outside", "--path /outside", "--path .git",
                        "--ref --upload-pack=evil", "--ref", "--path", "--unknown x",
                        "--path skills --path skills", '--path "unterminated'):
            with self.subTest(options=options):
                result = self.fetch(options)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("Usage:", result.stdout)
                self.assertFalse((self.workspace / ".agents").exists())
        for remote in ("--upload-pack=evil", "ext::touch injected", "../outside", "owner/repo/extra"):
            self.assertNotEqual(self.fetch(repository=remote).returncode, 0)
        self.assertNotEqual(self.fetch(extra=("--no-skills",)).returncode, 0)
        self.assertNotEqual(self.command("/skills fetch").returncode, 0)
        self.assertFalse((self.workspace / ".agents").exists())
        self.assert_clean()

    def test_clone_errors_do_not_leave_temporary_or_installed_files(self):
        self.skill()
        self.commit()
        for options, remote in (("--ref missing-branch", None), ("", (self.root / "missing").as_uri())):
            with self.subTest(options=options):
                result = self.fetch(options, repository=remote)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("repository fetch failed", result.stdout)
                self.assertFalse((self.workspace / ".agents").exists())
                self.assert_clean()

    def test_invalid_metadata_and_symlink_resources_are_never_installed(self):
        folder = self.skill()
        self.skill("skills/wrong-name", "not-the-folder")
        self.skill("skills/large", "large")
        (self.repository / "skills/large/SKILL.md").write_text("x" * 16385)
        self.skill("skills/binary", "binary")
        (self.repository / "skills/binary/SKILL.md").write_bytes(b"\xff\x00")
        (folder / "escape").symlink_to(self.root)
        self.commit()
        result = self.fetch()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("SKILL REJECTED", result.stdout)
        self.assertEqual(list(self.workspace.glob(".agents/skills/*/SKILL.md")), [])
        self.assert_clean()

    def test_source_directory_and_resource_limits_fail_without_partial_install(self):
        source = self.skill()
        (source / "oversized.bin").write_bytes(b"x" * (16 * 1024 * 1024 + 1))
        (self.repository / "linked-skills").symlink_to(self.workspace, target_is_directory=True)
        self.commit()
        for options in ("--path linked-skills", "--path missing-directory", "--path skills/review-docs"):
            with self.subTest(options=options):
                result = self.fetch(options)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse((self.workspace / ".agents/skills/review-docs").exists())
                self.assert_clean()

    def test_ignored_targets_and_symlinked_destinations_are_rejected(self):
        source = self.skill()
        (source / "reference.md").write_text("REFERENCE")
        self.commit()
        for pattern in (".agents/skills/\n", ".agents/skills/review-docs/reference.md\n"):
            with self.subTest(pattern=pattern):
                (self.workspace / ".gitignore").write_text(pattern)
                result = self.fetch()
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse((self.workspace / ".agents/skills/review-docs").exists())
                self.assert_clean()
        (self.workspace / ".gitignore").unlink()
        if (self.workspace / ".agents").exists():
            shutil.rmtree(self.workspace / ".agents")
        outside = self.root / "outside"
        outside.mkdir()
        (self.workspace / ".agents").symlink_to(outside, target_is_directory=True)
        result = self.fetch()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(list(outside.iterdir()), [])
        self.assert_clean()

    def test_tui_refreshes_catalog_and_can_cancel_clone(self):
        self.skill()
        self.commit()
        terminal = Terminal(["--cwd", str(self.workspace), "--no-session-logs"], self.env, rows=40, columns=160)
        try:
            terminal.wait_for("Your terminal.")
            terminal.send(f'/skills fetch "{self.repository.as_uri()}"\r')
            terminal.wait_for("Catalog refreshed.")
            terminal.send("/skill review-docs\r")
            terminal.wait_for("SKILL SELECTED")
        finally:
            terminal.close()
        self.assert_clean()

        shim = self.root / "bin"
        shim.mkdir()
        real_git = shutil.which("git")
        # A controllably stalled clone, while path/ignore Git commands still run.
        (shim / "git").write_text(
            '#!/bin/sh\nfor arg do\n  if [ "$arg" = clone ]; then\n'
            '    printf "%s" "$$" > "$FETCH_TEST_PID"\n    exec sleep 60\n  fi\ndone\n'
            f'exec "{real_git}" "$@"\n')
        (shim / "git").chmod(0o755)
        env = self.env.copy()
        env["PATH"] = str(shim) + os.pathsep + env["PATH"]
        env["FETCH_TEST_PID"] = str(self.root / "clone.pid")
        terminal = Terminal(["--cwd", str(self.workspace), "--no-session-logs"], env, rows=40, columns=160)
        try:
            terminal.wait_for("Your terminal.")
            terminal.send(f'/skills fetch "{self.repository.as_uri()}"\r')
            terminal.wait_for("fetching skills")
            terminal.send(b"\x1b")
            terminal.wait_for("Cancelled.")
            terminal.send("/skills\r")
            terminal.wait_for("review-docs")
            pid = int((self.root / "clone.pid").read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
            self.assert_clean()
        finally:
            restored = terminal.close()
        self.assertEqual(restored, terminal.original)


if __name__ == "__main__":
    unittest.main()
