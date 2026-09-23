# Peer agents, large context and an editable program graph

ZeroCode runs a native Zero terminal interface. Each launched peer has its own
model loop, workspace, approvals and optional executable lineage. Every peer
serves the same learning protocol; there is no leader or central learning server.
Seed addresses are bootstrap links, not privileged agents.

## Start a local mesh

Build with `./scripts/build.sh`. Set the same randomly generated group secret in
each terminal (`ZERO_PEER_TOKEN`, 32–256 printable characters). Generate it once:

```sh
openssl rand -hex 32
```

Copy the resulting 64-character value into each terminal's export below.
`ZERO_PEER_TOKEN` is an environment variable, not a command-line argument or a
token-length setting. Both peers must use the same value.
Configure the provider's API key separately; it is never sent to other peers.

```sh
# Terminal A
export ZERO_PEER_TOKEN='<paste the generated value here>'
./zero-code --peer-name alpha --peer-port 4311 \
  --peers http://127.0.0.1:4312 --workflow agentic --context-window 1000000

# Terminal B (may use another --cwd and another provider/model)
export ZERO_PEER_TOKEN='<paste the same generated value here>'
./zero-code --peer-name beta --peer-port 4312 \
  --peers http://127.0.0.1:4311 --workflow agentic --context-window 1000000

# Terminal C only needs a link into the mesh. Lessons propagate transitively.
export ZERO_PEER_TOKEN='<paste the same generated value here>'
./zero-code --peer-name gamma --peer-port 4313 \
  --peers http://127.0.0.1:4312 --workflow agentic --context-window 1000000
```

Use a model that actually supports at least one million total context tokens.
The default provider/model is not a capability guarantee. Set `--model` and
`--provider` for your account, or lower `--context-window` for a smaller model.

`--peer` selects an available local port. `--peers` accepts up to 16 comma-separated
base URLs; `ZERO_PEERS` provides the same setting through the environment.
`--no-peers` disables networking; `--no-learning` also disables peer learning and
its network transport. With no peer options or seeds the application
runs standalone. Each named peer has a persistent random identity, scoped to its
workspace. Give simultaneous agents in one workspace different names.

For remote machines, expose the listener behind your HTTPS reverse proxy and
configure its HTTPS base URL as a seed. `--peer-bind` accepts a numeric IPv4
address; the default is loopback. The native listener itself speaks HTTP.
Plain HTTP seed URLs are accepted only for loopback hosts. Bearer credentials
are checked on every request and redirects are disabled.

## What agents learn together

Local tool results produce records in six classes: invalid paths, invalid read
ranges, ambiguous edits, failed commands, missing/unreadable files, and edits
invalidated by concurrent changes. A record contains a protocol version, random
origin ID, class and HMAC-SHA256 action fingerprint. File contents, paths, shell
text, tool output, prompts and API keys are absent from the wire format.

Authenticated peers exchange immutable sets every two seconds and after local
observations. Records are deduplicated, merged under a process/file lock and
persisted with an atomic replacement before they become visible. Repeated and
out-of-order deliveries converge to the same set. Disconnected peers retain
their records and merge again on reconnection. There are no central authorities,
automatic peer discovery, public DHT or NAT traversal.

The bounded store, `.zero-agent/peers/lessons.v1`, holds 4096 records. Reaching
capacity rejects new merges and raises the visible error counter; it does not
silently discard evidence. `/peers` shows identity, listener port, recently
reachable seeds, exchanges, durable lessons, admitted rules and errors. A seed
is shown as reachable only after a successful authenticated exchange. Incoming
connections and delegated peers do not inflate the seed count.

Learned rules are consulted before tool execution. Invalid path/range calls are
rejected only when the corresponding invariant also fails locally. Other classes
provide fixed recovery guidance to the model: inspect the current file, repair
the failing command, or construct a fresh edit. A transient failure in another
workspace never becomes a permanent ban on a valid local command or file.
Peer messages cannot become arbitrary system prompts or executable patches.

This reduces repeated known mistakes; it is not a guarantee that an LLM never
makes a new mistake. The six classes are the current learning vocabulary, not
distributed model-weight training. New recovery policies can be implemented by
changing the Zero functions that admit and apply those classes.

Delegated model workers join the same protocol with ephemeral peer identities
and their parent's listener as a seed. They retain the task's file ownership
restrictions. Independent TUI peers remain equals and each can run commands and
evolve its own program; task delegation does not appoint a mesh leader. File
batch execution is serialized while P2P is active so every call passes the same
learned guards. Explicit `delegate_tasks` still runs independent tasks in parallel.

## One million tokens

The default total budget is **1,000,000 tokens**, configurable from 16,384 to
1,000,000 with `--context-window`. History, system instructions, tool schemas,
output and a 2048-token margin share that budget. `/context` distinguishes the
estimated next request from the last provider-reported input usage.

