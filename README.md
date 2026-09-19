# zero-coding

A terminal coding assistant built with **Zero**, with native file tools, streaming model responses, MCP servers, and project skills.

## Overview

`zero-coding` runs coding tasks in your current workspace. The project includes:

- **Zero compiler** – compiles Zero source into a standalone binary (`zero-coding`)
- **Native transport** – a small C worker uses system libcurl; Zero parses responses and controls tool execution
- **Test suite** – black-box tests for the compiled Zero executable
- **Build system** – `Makefile` for cross-compilation targets

## Features

- **Lightweight** – native executable using the system libcurl for HTTP/TLS
- **Native execution** – compiled directly to native code via the Zero compiler
- **Agent integration** – supports multiple LLM providers (OpenAI, Claude, Gemini, OpenRouter)
- **Streaming** – incremental text and tool-call deltas for all four providers, with cancellation
- **Large files** – UTF-8 range reads and approved edits of files up to 64 MiB
- **Bounded context** – automatic shortening of old tool output and a digest of older exchanges
- **Host builds** – Linux and macOS builds using the pinned Zero compiler and system libcurl

## Project Structure

```
zero-coding/
├── src/main.0          # Application state, task loop, approvals, startup
├── src/buffers.0       # Bounded buffers, JSON and UTF-8 helpers
├── src/workspace.0     # Path checks, range reads and atomic file edits
├── src/providers.0     # Provider configuration and request/response mapping
├── src/streaming.0     # SSE parsing and incremental message assembly
├── src/context.0       # History, tool results and automatic compaction
├── src/mcp.0           # Persistent MCP stdio connections
├── src/skills.0        # Skill discovery and loading
├── src/ui.0            # Terminal rendering and keyboard input
├── native/http_stream.c # HTTP transport worker
├── tests/              # Black-box tests of the compiled binary
├── zero.toml           # Package manifest
├── zero.graph          # Canonical Zero program graph
├── Makefile            # Build automation
├── install.sh          # Linux/macOS installer for the zero-coding command
├── scripts/
│   ├── setup-zero.sh   # Setup Zero compiler environment
│   └── build.sh         # Build the Zero binary
└── README.md           # This file
```

## Install globally on Linux or macOS

The installed command is **`zero-coding`**. From this checkout, run:

```sh
make install
```

Or download and run the installer without cloning the repository:

```sh
curl -fsSL https://raw.githubusercontent.com/Protocol-Lattice/zero-coding/main/install.sh | sh
```

The installer builds for your machine, installs into `~/.local`, and adds
`~/.local/bin` to your Bash, Zsh, or POSIX shell startup files when needed. Open a
new terminal or run the printed `export PATH=...` command in the current terminal:

```sh
cd /path/to/your/project
zero-coding
```

Your current directory is the workspace; `--cwd PATH` selects another one. The
installation contains the native executable and a small launcher, so you can
remove the source checkout afterward. Node.js and the Zero compiler are only
needed while building. Git is needed for the application's workspace file tools.
For other shells, add `~/.local/bin` to PATH manually.

### Prerequisites for the installer

- Linux or macOS, with a C compiler (`cc`) and `make`.
- libcurl and its development headers for HTTP/TLS support.
- Node.js 24 or newer, Git, `curl`, `tar`, and `sha256sum` or `shasum`.
- Internet access on the first build to fetch the pinned Zero compiler.

On macOS, install Apple's command line tools with `xcode-select --install` if
`cc`, `make`, or Git is missing. On Debian/Ubuntu, the build tools are provided by
`build-essential`; install `git`, `curl`, `libcurl4-openssl-dev`, and
`ca-certificates` as well. Install
Node.js 24+ separately if your distribution provides an older version.

### Custom location, updates, and removal

```sh
# Another prefix; leave shell startup files alone.
sh ./install.sh --prefix "$HOME/apps/zero" --no-modify-path

# Install a binary you have already built (no compiler setup or rebuild).
sh ./install.sh --binary ./dist/zero-coding

# System-wide install: build as your user, then copy with administrator rights.
make setup build
sudo sh ./install.sh --binary ./dist/zero-coding --prefix /usr/local --no-modify-path

# Remove the default user installation.
make uninstall
# For a custom installation, use the same prefix used to install it.
sh ./install.sh --uninstall --prefix "$HOME/apps/zero"
```

Rerun the installer to update. From a checkout it builds that checkout; the
downloaded installer builds `main` by default. `--ref TAG_OR_COMMIT` downloads a
specific revision instead. Uninstall removes the launcher and native executable;
it keeps shell PATH entries and user configuration.

### Publishing

