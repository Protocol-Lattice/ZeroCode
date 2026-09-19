# zero-coding-tui

A terminal user interface (TUI) framework built with **Zero** — a custom programming language designed for creating high-performance terminal applications.

## Overview

`zero-coding-tui` is a minimalistic TUI toolkit written in the **Zero** language. It provides a lightweight foundation for building interactive command-line interfaces, chat agents, and other terminal-based applications. The project includes:

- **Zero compiler** – compiles Zero source into a standalone binary (`zero-coding`)
- **Native backend** – uses `native/exit.h` for low-level terminal operations
- **Test suite** – black-box tests for the compiled Zero executable
- **Build system** – `Makefile` for cross-compilation targets

## Features

- **Lightweight** – single binary, no external dependencies beyond standard library
- **Zero runtime** – compiled directly to native code via the Zero compiler
- **Agent integration** – supports multiple LLM providers (OpenAI, Claude, Gemini, OpenRouter)
- **Cross-compilation** – targets Linux musl x64, host development, etc.
- **Self-contained** – produces a single distributable binary

## Project Structure

```
zero-coding-tui/
├── src/main.0          # Main Zero source (TUI framework)
├── tests/test_agent.py # Unit tests for the Zero binary
├── zero.c              # Compiled entry point (generated)
├── zero.toml           # Package manifest
├── zero.graph          # Dependency graph
├── Makefile            # Build automation
├── scripts/
│   ├── setup-zero.sh   # Setup Zero compiler environment
│   └── build.sh         # Build the Zero binary
└── README.md           # This file
```

## Building

### Prerequisites