The estimator starts at three UTF-8 bytes per token, then uses provider usage
with ten percent headroom. It is not an exact model tokenizer; JSON wire size
and actual token count differ. Provider context limits remain authoritative.
The client cannot enlarge a model's window. Local history has a separate 6 MiB
storage ceiling, and the HTTP envelope can use the existing 12,032,768-byte
argument arena. Exceptionally verbose token encodings may reach the byte ceiling
before the token budget. The old unconditional 64 KiB compaction threshold and
1.25 MiB HTTP transport ceiling no longer constrain model requests.

When a limit is reached, old tool output is shortened and older exchanges get a
digest. Current instructions and complete recent tool exchanges are retained.
An active exchange that cannot fit stops with an explanation instead of silently
exceeding the configured budget. `/clear` starts a fresh conversation; persisted
learning remains available.

If an incoming response cannot be retained, diagnostics distinguish serialization
failure from context exhaustion and report history usage, the token-derived byte
budget and response staging size. A failed append preserves the prior history.
Use `/context` to inspect the budget; the 6 MiB storage ceiling alone does not
describe the available token budget.

## The program is `zero.graph`

`zero.graph` is the real Vercel Zero compiler input. Files under `src/` are its
readable projections. The peer transport is a narrow native C bridge; admission,
tool dispatch, context management and self-edit orchestration live in the graph.

Launch with `--self-evolve` to enable executable generations:

```sh
./zero-code --self-evolve --peer-name alpha --peer-port 4311 \
  --peers http://127.0.0.1:4312 --workflow agentic
```

The model receives `self_inspect` and `self_patch`. First read the actual function
from the running executable graph:

```json
{"function":"helpText"}
```

`self_inspect` is automatic and read-only. It validates the executable generation
and asks the compiler for canonical function source, returning its version and
the full `graph_path`. It accepts function names, not filesystem paths. Use
`offset` with the returned `next_offset` until `truncated` is false; `limit` is
4–12000 UTF-8 bytes. No provider request, compilation or candidate execution is
performed by this tool.

Workspace `read_file` cannot read the installed bundle under `~/.local/libexec`
or ignored evolution directories. Use `self_inspect` for your own executable
even when the task workspace happens to be the ZeroCode source checkout.

A `self_patch` contains 1–8 existing function names and replacement bodies:

```json
{"patches":[{"function":"learningWindowValue","body":"return 12000"}]}
```

For a function returning text, use `return_text` to have the driver generate the
Zero `return` statement and escape its string literal:

```json
{"patches":[{"function":"systemPrompt","return_text":"The complete new system prompt."}]}
```

Each replacement requires exactly one of `body` or `return_text`. `body` contains
Zero statements, without a function signature, enclosing braces, Markdown fences
or a trailing semicolon. Read the existing function with `self_inspect` first.
Both forms are limited to 12000 UTF-8 bytes of generated function body.

Patch requests travel over the compiler driver's standard input, avoiding the
Zero runtime's 4 KiB process-argument limit. The driver reads at most 64 KiB of
request data plus one byte to detect overflow.

The TUI previews the proposal and uses the normal approval flow (`--approve` for
an unattended run). The driver copies the active program, applies real
`zero patch --replace-fn` operations, exports projections and compiles it. Only a
successful build is atomically saved and loaded into the **current session**.
The event loop switches to the new code before the next tool or model request,
keeping the process, conversation, terminal and connections alive. A failed
build or stale proposal leaves HEAD unchanged. A module load or integrity failure
keeps the old code running and explicitly reports that activation failed.
Cancellation stops the compiler process group. Explicit patches use compiler
validation; they are not claimed to improve behavior and the candidate
is not executed during admission.

Rejected patches are shown as failures with compiler diagnostics. After three
failed `self_patch` calls within one user task, the loop stops, including when
other tools were called between attempts. Send a new message with a corrected
proposal to start another task. An accepted patch is displayed with its saved
generation and can be followed by another patch in the same session.

After a successful patch and activation, the default `self_inspect` target
`"running"` reads the active generation. `"target":"selected"` reads the latest
saved generation; these can differ if activation failed or another session
changed HEAD. Changes apply on subsequent calls; startup work already completed
is not rerun. Existing workers complete with their original generation and new
workers use the updated executable.

Each `--peer-name` has a separate lineage under
`.zero-agent/evolution/peers/NAME/` in the source checkout or installed program
bundle. No peer can publish another peer's graph through the network.

```sh
./zero-code --self-evolve --peer-name alpha --learning-history
./zero-code --self-evolve --peer-name alpha --learning-rollback base
```

Automatic read-window/retry learning still follows its existing narrower
build-and-replay gates. See [learning](learning.md) for that evaluator and
generation integrity checks. Shell tools run with the user's host permissions;
file tools enforce their existing workspace, path and approval rules.

### A visible graph-change example

In a session started with `--self-evolve --peer-name alpha`, ask:

> Call self_inspect with function renderInput and read its complete source. Then
> use self_patch to add a visible `[zero.graph: v2]` marker to the input renderer.
> Preserve the existing input behavior. Report the compiler result, new
> generation ID and graph_path. Use self_inspect with target running to show
> the active function.

After the proposal is accepted, the marker appears in the same TUI. The updated
renderer runs without restarting the program. Reuse the
same peer name and program checkout/bundle when inspecting history or rolling back.
