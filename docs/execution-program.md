# Bounded execution programs (experimental)

This opt-in **checkout launcher mode** moves a bounded file-edit workflow out of the model/tool round-trip loop. It is a first implementation of request reduction, not a rewrite of the interactive TUI and not a claim of 90% savings across arbitrary coding tasks.

```
explicit file scope -> local native reads -> one model program
                    -> approval -> ordered native writes
                    -> user-configured formatters and checks
                    -> checks_passed, or one optional repair request
```

The model does not choose verification commands, spawn subagents or decide when to run a formatter. File access still goes through the existing native `--job-worker` protocol with `kind=tool`: no LLM request is made by a file worker. The controller is Python 3.10+ standard library code, following the project's existing optional Python-driver approach. The default native executable, `zero.graph` and `.0` projections are unchanged.

## Run from a checkout

Build the existing native binary with the normal project setup, then use the **checkout's** `./zero-code` launcher. Put `--execution-program` first. The installed launcher and `dist/zero-code` do not dispatch this experimental mode yet.

```sh
make setup build
export OPENROUTER_API_KEY='...'

./zero-code --execution-program \
  --cwd /absolute/path/to/project \
  --provider openrouter --model openrouter/free \
  --llm-request-budget 2 \
  --context internal/parser/parser.go \
  --context internal/parser/parser_test.go \
  --format 'gofmt -w internal/parser/parser.go internal/parser_test.go' \
  --verify 'go test ./internal/parser' \
  --prompt 'Fix the empty-input parser regression and its test'
```

The named context files must exist. Add `--create path/to/new_file` to explicitly authorize a missing file. A creation target that already exists is rejected; supply it as `--context` instead. Context collection is deterministic and makes no model request, but **this version does not search the repository or infer a complete task scope**.

Before applying a program, the controller shows its complete replacement texts and the fixed command list on stderr. Interactive runs require `y`; noninteractive runs deny by default. Add `--approve` only when authorizing all proposed scoped changes and the supplied formatter/verification commands. A repair also goes through approval. Output on stdout is a JSON execution report, and only `checks_passed` exits with code zero.

`--provider openai`, `--provider claude` and `--provider gemini` require an explicit `--model`. Their keys come from `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and `GEMINI_API_KEY` (or `GOOGLE_API_KEY`), respectively. The OpenRouter default is `openrouter/free`. `--api-url` supports an explicit HTTPS endpoint or a loopback HTTP fixture. Provider redirects are rejected to avoid forwarding credentials. No live-provider compatibility or performance result is claimed by the fixture tests.

## Execution contract

The model returns exactly one `submit_execution_program` tool call. Its input has `summary` and `changes`. Each change has exactly these fields:

```json
{
  "id": "change-1",
  "path": "explicitly/scoped/file.txt",
  "before_sha256": "the exact SHA-256 supplied with that file, or missing for a new file",
  "content": "complete replacement text\n",
  "depends_on": []
}
```

The hash in an actual request is a 64-character digest copied from discovery; the explanatory value above is not a runnable program. `depends_on` names other change IDs. The controller validates the entire program and topologically orders it before execution. This first executor is **sequential**, including independent changes; it does not introduce concurrent writes.

Unknown fields, commands in model output, unscoped paths, duplicate or conflicting paths, stale hashes, cyclic/unknown dependencies and no-op replacements fail closed. A malformed proposal or transport failure is terminal, not an invitation to invent argument mappings. Only failed local formatting/verification can lead to the optional semantic repair request.

All model-visible existing files are read through the native file tools. Paths containing traversal, secrets, symlinks or `zero.graph` are rejected; native `.gitignore` and worker-scope restrictions still apply. Before mutations, the controller checks the initial snapshots. At each native approval event it checks the model's snapshot again, then the native writer checks its own preview fingerprint. It does not use fuzzy patch relocation or bypass a denied tool with shell writes.

## Verification and request accounting

`--verify` is required and repeatable. These exact **user-supplied** shell commands run locally after approved writes and optional `--format` commands. The model cannot replace a failing check with a trivial command. A formatter failure prevents verification for that attempt. Commands have bounded captured output and a timeout; cancellation kills their process groups, including descendants whose parent has already exited.

Shell checks and formatters are real code execution, not a sandbox. They can modify files or make network requests. Approve only commands you trust. The controller removes common provider-key/token environment variables from child processes, but does not claim to isolate commands from the host or to meter arbitrary network activity performed inside user commands.

On a failed check, the repair request receives the original goal, fixed commands, failure evidence, and fresh contents of changed files or scoped files named in diagnostics. It does not replay the full previous conversation. There are no separate planning, routing, reviewing, summarizing or finishing model requests. The default request budget is two (initial proposal plus one repair); configurable range is one to four. Budget exhaustion is failure, never fabricated completion.

The report distinguishes:

- `model_requests`: charged planner invocations, including failures; the request budget applies here.
- `provider_requests`: actual HTTP inference attempts by the CLI planner; no automatic retries or redirects.
- `tool_calls`: native worker calls, including reads and approval-time rechecks; these are not model requests.
- `files_changed`: confirmed program writes; `unconfirmed_changes` identifies a write whose outcome needs inspection.
- `checks`: formatter/verification receipts, exit codes, bounded output and timeout flags.

Provider usage is reported as supplied, with `null` when absent, rather than estimated. `checks_passed` means only that the declared commands exited successfully. It is not a semantic proof that every aspect of the natural-language task was fulfilled. Review the diff and choose meaningful checks.

## Boundaries of this first PR

A task is limited to 32 explicit paths, 64 KB of context, 16,000 UTF-8 bytes per file/replacement, bounded escaped replacement size, and 32 changes per program. It uses complete replacements, so it targets small, clearly scoped tasks rather than large-file refactors. No P2P, MCP, automatic skill loading, graph self-patching, learned replay or general-purpose repository discovery is enabled in this mode. The provider response is collected as a bounded JSON program; this does not change the TUI's streaming decoder or a model's tokens-per-second rate.

A program is **not an atomic multi-file transaction**. Earlier writes are retained if a later operation fails. Failed or uncertain mutations are never replayed automatically, and there is no destructive workspace rollback. Inspect `files_changed`, `unconfirmed_changes`, the command receipts and your working tree after a failure. Formatter/check side effects are not comprehensively enumerated by the confirmed program-write list.

## Tests and measurement

```sh
python3 -m unittest discover -s tests -p 'test_execution_program.py' -v
make test
```

The focused suite covers a ten-file task with one planner invocation, bounded repairs, delta context, approvals, stale snapshots, partial failures, dependency validation, strict JSON, endpoint/redirect behavior, provider envelopes, bounded IO, and process-group cleanup. An offline native-protocol fixture exercises the approval handshake. Separate real-native tests run when `dist/zero-code` is available; without it they are explicitly skipped.

The ten-file fixture is a **request-count contract**, not a live-model benchmark and not a measurement against the existing agentic workflow. To claim 90% reduction, compare the same held-out tasks, provider/model settings, initial workspaces and acceptance checks against the unmodified agent, count all inference attempts, and report failure/repair rates as well as latency, tokens and requests per verified success. Do not obtain a better percentage by dropping unsuccessful tasks or skipping verification.
