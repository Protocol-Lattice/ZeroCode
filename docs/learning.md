# Continuous learning

Normal prompts follow this path automatically:

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
fixed regressions. Its metric is deterministic tool-request cost and coverage,
not a claim about improved general reasoning or live model accuracy. End-to-end
tests verify the predicted savings against actual tool execution.

There is no arbitrary source-code self-rewrite admission path. Changing ZeroCode's
implementation beyond these declared policy slots requires extending the trusted
mutation vocabulary and supplying an evaluator with appropriate isolated build,
replay and regression checks. Free-form patches, tool permissions, path checks,
approval policy, credential handling, and evaluation thresholds are outside the
automatic mutation vocabulary. The graph and evaluator are extensible without
pretending that an untested code patch is a validated improvement.

## Scope and promotion

A first accepted strategy is bound to the task fingerprint (SHA-256 of the
prompt). Project promotion requires qualifying evidence from three distinct task
fingerprints. Repeating the same prompt does not count as independent evidence.

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
