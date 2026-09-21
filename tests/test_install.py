"""Exercise the installed command without changing the user's installation."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import unittest

from tests.test_agent import EXE, MockAPI, ROOT, environment, reply


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="zero install ")
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name).resolve()
        self.prefix = self.folder / "prefix with 'quotes' $literal `literal`"
        self.workspace = self.folder / "workspace"
        self.workspace.mkdir()
        self.env = environment()
        self.env.pop("ZERO_COMPILER", None)
        self.env["ZDOTDIR"] = str(self.folder / "shell config")
        self.env["SHELL"] = "/bin/zsh"

    def install(self, *args, binary=EXE, configure=False, script=ROOT / "install.sh", input=None):
        command = ["sh", str(script)] if input is None else ["sh", "-s", "--"]
        command += ["--prefix", str(self.prefix)]
        if binary is not None:
            command += ["--binary", str(binary)]
        if not configure:
            command += ["--no-modify-path"]
        return subprocess.run([*command, *args], input=input, cwd=self.workspace,
                              env=self.env, text=True, capture_output=True, timeout=30)

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def run_installed(self, *args, env=None):
        command_env = (env or self.env).copy()
        command_env["PATH"] = str(self.prefix / "bin") + os.pathsep + command_env["PATH"]
        return subprocess.run(["sh", "-c", 'exec zero-code "$@"', "test", *args],
                              cwd=self.workspace, env=command_env, text=True,
                              capture_output=True, timeout=15)

    def write_executable(self, path, contents):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nset -eu\n" + contents)
        path.chmod(0o755)
        return path

    def test_installed_native_binary_runs_from_path_with_quoted_prefix(self):
        # A downloaded binary may be supplied as a bare filename in the cwd.
        downloaded = self.workspace / "downloaded-zero"
        shutil.copy2(EXE, downloaded)
        self.assert_ok(self.install(binary=downloaded.name))
        downloaded.unlink()
        result = self.run_installed("--version")
        self.assert_ok(result)
        self.assertIn("zero-code 0.1.0", result.stdout)
        self.assertEqual((self.prefix / "libexec/zero-code/zero-code").read_bytes(), EXE.read_bytes())
        self.assertFalse((Path(self.env["ZDOTDIR"]) / ".zshrc").exists())

    def test_requests_reexec_installed_binary_and_keep_workspace(self):
        self.assert_ok(self.install())
        other = self.folder / "other workspace"
        other.mkdir()
        for directory, args in ((self.workspace, ()), (other, ("--cwd", str(other)))):
            with self.subTest(directory=directory):
                (directory / "hello.txt").write_text("installed workspace marker")
                with MockAPI([
                    reply("openrouter", calls=[("read_file", {"path": "hello.txt"})]),
                    reply("openrouter", "Installed request complete.")
                ]) as api:
                    result = self.run_installed("--prompt", "Read hello.txt 'quoted' $literal",
                                                "--no-skills", *args, env=environment(api.url))
                    self.assert_ok(result)
                    self.assertIn("Installed request complete.", result.stdout)
                    self.assertEqual(len(api.requests), 2)
                    self.assertEqual(api.requests[-1][1]["messages"][-1]["content"],
                                     "installed workspace marker")
                    self.assertIn("Read hello.txt 'quoted' $literal",
                                  str(api.requests[0][1]["messages"]))

    def test_binary_only_install_reports_missing_evolution_bundle(self):
        self.assert_ok(self.install())
        result = self.run_installed("--self-evolve", "--version")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires a source install", result.stderr)

    def test_reinstall_and_uninstall_leave_other_commands_and_config(self):
        self.assert_ok(self.install(configure=True))
        self.assert_ok(self.install(configure=True))
        profile = Path(self.env["ZDOTDIR"]) / ".zshrc"
        self.assertEqual(profile.read_text().count("# zero-code"), 1)
        other = self.prefix / "bin/unrelated"
        other.write_text("keep me")
        self.assert_ok(self.install("--uninstall", binary=None))
        self.assertFalse((self.prefix / "bin/zero-code").exists())
        self.assertFalse((self.prefix / "libexec/zero-code").exists())
        self.assertEqual(other.read_text(), "keep me")
        self.assertTrue(profile.exists())
        self.assert_ok(self.install("--uninstall", binary=None))

    def test_profile_makes_command_available_in_new_shell(self):
        profile = Path(self.env["ZDOTDIR"]) / ".zshrc"
        profile.parent.mkdir(parents=True)
        profile.write_text("# existing user settings\n")
        self.assert_ok(self.install(configure=True))
        self.assertTrue(profile.read_text().startswith("# existing user settings\n"))
        for shell in ("sh", "bash", "zsh"):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell):
                result = subprocess.run([shell, "-c", '. "$1"; zero-code --version',
                                         "test", str(profile)], cwd=self.workspace,
                                        env=self.env, text=True, capture_output=True, timeout=10)
                self.assert_ok(result)
                self.assertIn("zero-code", result.stdout)

    def test_failed_binary_validation_keeps_previous_installation(self):
        self.assert_ok(self.install())
        launcher = self.prefix / "bin/zero-code"
        previous = launcher.read_bytes()
        broken = self.write_executable(self.folder / "broken", "exit 42\n")
        result = self.install(binary=broken)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot run on this machine", result.stderr)
        self.assertEqual(launcher.read_bytes(), previous)
        self.assert_ok(self.run_installed("--version"))

    def source_fixture(self):
        source = self.folder / "source"
        source.mkdir()
        shutil.copy2(ROOT / "install.sh", source / "install.sh")
        (source / "zero.toml").write_text('[package]\nname = "fixture"\n')
        (source / "zero.graph").write_text("fixture\n")
        setup_log = self.folder / "setup.log"
        build_log = self.folder / "build.log"
        self.write_executable(source / "scripts/setup-zero.sh", """
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mkdir -p "$project_dir/.tools/bin"
printf '#!/bin/sh\\nexit 0\\n' > "$project_dir/.tools/bin/zero"
chmod 755 "$project_dir/.tools/bin/zero"
printf '16777216\\n' > "$project_dir/.tools/compiler-frame-limit"
printf 'setup\\n' >> """ + shlex.quote(str(setup_log)) + "\n")
        self.write_executable(source / "scripts/build.sh", """
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
test -x "$project_dir/.tools/bin/zero"
mkdir -p "$project_dir/dist"
cp """ + shlex.quote(str(EXE)) + ' "$project_dir/dist/zero-code"\n' +
                              "printf 'build\\n' >> " + shlex.quote(str(build_log)) + "\n")
        return source, setup_log, build_log

    def test_checkout_builds_and_bootstraps_compiler_only_once(self):
        source, setup_log, build_log = self.source_fixture()
        for _ in range(2):
            self.assert_ok(self.install(binary=None, script=source / "install.sh"))
        self.assertEqual(setup_log.read_text(), "setup\n")
        self.assertEqual(build_log.read_text(), "build\nbuild\n")
        shutil.rmtree(source)
        self.assert_ok(self.run_installed("--version"))

    def test_existing_compiler_is_refreshed_for_large_file_buffers(self):
        source, setup_log, _ = self.source_fixture()
        self.write_executable(source / ".tools/bin/zero", "exit 0\n")
        self.assert_ok(self.install(binary=None, script=source / "install.sh"))
        self.assertEqual(setup_log.read_text(), "setup\n")

    def test_compiler_buffer_patch_is_repeatable_and_rejects_unknown_definition(self):
        header = self.folder / "zero.h"
        header.write_text("#define Z_DIRECT_FRAME_LOCAL_LIMIT_BYTES 131072u\n")
        command = ["node", str(ROOT / "scripts/configure-zero-buffers.mjs"), str(header)]
        for _ in range(2):
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
            self.assert_ok(result)
            self.assertEqual(header.read_text(), "#define Z_DIRECT_FRAME_LOCAL_LIMIT_BYTES 16777216u\n")
        header.write_text("#define Z_DIRECT_FRAME_LOCAL_LIMIT_BYTES 1u\n")
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("definition changed", result.stderr)
        self.assertEqual(header.read_text(), "#define Z_DIRECT_FRAME_LOCAL_LIMIT_BYTES 1u\n")

    def test_piped_installer_downloads_requested_revision(self):
        source, setup_log, _ = self.source_fixture()
        archive = self.folder / "source.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(source, arcname="zero-code-tui-fixture")
        download_log = self.folder / "download.log"
        mock_bin = self.folder / "mock-bin"
        self.write_executable(mock_bin / "curl", """