- **Node.js ≥ 24** (required by Zero's standard library)
- **Git** (for fetching the Zero source)

### Compile

```bash
# Using the Makefile (recommended)
make

# Or manually via the Zero compiler
./scripts/build.sh
```

The build creates `dist/zero-coding` (Linux musl x64) and copies it to `.tools/bin/zero`.

### Running

```bash
# Run the built binary
./dist/zero-coding

# Or invoke via the Zero compiler directly
./zero-coding --help
```

### Testing

```bash
make test
# or
python3 -m unittest discover -s tests -v
```

## Quick Start

1. **Clone & install dependencies**
   ```bash
   git clone <repo>
   cd zero-coding-tui
   ```

2. **Compile the binary**
   ```bash
   make
   ```

3. **Run the TUI**
   ```bash
   ./dist/zero-coding
   ```

4. **Test the implementation**
   ```bash
   make test
   ```

## API Reference

### Providers

The Zero compiler supports four LLM providers out of the box:

| Provider | Model | Endpoint |
|----------|-------|----------|
| OpenAI   | gpt-5.4 | `https://api.openai.com/v1/chat/completions` |
| Claude   | claude-sonnet-4-6 | `https://api.anthropic.com/v1/messages` |
| Gemini   | gemini-3.8-flash | `https://generativelanguage.googleapis.com/v1beta/openai/chat/completions` |
| OpenRouter | free | `https://openrouter.ai/api/v1/chat/completions` |

### Common Flags

- `--provider <name>` – Select the AI provider (openai, claude, gemini, openrouter)
- `--model <model>` – Choose a model (default: provider-specific)
- `--max-turns <N>` – Maximum conversation turns
- `--cwd <dir>` – Working directory for the agent

## Example Usage

```bash
# Connect to OpenAI with GPT-4
./dist/zero-coding --provider openai --model gpt-4

# Connect to Claude
./dist/zero-coding --provider claude --model claude-sonnet-4-6

# Connect to Gemini
./dist/zero-coding --provider gemini --model gemini-3.8-flash

# Connect to OpenRouter (free tier)
./dist/zero-coding --provider openrouter --model any/custom-model-id
```

## MCP servers

The native Zero application connects to MCP servers over **stdio**. Define servers
in `.mcp.json` in the workspace:

```json
{
  "mcpServers": {
    "my-tools": {
      "command": "/absolute/path/to/mcp-server",
      "args": ["--example-option"],
      "env": {"SERVICE_TOKEN": "${SERVICE_TOKEN}"}
    }
  }
}
```

Replace the command and arguments with those required by your server. Commands
are executed directly, with argument boundaries preserved. `${VARIABLE}` expands
an existing environment variable in `command`, `args`, `cwd`, or `env` values.
The default server working directory is the workspace; `cwd` can override it.
Keep credentials in environment variables rather than literal configuration values.

In the TUI:

```text
/mcp                       List servers and discovered tools
/mcp connect my-tools      Start a server and discover its tools
/mcp connect all           Connect all configured servers
/mcp disconnect my-tools   Stop one server
/mcp disconnect all        Stop all servers
/mcp reload                Disconnect and reread configuration
```

Connections are opt-in. Server processes stay alive across tool calls and stop
when disconnected or when the app exits. Discovered tools work with all four
providers and require the same approval as shell commands. `Esc` cancels an active
MCP request and disconnects its server. Reconnect a server to refresh changed tools.

For a noninteractive task:

```sh
./zero-coding --mcp all --approve --prompt "Use the configured tools to complete the task."
```

`--mcp-config PATH` selects another JSON configuration file. `--approve` authorizes
MCP calls, file edits, and shell commands for that run; without it, noninteractive
MCP calls are denied. Configuration supports up to four servers, 128 tools in a
64 KiB catalog, 64 KiB protocol messages, and 16 KiB text tool results. Requests
time out after 60 seconds. This client implements MCP tool discovery and calls;
remote HTTP endpoints need a separately configured stdio bridge. Server-provided
sampling, elicitation, resources, and prompts are not exposed as client features.

## Skills

Add project skills under `.agents/skills/NAME/SKILL.md`:

```markdown
---
name: review-docs
description: Review documentation for accuracy and runnable examples.
---

Check the requested documentation against the implementation.
Read references/checklist.md if a detailed checklist is needed.
Summarize the findings and stop when the requested review is complete.
```

The frontmatter name must match its directory. Plain, quoted, and folded
descriptions are supported. Optional references, scripts, and assets live beside
`SKILL.md`. Only skill metadata enters the model's initial skill catalog;
the model calls `load_skill` when it needs the instructions. Its optional `path`
argument reads a text reference relative to that skill's directory.

```text
/skills             Show available skills
/skills reload      Rescan skill directories
/skill review-docs  Apply a skill to the session
/skill off          Clear the selected skill
```

User skills are discovered in `$XDG_CONFIG_HOME/zero-coding/skills`, or
`~/.config/zero-coding/skills` when `XDG_CONFIG_HOME` is unset. Use
`--skills-dir PATH` to replace that user collection with another directory;
project skills take precedence when names collide. `--skill NAME` selects a skill
at startup, and `--no-skills` disables discovery.

Discovery is limited to 32 valid skills and a bounded catalog. Skill files and
text references must be UTF-8 and at most 16 KiB. Workspace `.gitignore` rules,
path checks, and symlink restrictions also apply to skills. Loading a skill does
not run its scripts or grant approvals: scripts use the existing approved
`run_command` flow. Skill instructions remain subordinate to the user's task.

Both MCP and skills are implemented in Zero and exported into `src/main.0` from
the canonical `zero.graph` program graph.

## Development

- **Source**: `src/main.0` – The core TUI framework written in Zero
- **Tests**: `tests/test_agent.py` – Black-box tests exercising the compiled binary
- **Build**: `Makefile` – Handles compilation and distribution

## License

This project is licensed under the MIT License (see `LICENSE`).

## Contributing

Contributions are welcome! Please ensure:
- All Zero source remains readable and well-documented
- Tests cover edge cases and provide meaningful assertions
- Build system continues to work across supported platforms

## Acknowledgments

- **Zero Language** – https://github.com/ZeroLang/zero
- **std.term.terminal** – The underlying terminal abstraction
- **OpenAI**, **Claude**, **Gemini**, **OpenRouter** – Provided LLM APIs

---

*Last updated: 2026-09-19*
