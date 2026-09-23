# Continuous learning

P2P experience sharing and the explicit `self_patch` tool are described in
[P2P agents and editable graphs](p2p.md). The automatic policy mutations below
retain their existing replay gates. Explicit patches use compiler validation
and local approval; no behavioral improvement is inferred from compilation.
With `--self-evolve --peer-name NAME`, each peer has its own program lineage in
`.zero-agent/evolution/peers/NAME/` inside its program checkout/bundle.

## Executable graph evolution

Source installs bundle the canonical graph, sources, validator and pinned Zero
compiler so the global command can evolve from any workspace. Python 3.10+, a C
compiler and libcurl development files are needed when building generations:

```sh
make install
zero-code --self-evolve --prompt "Read the complete large file"
zero-code --self-evolve --learning-history
zero-code --self-evolve --learning-rollback base
```

Each installation bundle has its own lineage in
`PREFIX/libexec/zero-code/programs/p-.../.zero-agent/evolution/`. It is independent
of the original checkout and task directory. Reinstalling identical sources keeps
the same bundle and history; upgraded sources create a new bundle and retain old
generations. Uninstall removes the command and retains program history. Installs
using `--binary` alone do not contain a program bundle.

The development launcher also supports evolution within its source checkout:

```sh
make setup build
./zero-code --self-evolve --cwd /path/to/workspace --prompt "Read the complete large file"
./zero-code --self-evolve --learning-history
./zero-code --self-evolve --learning-rollback base
# Restore a g-... generation ID from history with --learning-rollback.
```

`--self-evolve` is a launcher option and must come first. The first launch builds
a baseline from the bundle or checkout's canonical `zero.graph`. Later launches run the
selected generation's compiled executable. Qualifying experience drives:

```
task → experience → function replacement → staged zero.graph
     → native build + self-test → paired executable replay → atomic HEAD
     → live module activation at the next event-loop boundary
```

`src/learning.0` authors function replacements from measured experience.
`scripts/learning_program.py` invokes the compiler, manages isolated working
directories, validates the executables, and publishes generations. It runs
`zero patch --replace-fn`, exports the readable projection, verifies consistency,
builds the entire program plus a loadable native module, and runs its native self-test.

The initial mutation targets are `learningWindowValue(policy)` and
`learningRetryValue(policy)`. Complete default-window ASCII reads can propose a
12000-byte implementation; three identical permanent argument errors can propose
a two-attempt implementation. These replace executable function bodies in a real
compiler graph. The body vocabulary permits pure scalar expressions and branches
over `policy`. Calls, loops, declarations, imports and other target functions are
rejected before compilation. Additional targets require extending the trusted
target list and independent acceptance cases.

Both executables run the same 33 held-out cases in fresh temporary workspaces,
with frozen learning and a local scripted model transport. Cases cover sizes and
request budgets, exact contiguous byte coverage, Unicode, escaping, explicit
limits, small files, and invalid arguments. No paid provider is called. Every
previously successful case must still succeed, and no case may increase tool
calls or requests. Aggregate tool calls and modeled request cost must both
strictly decrease. No-ops, compiler failures, timeouts, incomplete experience,
failed test observations, and replay regressions cannot advance HEAD. Costs are
synthetic, not provider bills or evidence of improved general reasoning.

The strategy functions have no filesystem, shell, model or network capabilities.
Read bounds, error classification, path checks, approvals and the evaluator stay
outside the mutation targets. Validation children receive a fresh home directory
and an environment without provider credentials or inherited process hooks.
Child process groups have time limits. The trusted compiler and validation tools
themselves are ordinary local processes.

Program history lives in the **program bundle or checkout's** `.zero-agent/evolution/`.
Each generation retains its complete `zero.graph`, readable projection, native
sources, manifest, compiled executable, live module, hashes and paired evaluation report.
Publication holds an OS lock and checks the expected parent and source identity
again before moving `head.json`. The accepting session verifies the module hash,
loads it, and switches to its event-loop callback before the next tool or model
request. The process, conversation, approvals, terminal, MCP connections, peer
listener and session journal stay alive. New workers use the active generation's
executable; already running workers finish with their original code. Later
launches select the saved graph and executable through the same pointer.
Artifact hashes are checked before execution and live activation. Rollback selects
an archived generation without deleting later history. At most 32 generations are retained;
reaching that limit stops publication rather than pruning history.

The stable host retains session storage and native resources. Old code images
remain mapped because session data can reference their constant strings. Function
replacements take effect on subsequent calls through the new event loop; already
completed startup work is not replayed. A load or integrity failure leaves the
running code unchanged and reports that the saved generation was not activated.
Other sessions do not automatically adopt a changed HEAD, including an external
rollback; their proposals still have to match their running parent.

The program bundle or checkout remains the baseline; evolution never overwrites it.
Editing its graph, projection, native sources or validation driver invalidates
the lineage; use a fresh checkout to start a new lineage. Program-mode experience
lives in the **task workspace's** `.zero-agent/program-learning/` and records the
executable generation. This mode does not import or mutate the older policy
overlay. `--learning-frozen` records experience and executes the selected program
without proposing changes. `--no-learning` disables capture and uses the original
read/retry defaults even in an evolved binary. Workers inherit the selected
strategy and cannot evolve programs.