printf '%s\\n' "$@" > """ + shlex.quote(str(download_log)) + """
while [ "$#" -gt 0 ]; do
    if [ "$1" = -o ]; then
        cp """ + shlex.quote(str(archive)) + """ "$2"
        exit
    fi
    shift
done
exit 1
""")
        self.env["PATH"] = str(mock_bin) + os.pathsep + self.env["PATH"]
        result = self.install("--ref", "v0.1.0", binary=None,
                              input=(ROOT / "install.sh").read_text())
        self.assert_ok(result)
        self.assertIn("https://codeload.github.com/Protocol-Lattice/ZeroCode/tar.gz/v0.1.0",
                      download_log.read_text())
        self.assertTrue(setup_log.exists())
        self.assert_ok(self.run_installed("--version"))

    def test_invalid_options_and_unsupported_os_do_not_install(self):
        for args in (("--prefix", "relative"), ("--prefix", "/tmp/with:colon"),
                     ("--ref", "v0.1.0"), ("--prefix",), ("--unknown",)):
            with self.subTest(args=args):
                self.assertNotEqual(self.install(*args).returncode, 0)
                self.assertFalse((self.prefix / "bin/zero-code").exists())
        mock_bin = self.folder / "mock-bin"
        self.write_executable(mock_bin / "uname", "printf 'FreeBSD\\n'\n")
        self.env["PATH"] = str(mock_bin) + os.pathsep + self.env["PATH"]
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Only Linux and macOS", result.stderr)


if __name__ == "__main__":
    unittest.main()