`zero.toml` describes the package and build targets. Zero currently has no
`zero publish` or global `zero install` command; see the upstream
[package manifest reference](https://zerolang.ai/package-manifest) and
[CLI reference](https://zerolang.ai/cli).

Publish `install.sh` with the rest of this repository to make the download URL
above available. Include `zero.graph`, `zero.toml`, `src/`, `native/`, and
`scripts/`: the manifest alone is not a distributable application. A Git tag lets
users pin a version with `sh install.sh --ref TAG`. For installation without build
tools, distribute a native binary for each OS/architecture in GitHub Releases;
users can install their matching download with `--binary`.

## Building from source

### Prerequisites

- **Node.js ≥ 24** (required by Zero's standard library)
- A C compiler, libcurl development headers, `make`, Git, `curl`, `tar`, and a SHA-256 checksum tool

### Compile

```bash
# Set up the pinned compiler, then build for your host OS/architecture.
make setup build

# Or manually via the Zero compiler
./scripts/build.sh
```

The build creates `dist/zero-coding` for the host OS/architecture. The compiler
itself is stored at `.tools/bin/zero`.

### Running

```bash
# Run the built binary
./dist/zero-coding

# Or use the development launcher
./zero-coding --help
```

Use **↑ / ↓** to scroll the conversation and logs one line at a time, including
when reviewing an action for approval. **PgUp / PgDn** scroll by ten lines.
In the provider picker, **↑ / ↓** select a provider.

### Testing

```bash
make test
# or
python3 -m unittest discover -s tests -v
```

CI runs the build, tests, and an installation smoke test on Linux and macOS.

## Quick Start

1. **Clone & install dependencies**
   ```bash
   git clone https://github.com/Protocol-Lattice/zero-coding-tui.git
   cd zero-coding-tui
   ```

2. **Build and install**
   ```bash
   make install
   ```

3. **Run the TUI**
   ```bash
   zero-coding
   ```

4. **Test the implementation**
   ```bash
   make test
   ```

## API Reference

### Providers

The application supports four LLM providers:

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
- `--parallel <N>` – Concurrent file workers or model subagents (1–4, default 4)
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

## Parallel work and subagents

The coordinator can call `delegate_tasks` to run up to four independent model
subagents. Each task declares a prompt, a mode (`read`, `write`, or `edit`), and
one to eight exact workspace file paths. For example:

```json
{"tasks":[
  {"mode":"read","paths":["src/parser.0"],"prompt":"Review the parser and report proposed changes."},
  {"mode":"edit","paths":["README.md"],"prompt":"Correct the documented command examples."}
]}
```

Subagents inherit the selected provider, model and active skill instructions,
with separate conversations and a maximum of four model requests each. `read`
allows reading; `edit` also allows fragment edits; `write` also allows creating
or replacing files. Runtime checks restrict every file operation to the declared
paths. Subagents cannot run commands, call MCP tools, load skills, or delegate
again. Only the coordinator can modify `zero.graph`; subagents return proposals
for graph changes. The coordinator receives an ordered list of task statuses and
summaries. Delegation uses additional model requests beyond the coordinator's
`--max-turns` budget.

Consecutive `read_file`, `write_file`, and `edit_file` calls also run in a worker
pool, without another model request. `--parallel` bounds both pools; setting it
to `1` makes execution serial. Operations that may write overlapping paths wait
for earlier owners, including ASCII case aliases. Jobs with non-ASCII paths are
conservatively serialized against writers to cover filesystem case and Unicode
normalization aliases. Results enter conversation
history in their original order. Commands, MCP calls, file listings, skill loads,
and task completion wait for preceding file work.

Each mutation retains its own preview and approval. Other independent workers
continue while one waits for a decision. Headless runs deny mutations unless
`--approve` is supplied. `Esc` cancels the pool and its nested HTTP processes;
changes already completed remain on disk. Existing workspace, symlink, ignore,
and stale-preview checks apply in workers too.

## Streaming, files, and conversation limits

Requests enable streaming. Text appears while the model responds, in both the
TUI and `--prompt` mode. Tool arguments and provider metadata are assembled across
SSE events; tools run only after the response completes successfully. `Esc`
cancels the worker and restores the conversation to the beginning of the task.
Providers that return ordinary JSON are also supported. An incomplete stream,
invalid JSON, or an HTTP/transport error ends the task without executing partial
tool calls. The transport allows 8 MiB of incoming data, with 64 KiB per SSE event
and a 96 KiB assembled response buffer.

`read_file` accepts optional byte `offset` and `limit` arguments. Small files keep
the original plain-text result when these arguments are omitted. Large files or
explicit ranges return an object containing `content`, `offset`, `next_offset`,
`total_bytes`, and `truncated`. Follow `next_offset` to continue reading. Range
boundaries preserve UTF-8 characters. The default range is 8,192 bytes; `limit`
accepts 4–12,000 bytes. Heavily escaped content may require a smaller range.

`edit_file` can replace one unique text fragment in a UTF-8 file up to 64 MiB.
It previews the old and new fragment, checks a fingerprint of the entire file
again after approval, and writes through an adjacent temporary file before an
atomic rename. Existing permissions are preserved. Changes elsewhere in the file
invalidate the preview. `write_file` still accepts at most 12 KiB of complete file
content; existing per-call argument and result limits apply. Workspace path,
symlink, secret-file, and ignore checks apply to both small and large files.

Before history fills its buffer, the client shortens older tool results. If that
is insufficient, it replaces older exchanges with a local digest and retains the
current user request and recent complete tool exchanges. Tool-call IDs remain
paired with their results, including during multi-tool batches. A
`CONTEXT COMPACTED` notice marks this operation. This is deterministic, lossy
compaction based on byte budgets, without an extra model request: omitted details
may need to be read again. It does not calculate a provider-specific token window.
If the active request, tool batch, or tool catalog alone exceeds the available
space, the client reports an error and asks for a smaller task or catalog.

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

Both MCP and skills are implemented in Zero, in `src/mcp.0` and `src/skills.0`.

## Development

- **Source**: `src/main.0` is the entry point; modules group code by responsibility
- **Parallel execution**: `src/parallel.0` owns worker scheduling, scoped subagents, approvals, and ordered results
- **Tests**: `tests/` covers providers, file operations, concurrency, streaming, context compaction, extensions, and installation
- **Build**: `Makefile` – Handles compilation and distribution

After editing the source projection, synchronize and validate the program graph:

```sh
.tools/bin/zero import .
make test
.tools/bin/zero verify-projection .
```

Keep both `src/` and `zero.graph` in version control. Zero's package modules share
the package namespace. The startup buffer frames restore caller-owned spans
before returning, and are ordered from their dependencies outward for the pinned
compiler's bounded provenance analysis. The streaming JSON merger uses an
explicit stack to avoid recursive mutable borrows and stays within the direct
ARM64 backend's eight ABI argument slots.

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
