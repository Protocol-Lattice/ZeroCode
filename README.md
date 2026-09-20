<img width="1620" height="971" alt="1" src="https://github.com/user-attachments/assets/7881c456-26cc-4f96-890d-02d4e21490bb" />

A terminal coding assistant built with **Zero**, with native file tools, streaming model responses, MCP servers, and project skills.

## Overview

`zero code` runs coding tasks in your current workspace. The project includes:

- **Zero compiler** – compiles Zero source into a standalone binary (`zero-code`)
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
- **Project memory** – automatic task recaps and saved facts persist across sessions, with commands to inspect and forget them
- **Validated learning** – structured task experience, isolated policy replay, immutable strategy versions, and rollback are part of the normal prompt lifecycle
- **Host builds** – Linux and macOS builds using the pinned Zero compiler and system libcurl

See [continuous learning](docs/learning.md) for the mutation gates, scope
promotion, supported policy changes, experience storage, and rollback commands.

## Project Structure

```
zero-code/
├── src/main.0          # Application state, task loop, approvals, startup
├── src/buffers.0       # Bounded buffers, JSON and UTF-8 helpers
├── src/workspace.0     # Path checks, range reads and atomic file edits
├── src/providers.0     # Provider configuration and request/response mapping
├── src/streaming.0     # SSE parsing and incremental message assembly
├── src/stream_arguments.0 # Separate buffers for streamed tool arguments
├── src/context.0       # History, tool results and automatic compaction
├── src/memory.0         # Persistent project facts, approvals and atomic storage
├── src/logs.0           # Automatic local session journals and key redaction
├── src/learning.0       # Semantic strategy graph, evidence, replay gates and versions
├── src/mcp.0           # Persistent MCP stdio connections
├── src/skills.0        # Skill discovery and loading
├── src/skill_fetch.0   # Repository fetching and project skill imports
├── src/ui.0            # Terminal rendering and keyboard input
├── native/http_stream.c # HTTP transport worker
├── native/session_log.c # Bounded native append writer
├── native/learning_store.c # Private journals and atomic graph transactions
├── tests/              # Black-box tests of the compiled binary
├── zero.toml           # Package manifest
├── zero.graph          # Canonical Zero program graph
├── Makefile            # Build automation
├── install.sh          # Linux/macOS installer for the zero-code command
├── scripts/
│   ├── setup-zero.sh   # Setup Zero compiler environment
│   └── build.sh         # Build the Zero binary
└── README.md           # This file
```

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

The build creates `dist/zero-code` for the host OS/architecture. The compiler
itself is stored at `.tools/bin/zero`.

Setup applies an exact patch to the pinned compiler's per-function fixed-buffer
limit (16 MiB) for large file-tool arguments. The application reserves up to 64 MiB of
stack before entering its buffer frames; buffer bounds checks remain enabled.
After updating an older checkout, rerun `make setup build` to refresh the compiler.

### Running

```bash
# Run the built binary
./dist/zero-code

# Or use the development launcher
./zero-code --help
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

The slow-provider regression waits 105 seconds before returning a file write:
```bash
ZERO_TEST_SLOW=1 python3 -m unittest tests.test_streaming.StreamingTests.test_provider_reply_after_100_seconds_still_writes_and_edits -v
```

CI runs the build, tests, and an installation smoke test on Linux and macOS.

## Quick Start

1. **Clone & install dependencies**
   ```bash
   git clone https://github.com/Protocol-Lattice/ZeroCode-tui.git
   cd zero-code-tui
   ```

2. **Build and install**
   ```bash
   make install
   ```

3. **Run the TUI**
   ```bash
   zero-code
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
- `--no-memory` – Disable reading and changing saved project memory for this run
- `--no-session-logs` – Disable automatic session logging for this run
- `--no-learning` – Disable experience capture and automatic policy learning
- `--no-global-learning` – Keep learning local to this project
- `--learning-frozen` – Capture measurements and use existing strategies without mutation or promotion

## Example Usage