The integration test checks a global source install, operation after deleting the
original checkout, real graph rewrites, compilation, fewer reads and retries,
rejection of a compiled regression, integrity checks, and rollback. Live-session
tests also exercise consecutive explicit patches, immediate prompt/tool/UI
changes, preserved MCP connections, and seven reads becoming five on the next
task in the same TUI:

```sh
python3 -m unittest tests.test_live_program tests.test_learning_program -v
```

## Standalone binary policy learning

Without the source-checkout launcher mode, normal prompts retain this compatible
policy-learning path:

```
prompt → solve → structured experience → candidate → isolated replay
       → regression gate → accepted version or rejection → next prompt
```

The implementation is in the canonical `zero.graph`, with a readable projection
in `src/learning.0`. Its runtime semantic extension is stored under
`.zero-agent/learning/graph.json`. This overlay binds policy nodes to ZeroCode's
canonical function symbols; it does not rewrite the compiler's binary graph on
each prompt. Source semantics remain compiler-owned. Runtime experience is data,
not executable source or extra conversational memory.

The graph represents experiences, task patterns, strategies, workflows,
heuristics, failures, improvement candidates, evaluations, mutation decisions,
strategy versions, ancestry and rollback. An accepted version contains an entire
policy snapshot and references its triggering experience, evaluator and parent.
Rejected candidates retain audit metadata but never enter the active snapshot.

## Experience

Every task and local control interaction creates a private JSONL experience
journal. Model requests, tool arguments and outcomes, graph queries, worker
events, failures, observed recoveries, test-like commands and exit codes,
context compaction, request/token/time metrics, and the final outcome are
recorded. Cancellation and interruption cannot publish improvements. A process
killed before completion leaves an incomplete journal and no new policy.

Known provider credentials are redacted before text is truncated. Argument and
result excerpts are bounded; the replay evidence separately records exact range
coverage. Text saying that tests passed is never test evidence. Test commands
are identified conservatively from `test`/`check` in their command text and their
actual exit status; they are recorded as observations, not proof that an entire
project suite ran. Failed test observations disqualify read-window evidence.

Journals and graph files are private local data. `--no-learning` disables both
capture and adaptation independently of `--no-memory` and `--no-session-logs`.
Changing memory notes does not delete the separate experience audit trail.

## Supported mutations and their evaluators

The current automatically executable mutation vocabulary is deliberately finite:

| Policy | Evidence | Validation | Runtime effect |
|---|---|---|---|
| Ranged read window, 8192 → 12000 bytes | Successful complete, contiguous, default-window reads of a large ASCII file | Replay counts must strictly decrease with unchanged byte coverage; explicit-limit, escaping, Unicode boundary, empty/small-file and core-safety regressions | The existing `read_file` implementation selects the validated default window; explicit limits retain priority |
| Identical invalid-argument retries, 3 → 2 | Three identical calls with a permanent argument validation error | Fewer redundant calls with the same stopped outcome; successful calls, denials and potentially transient failures keep their existing limit | The existing repetition guard ends the invalid loop earlier |

These mutations change execution, tool retry selection, retrieval and associated
planning/workflow guidance. Only immutable guidance templates are rendered into
the next request's context. Experience text cannot become a system instruction.

Replay uses the same pure policy functions used by the executor. Candidates are
evaluated in detached scalar state, without filesystem, shell, network, model,
or `App` capabilities. The evaluator also checks retained experience fixtures and
fixed regressions. Evaluator `deterministic-policy-replay-v2` additionally runs an
independent nine-case admission suite: seven ASCII file sizes and two permanent
or transient retry failures. Every case has the same six-request budget,
including the final model response. A candidate must reduce calls on its
triggering experience and must not increase tool calls or request cost, or reduce
successful completions, across this suite. A retry failure remains a failure;
ending it sooner cannot increase the success count. Snapshots retain both policies,
the suite identity, counts and outcomes. Loading a v2 version recomputes this
evidence; previously accepted v1 snapshots remain compatible.

Admission cost is explicitly one unit per model request. It does not predict
provider token costs or general reasoning accuracy. The end-to-end benchmark
below separately measures real tool execution, accumulated context and outcome.

This standalone mode changes the runtime policy overlay. Executable graph
rewrites use the program mode above. Free-form patches, tool permissions,
path checks, approvals, credential handling, and evaluation thresholds remain
outside both automatic mutation vocabularies.

## Scope and promotion

A first accepted strategy is bound to the task fingerprint (SHA-256 of the
prompt). Project promotion requires qualifying evidence from three distinct task
fingerprints. Repeating the same prompt does not count as independent evidence.
Experiences captured with `--learning-frozen` do not count toward promotion.

Global promotion requires project-validated evidence from three distinct
workspace fingerprints and another replay/regression gate. Only scalar evidence,
hashed identifiers and version attribution enter the global graph; task text,
file paths, commands and output stay in the project. Each experience records the
local and inherited global version. Later prompts can use newly validated global
strategies; explicit rollback pins the inherited policy from the chosen snapshot
so reversal is deterministic. Delegated workers receive the selected scalar
policy snapshot and cannot write learning stores.

