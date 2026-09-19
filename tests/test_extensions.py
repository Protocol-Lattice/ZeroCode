"""MCP protocol and skills tests against the actual native application."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests import test_agent as agent
from tests.test_agent import EXE, MockAPI, ROOT, Terminal, environment, reply


class ExtensionTests(unittest.TestCase):
    run_agent = agent.AgentTests.run_agent

    def configure_mcp(self, root, names=("demo",), mode=None):
        servers = {}
        for name in names:
            env = {"MCP_TEST_LOG": str(root / f"{name}.jsonl"),
                   "MCP_TEST_STARTED": str(root / f"{name}.pid"),
                   "MCP_TEST_SECRET": "${OPENROUTER_API_KEY}"}
            if mode:
                env["MCP_TEST_MODE"] = mode
            servers[name] = {"command": sys.executable, "args": [str(ROOT / "tests/mcp_server.py"), "an argument with spaces; $(literal)"], "env": env}
        (root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}, indent=2))

    def protocol_log(self, root, name="demo"):
        return [json.loads(line) for line in (root / f"{name}.jsonl").read_text().splitlines()]

    def add_skill(self, root, name="review-docs", marker="FULL_SKILL_INSTRUCTIONS"):
        skill = root / ".agents/skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: >-\n  Review documentation for accuracy\n  and runnable examples.\n---\n\n{marker}\nUse references/checklist.md when needed.\n")
        (skill / "references").mkdir()
        (skill / "references/checklist.md").write_text("REFERENCE_CHECKLIST: verify build commands.\n")
        return skill

    def test_mcp_handshake_pagination_and_persistent_calls_all_providers(self):
        for provider in ("openrouter", "openai", "claude", "gemini"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory(prefix="zero extensions ") as folder:
                root = Path(folder)
                self.configure_mcp(root)
                with MockAPI([
                    reply(provider, calls=[("mcp__demo__0_echo", {"text": "first"})]),
                    reply(provider, calls=[("mcp__demo__1_path_query", {})]),
                    reply(provider, "MCP task complete.")]) as api:
                    result = self.run_agent(api, provider, root, extra=("--mcp", "all", "--approve", "--no-skills"))
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(len(api.requests), 3)
                    definitions = api.requests[0][1]["tools"]
                    names = [(tool if provider == "claude" else tool["function"])["name"] for tool in definitions]
                    self.assertIn("mcp__demo__0_echo", names)
                    self.assertIn("mcp__demo__1_path_query", names)
                    history = json.dumps(api.requests[-1][1]["messages"], ensure_ascii=False)
                    self.assertIn("MCP ECHO 1: first", history)
                    self.assertIn("MCP ECHO 2: query", history)
                    self.assertIn("Structured content:", history)
                    self.assertNotIn("test-key-never-render-me", history + result.stdout + result.stderr)
                events = self.protocol_log(root)
                self.assertEqual(events[0]["argv"], ["an argument with spaces; $(literal)"])
                self.assertEqual(events[0]["secret"], "test-key-never-render-me")
                methods = [event.get("method") for event in events[1:] if event.get("method")]
                self.assertEqual(methods[:4], ["initialize", "notifications/initialized", "tools/list", "tools/list"])
                self.assertEqual(methods.count("initialize"), 1)
                self.assertEqual(methods.count("tools/call"), 2)
                self.assertTrue(any(event.get("id") == "server-ping" and event.get("result") == {} for event in events))
                self.assertTrue(any(event.get("id") == "ready-ping" and event.get("result") == {} for event in events))
                remote_calls = [event["params"]["name"] for event in events if event.get("method") == "tools/call"]
                self.assertEqual(remote_calls, ["echo", "path.query"])
                with self.assertRaises(ProcessLookupError):
                    os.kill(events[0]["pid"], 0)

    def test_mcp_is_opt_in_and_mutations_require_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.configure_mcp(root)
            with MockAPI([reply("openrouter", "Builtin only.")]) as api:
                result = self.run_agent(api, directory=root, extra=("--no-skills",))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(len(api.requests[0][1]["tools"]), 7)
                self.assertFalse((root / "demo.pid").exists())
            with MockAPI([reply("openrouter", calls=[("mcp__demo__0_echo", {"text": "not approved"})]),
                          reply("openrouter", "Denied.")]) as api:
                result = self.run_agent(api, directory=root, extra=("--mcp", "demo", "--no-skills"))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("denied", result.stdout)
                self.assertFalse(any(event.get("method") == "tools/call" for event in self.protocol_log(root)))

    def test_mcp_multiple_servers_and_tool_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.configure_mcp(root, names=("first", "second"))
            with MockAPI([
                reply("claude", calls=[("mcp__first__0_echo", {"text": "tool-error"})]),
                reply("claude", calls=[("mcp__second__2_echo", {"text": "rpc-error"})]),
                reply("claude", "Errors acknowledged.")]) as api:
                result = self.run_agent(api, "claude", root, extra=("--mcp", "all", "--approve", "--no-skills"))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                results = [block for msg in api.requests[-1][1]["messages"] if isinstance(msg.get("content"), list)
                           for block in msg["content"] if block.get("type") == "tool_result"]
                self.assertEqual(len(results), 2)
                self.assertTrue(all(result["is_error"] for result in results))
            for name in ("first", "second"):
                self.assertEqual(sum(event.get("method") == "tools/call" for event in self.protocol_log(root, name)), 1)

    def test_mcp_invalid_config_and_protocol_fail_cleanly(self):
        for mode in ("invalid-json", "malformed", "remote"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                self.configure_mcp(root, mode=mode)
                if mode == "invalid-json":
                    (root / ".mcp.json").write_text("{broken")
                elif mode == "remote":
                    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"remote": {"url": "https://example.invalid/mcp"}}}))
                result = subprocess.run([str(EXE), "--cwd", folder, "--mcp", "all", "--prompt", "hello", "--no-skills"],
                                        env=environment(), capture_output=True, text=True, timeout=10)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("MCP", result.stdout)
                self.assertNotIn("trap:", result.stderr)

    def test_mcp_tui_connect_approve_cancel_and_disconnect(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.configure_mcp(root)
            with MockAPI([
                reply("openrouter", calls=[("mcp__demo__0_echo", {"text": "first"})]),
                reply("openrouter", "MCP approved successfully."),
                reply("openrouter", calls=[("mcp__demo__0_echo", {"text": "wait"})])]) as api:
                terminal = Terminal(["--cwd", folder, "--no-skills"], environment(api.url), rows=40, columns=140)
                try:
                    terminal.wait_for("Your terminal.")
                    terminal.send("/mcp\r")
                    terminal.wait_for("demo")
                    self.assertFalse((root / "demo.pid").exists())
                    terminal.send("/mcp connect demo\r")
                    terminal.wait_for("MCP CONNECTED")
                    terminal.send("Use the MCP echo tool\r")
                    terminal.wait_for("Approve this action?")
                    self.assertFalse(any(event.get("method") == "tools/call" for event in self.protocol_log(root)))
                    terminal.send("y")
                    terminal.wait_for("MCP approved successfully.")
                    terminal.send("Start the waiting tool\r")
                    terminal.wait_for("Approve this action?", after=len(terminal.output))
                    terminal.send("y")
                    terminal.wait_for("MCP request")
                    terminal.send(b"\x1b")
                    terminal.wait_for("Cancelled.")
                    pid = int((root / "demo.pid").read_text())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
                    terminal.send("/mcp disconnect all\r")
                    terminal.wait_for("All MCP servers disconnected.")
                finally:
                    restored = terminal.close()
                self.assertEqual(terminal.process.returncode, 0)
                self.assertEqual(restored, terminal.original)

    def test_skills_metadata_then_instructions_and_reference_all_providers(self):
        for provider in ("openrouter", "openai", "claude", "gemini"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                self.add_skill(root)
                with MockAPI([
                    reply(provider, calls=[("load_skill", {"name": "review-docs"})]),
                    reply(provider, calls=[("load_skill", {"name": "review-docs", "path": "references/checklist.md"})]),
                    reply(provider, "Skill applied.")]) as api:
                    result = self.run_agent(api, provider, root)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    first = json.dumps(api.requests[0][1])
                    self.assertIn("review-docs", first)
                    self.assertIn("Review documentation for accuracy and runnable examples.", first)
                    self.assertNotIn("FULL_SKILL_INSTRUCTIONS", first)
                    self.assertIn("FULL_SKILL_INSTRUCTIONS", json.dumps(api.requests[1][1]))
                    self.assertIn("REFERENCE_CHECKLIST", json.dumps(api.requests[2][1]))

    def test_skills_ignore_rules_and_reference_confinement(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            skill = self.add_skill(root)
            self.add_skill(root, "ignored-skill", "DO_NOT_READ_IGNORED_SKILL")
            (root / ".gitignore").write_text(".agents/skills/ignored-skill/\n.agents/skills/review-docs/references/private.md\n")
            (skill / "references/private.md").write_text("DO_NOT_READ_PRIVATE_REFERENCE")
            (root / "outside.txt").write_text("DO_NOT_READ_OUTSIDE_SKILL")
            (skill / "references/link.md").symlink_to(root / "outside.txt")
            with MockAPI([reply("openrouter", calls=[
                ("load_skill", {"name": "ignored-skill"}),
                ("load_skill", {"name": "review-docs", "path": "references/private.md"}),
                ("load_skill", {"name": "review-docs", "path": "../../outside.txt"}),
                ("load_skill", {"name": "review-docs", "path": "references/link.md"})]), reply("openrouter", "Protected.")]) as api:
                result = self.run_agent(api, directory=root)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                first = json.dumps(api.requests[0][1])
                self.assertNotIn("ignored-skill", first)
                self.assertNotIn("DO_NOT_READ", json.dumps(api.requests) + result.stdout)
                results = [msg["content"] for msg in api.requests[-1][1]["messages"] if msg["role"] == "tool"]
                self.assertTrue(all(content.startswith("Error:") for content in results))

    def test_skill_selection_reload_disable_and_custom_directory(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory(prefix="extra skills ") as custom:
            root = Path(folder)
            self.add_skill(root)
            external = self.add_skill(Path(custom), name="external-guide", marker="EXTERNAL_GUIDE")
            with MockAPI([reply("openrouter", "Selected instructions seen.")]) as api:
                result = self.run_agent(api, directory=root, extra=("--skills-dir", str(external.parent), "--skill", "external-guide"))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("EXTERNAL_GUIDE", json.dumps(api.requests[0][1]))
            with MockAPI([reply("openrouter", "Skills disabled.")]) as api:
                result = self.run_agent(api, directory=root, extra=("--no-skills",))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("review-docs", json.dumps(api.requests[0][1]))
                self.assertEqual(len(api.requests[0][1]["tools"]), 7)
            with MockAPI([reply("openrouter", "TUI skill selected.")]) as api:
                terminal = Terminal(["--cwd", folder], environment(api.url), rows=40, columns=140)
                try:
                    terminal.wait_for("Your terminal.")
                    terminal.send("/skills\r")
                    terminal.wait_for("review-docs")
                    terminal.send("/skill review-docs\r")
                    terminal.wait_for("SKILL SELECTED")
                    terminal.send("Review the docs\r")
                    terminal.wait_for("TUI skill selected.")
                    self.assertIn("FULL_SKILL_INSTRUCTIONS", json.dumps(api.requests[0][1]))
                    terminal.send("/skill off\r")
                    terminal.wait_for("Selected skill cleared.")
                    self.add_skill(root, "new-skill")
                    terminal.send("/skills reload\r")
                    terminal.wait_for("new-skill")
                finally:
                    terminal.close()


if __name__ == "__main__":
    unittest.main()