```bash
# Connect to OpenAI with GPT-4
./dist/zero-code --provider openai --model gpt-4

# Connect to Claude
./dist/zero-code --provider claude --model claude-sonnet-4-6

# Connect to Gemini
./dist/zero-code --provider gemini --model gemini-3.8-flash

# Connect to OpenRouter (free tier)
./dist/zero-code --provider openrouter --model any/custom-model-id
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

Subagents inherit the selected provider, model, active skill instructions and a
read-only snapshot of project memory, with separate conversations and a maximum
of four model requests each. `read`
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

Parallel work appears in one **PARALLEL WORKERS** section with stable `worker-N`
rows in task order. Rows show queued, running, waiting for approval, done, error,
or cancelled states, the active tool and path, and elapsed time. Finished rows
retain their tool count and completion time until the group ends, then remain as
a compact conversation summary. Worker activity preserves the scroll position
and input, and the terminal updates only changed rows.

Tool results in the normal transcript are compact summaries. File reads show the
path, byte size, and line count; paged reads label the line count as belonging to
the returned range. Writes show bytes written, listings show file counts, and
errors retain a short diagnostic. Full results still reach the model and the
local session journal, including results from parallel workers.

## Streaming, files, and conversation limits

Requests enable streaming. Text appears while the model responds, in both the
TUI and `--prompt` mode. Tool arguments and provider metadata are assembled across
SSE events; tools run only after the response completes successfully. `Esc`
cancels the worker and restores the conversation to the beginning of the task.
Providers that return ordinary JSON are also supported. An incomplete stream,
invalid JSON, or an HTTP/transport error ends the task without executing partial
tool calls. The transport allows 64 MiB of incoming data, with 64 KiB per SSE event
and a 14 MiB assembled response buffer, including JSON escaping of file content.
Each HTTP request has a 360-second deadline. The application allows ten additional
seconds for worker cleanup, so slow responses are not cut off after 100 seconds.
`Esc` still cancels immediately; shell commands retain their 60-second limit.
Tool argument fragments accumulate in separate buffers for each call; the final
response JSON is assembled once when the stream ends. File content is copied in
bounded blocks, and events without new usage or finish metadata skip the extra
message merge. Local edits prefer small exact
replacements; long translations proceed section by section to reach the first
write sooner while preserving the full document.

`read_file` accepts optional byte `offset` and `limit` arguments. Small files keep
the original plain-text result when these arguments are omitted. Large files or
explicit ranges return an object containing `content`, `offset`, `next_offset`,
`total_bytes`, and `truncated`. Follow `next_offset` to continue reading. Range
boundaries preserve UTF-8 characters. The default range is 8,192 bytes; `limit`
accepts 4–12,000 bytes. Heavily escaped content may require a smaller range.

`edit_file` can replace one unique text fragment in a UTF-8 file up to 64 MiB.
Both `old_text` and `new_text` accept up to **1000 KB (1,000,000 UTF-8 bytes)** each.
It previews the old and new fragment, checks a fingerprint of the entire file
again after approval, and writes through an adjacent temporary file before an
atomic rename. Existing permissions are preserved. Changes elsewhere in the file
invalidate the preview. `write_file` can replace existing UTF-8 files up to 64 MiB,
with a bounded preview of the old content, a fingerprint check, preserved
permissions, and atomic replacement. It accepts up to **1000 KB (1,000,000 UTF-8
bytes)** of complete new content; use `edit_file` in fragments for larger rewrites
without dropping content. Large proposed fragments have explicitly truncated
previews; the complete supplied text is used when the operation is approved.
Previews and successful write summaries show the full proposed content's line
count, including lines beyond the preview. For translations, Zero is instructed
to preserve every section and example and to edit in sections when the complete
replacement cannot fit in one model response.
Existing per-call argument and result limits apply. Workspace path,
symlink, secret-file, and ignore checks apply to both small and large files.

Before history fills its buffer, the client shortens older tool results. If that
is insufficient, it replaces older exchanges with a local digest and retains the
current user request and recent complete tool exchanges. Tool-call IDs remain
paired with their results, including during multi-tool batches. A
`CONTEXT COMPACTED` notice marks this operation. This is deterministic, lossy
compaction based on byte budgets, without an extra model request: omitted details
may need to be read again. It does not calculate a provider-specific token window.
The normal working budget is 64 KiB, with up to 1 MiB available for a large active
exchange. Provider reasoning blocks and signatures are retained intact while
tools run. Completed exchanges may be summarized to make room for the next
response; the transport accepts the larger history together with system context
and tool definitions.
If the active request, tool batch, or tool catalog alone exceeds the available
space, the client reports an error and asks for a smaller task or catalog.
Large file arguments remain intact for execution but their text is replaced by an
explicit omission notice in subsequent model history. Paths, call IDs, metadata,
and results are retained. Calls whose arguments exceed the worker batch buffer
run sequentially. Provider output-token limits still apply independently.

## Project memory

Zero automatically saves a short recap after a successful task, using the final
response or `finish_task` summary. No separate command, approval or extra model
request is needed. The latest three recaps are kept, newest first, alongside
lasting project facts and preferences that you or the model explicitly save.
Recaps are bounded excerpts, not a separate AI summary; long responses are
shortened at a UTF-8 boundary.

Memory lives in `.zero-agent/memory.json` under the workspace selected by `--cwd`.
It is included in subsequent model requests for all four providers, including
chat-only mode, and survives conversation compaction, `/clear`, and provider or
model changes. It is treated as reference data; the current task and tool
restrictions still take precedence.

Manage notes directly in the TUI:

```text
/memory                              Show saved facts, recent recaps and commands
/memory set testing Run make test.    Add or replace the note named testing
/memory delete testing               Forget that note
/memory clear                        Forget all saved facts and recaps
```

These explicit commands work without an API key. They also work headlessly:

```sh
zero-code --cwd /path/to/project --prompt '/memory set testing Run make test.'
zero-code --cwd /path/to/project --prompt '/memory'
```

The model can use the `memory` tool with `action` set to `list`, `set`, `delete`,
or `clear`. `set` takes a `key` and `content`; `delete` takes a `key`. Listing runs
without approval. Model-requested changes show a preview and require approval, or
`--approve` in headless mode. Delegated subagents receive a snapshot of the notes
but cannot call this tool or change the store.

Memory contains facts and recaps; full activity is saved separately in session logs. Failed,
cancelled or empty responses and tasks containing tool errors or denied actions
do not get a recap. Recaps containing the active API key are also skipped.
An explicit `memory` deletion or clear suppresses the recap for that task so
the final acknowledgement does not reintroduce forgotten information.

Saved facts and recaps are sent to your selected provider as context. Avoid
storing secrets. Use `--no-memory` to disable both reading and changing memory for a session; existing
notes remain on disk. Demo mode also disables memory. Deleting notes removes them
from future memory context; use `/clear` as well to discard messages from the
current conversation that may already mention them.

The store is limited to 32 named facts, three recaps, and 8 KiB of encoded JSON.
Keys contain 1–64 ASCII letters, digits, underscores or hyphens; each note contains at most 1,024 UTF-8
bytes. Older recaps are dropped first when space is needed. If named facts leave
no room for a new recap, automatic saving reports that it was skipped and keeps
the facts intact. New stores use a private directory and files use mode `0600`.
The directory is already excluded from this repository and from normal file tools; add
`.zero-agent/` to other projects' `.gitignore` files if needed.

Saves compare the store with the preview under a short write lock and replace it
atomically. A conflicting or busy save fails without overwriting another
session's changes; list memory and retry. Invalid, oversized, unreadable or
symlinked stores are reported and left untouched. Repair the file before saving
again. If a process is forcibly killed during the write, a stale
`.zero-agent/memory.lock` directory may need to be removed after confirming no
save is active.

## Automatic session logs

Every normal session automatically creates
`.zero-agent/sessions/session-XXXXXXXX/events.jsonl` under its workspace. Each
prompt, displayed reply, tool preview/result, approval, and error is appended as
it happens. Streaming replies are saved incrementally, so received text remains
available even if the process is killed before the task completes. Parallel
worker activity is collected in the coordinator's journal.

Use `/logs` to show the current file, or inspect it without an API key:

```sh
zero-code --cwd /path/to/project --prompt '/logs'
```

Each line is a JSON object with a schema `version`, sequence `seq`, Unix-seconds
`time`, `event`, `title`, and `text`. Large entries share a sequence number and
use zero-based `part` values with `last: true` on the final part. Streamed text
uses `assistant_delta` records followed by `stream_end`; normal sessions finish
with `session_end`. Worker output uses `worker_delta` with a worker title.
An absent end record can indicate an abrupt termination.
Writes reach the operating system after each record; this is process-crash persistence, not
a guarantee against power loss. A forcibly interrupted write may leave a partial
last line; earlier complete JSON lines remain readable.

`/clear`, context compaction, and memory deletion retain the journal. Restarting
creates a new file, so simultaneous sessions never share a journal. Logs are not
loaded into model context and are not automatically pruned. Delete old session
directories when you no longer need them.

Files use mode `0600` inside private session directories. Configured provider API
keys and keys entered through `/key` are redacted, including across streaming
chunks; the key dialog itself is never recorded. Logs can still contain other
sensitive text from prompts or tool output. Keep `.zero-agent/` out of version
control. `--no-session-logs` disables logs independently of `--no-memory`; use
both flags to disable session logs and memory. Demo, snapshot, help, and
self-test runs do not create logs. If storage fails, a visible warning disables
logging for that run while the task continues. Learning experiences have their
own journal; add `--no-learning` to disable that persistence as well.

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
./zero-code --mcp all --approve --prompt "Use the configured tools to complete the task."
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
/skills fetch REPO  Fetch skills from a Git repository into this project
/skill review-docs  Apply a skill to the session
/skill off          Clear the selected skill
```