The global store is `zero-code-learning/learning/` under
`ZERO_LEARNING_GLOBAL_ROOT`, otherwise `XDG_STATE_HOME`, otherwise `HOME`. Its
parent must already exist. An unavailable global store does not block local
learning. Use `--no-global-learning` to prevent global reads and promotion.

## Persistence and rollback

The existing JSON/buffer, tool, session and execution abstractions supply the
observations and lifecycle. A small native scalar bridge supplies atomic storage,
as the pinned Zero backend does not lower the required filesystem primitives.
Publication uses a nonblocking OS lock, an exact comparison with the evaluated
snapshot, a durable version file and atomic rename. On contention the engine
reloads and re-evaluates once; it never publishes a stale decision. OS locks are
released on process death. Failed writes leave the previous active graph intact.

There are at most two local candidates and two promotion candidates per task,
with two transaction attempts. Graph snapshots are capped at 2 MiB, 4096 nodes,
8192 edges and 64 active rules. If a limit is reached, no further policy is
published; experience journals continue. History is never silently pruned.

```sh
zero-code --learning-history
# Use a version ID from --learning-history.
zero-code --learning-rollback 'version:e-0123456789abcdef0123456789abcdef.jsonl:0'
zero-code --learning-rollback base

# Inspect or roll back the shared store explicitly.
zero-code --learning-global --learning-history
zero-code --learning-global --learning-rollback base
```

Rollback selects a prior immutable policy snapshot and appends an audit record;
it does not delete history. Rolling a project back to `base` pins the original
defaults, including when a global strategy exists. Future completed tasks can
generate new candidates through the same gates.

## Reproduce the closed loop

```sh
make build
python3 -m unittest tests.test_learning -v
```

The restart test reads a 55,000-byte file in seven calls, validates and persists a
candidate, starts a fresh ZeroCode process, and reads identical bytes in five
calls. A no-op candidate is rejected. Rollback restores seven calls. Additional
tests exercise promotion, corruption, credentials, concurrent publication,
permanent and transient failures, and symlink confinement. No paid model calls
are used.

## Measure improvement over N tasks

```sh
make benchmark-learning
# Or choose a reproducible workload and report location:
python3 scripts/learning_benchmark.py --tasks 100 --holdout 40 --seed 20260920 \
  --require-improvement --output .zero/learning-benchmark.json
```

The benchmark runs 100 online tasks in each of two isolated workspaces. One
workspace learns normally; the other starts at `base` and uses
`--learning-frozen`. Both execute identical task content, prompts and request
budgets, and restart the real binary for every task. Frozen mode captures
experience and applies existing policies but creates no candidates or promotions.
It works with an empty store and requires no prepared policy file.

After training, both arms use frozen policies on 40 held-out tasks. Their prompt
fingerprints, contents and size ranges are separate from training. Holdout
observations cannot promote a strategy. The harness checks that the baseline
remains at `base` and the learned version does not change during evaluation.

Workloads include full ASCII reads, reads under a six-request budget, Unicode and
escaping, explicit read limits, small files and repeated invalid arguments. Only
the local model transport is scripted: it follows `next_offset` and never inspects
the arm, policy, system guidance or learned version. The executable performs the
actual reads, guards, experience capture, mutation, replay, promotion and restart.

Success requires exit zero **and exact, contiguous fixture-byte coverage**.
Invalid-argument tasks remain failures in both arms. A textual claim of success
cannot pass the oracle. Budgeted successes measure the ability to finish the
same work within fewer requests, not improved model reasoning.

The cost model uses `ceil(UTF-8 JSON bytes / 4)` as synthetic token usage, weighted
1 for input and 4 for output. It includes accumulated request context, tool
definitions, policy guidance and failed attempts. These are reproducible cost
units, **not billed provider tokens or dollars**. No real provider is called.
Normal experiences also record input/output token counts when the provider
supplies both, with explicit `usage_reports`, `usage_complete` and
`usage_scope: coordinator`. Missing or partial usage produces `null` token counts,
never a zero-cost claim; delegated worker costs are not included in these split
coordinator counters. Total session tokens retain their existing meaning.

The JSON and Markdown reports contain paired totals, success rates, cost per
successful task including failed attempts, windows of up to 20 online tasks,
per-family results and admission records. JSON retains every pair, fixture hash,
policy/version attribution and failure. `--require-improvement` exits nonzero
unless held-out tool calls and modeled cost decrease, verified success increases,
there are no success regressions or oracle errors, and a strategy was promoted.
Elapsed time is diagnostic because machine load makes it noisy. A run without
enough evidence fails these gates rather than reporting an improvement.

This protocol demonstrates the declared read/retry policies. A result on these
fixtures is not evidence of improvement on arbitrary coding tasks or paid models;
that requires a separate representative task suite with live provider usage and
independent acceptance tests. Runtime policies remain bounded to their existing
mutation vocabulary.