Fetch a skill collection using a Git URL or a GitHub `owner/repository` shorthand:

```text
/skills fetch anthropics/skills
/skills fetch https://github.com/owner/repository.git --path skills/review-docs
/skills fetch git@github.com:owner/repository.git --path skills --ref v1.0
```

`--path` selects one skill directory or a directory containing skill folders;
quote paths containing spaces. Without it, Zero checks for a root `SKILL.md`,
then `skills/`, then `.agents/skills/`, then skill folders at the repository root.
`--path .` explicitly selects the repository root. `--ref` selects a branch or
tag; otherwise Git's default branch is used. HTTPS, HTTP, SSH, Git, and `file://`
URLs are supported. Git uses your existing credentials, with interactive prompts
disabled, and the fetch stops after 120 seconds. Press **Esc** to cancel a clone.

Fetched skills are installed in the current workspace's `.agents/skills/` and
the catalog refreshes immediately. Existing files or directories with the same
skill name are kept, including local edits; fetching again does not update them.
Fetching is an explicit user action and works without an API key or `--approve`:

```sh
zero-code --cwd /path/to/project --prompt '/skills fetch owner/repository --path skills'
```

Zero validates metadata with the same rules used for local discovery and copies
tracked references, scripts, and assets, preserving executable permissions.
It never runs bundled scripts or initializes submodules. Symlinks, unsafe paths,
and files ignored by the source or workspace are rejected. Each skill is staged
before installation, so a failed copy does not expose a partial skill. Other
successfully imported skills remain installed if a later skill fails. Temporary
files are cleaned up after success, failure, or cancellation.

A fetch handles up to 32 skills and examines at most 512 collection entries.
Each skill may contain up to 256 tracked files totaling 16 MiB, with a file list
smaller than 32 KiB. Use `--path` to select a smaller collection if needed.
`SKILL.md` retains the 16 KiB UTF-8 limit. `--no-skills` and demo mode disable
fetching.

User skills are discovered in `$XDG_CONFIG_HOME/zero-code/skills`, or
`~/.config/zero-code/skills` when `XDG_CONFIG_HOME` is unset. Use
`--skills-dir PATH` to replace that user collection with another directory;
project skills take precedence when names collide. `--skill NAME` selects a skill
at startup, and `--no-skills` disables discovery.

Discovery is limited to 32 valid skills and a bounded catalog. Skill files and
text references must be UTF-8 and at most 16 KiB. Workspace `.gitignore` rules,
path checks, and symlink restrictions also apply to skills. Loading a skill does
not run its scripts or grant approvals: scripts use the existing approved
`run_command` flow. Skill instructions remain subordinate to the user's task.

MCP and skills are implemented in Zero, in `src/mcp.0`, `src/skills.0`, and
`src/skill_fetch.0`.

## Development

### Product website

The standalone Node.js promotional website lives in [`website/`](website/README.md).
Run `npm run dev` from this repository and open `http://localhost:3000`.
It requires Node.js 24+ and has no package dependencies or build step.

### Terminal application

- **Source**: `src/main.0` is the entry point; modules group code by responsibility
- **Parallel execution**: `src/parallel.0` owns worker scheduling, scoped subagents, approvals, and ordered results
- **Memory**: `src/memory.0` owns bounded project notes, persistence and memory tool/command handling
- **Tests**: `tests/` covers providers, file operations, concurrency, streaming, context compaction, persistent memory, extensions, and installation
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
