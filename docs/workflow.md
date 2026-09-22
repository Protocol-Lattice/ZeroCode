# Agentic workflow

ZeroCode has an optional built-in development loop inspired by Superpowers:

```text
discover -> plan -> implement -> review -> verify -> complete
                       ^           |         |
                       +-- repair -+---------+
```

Enable it for a task:

```sh
./zero-code --workflow agentic --prompt "Fix the parser bug and add a regression test" --approve
```

In the TUI, use `/workflow agentic` before sending a task. `/workflow` shows
the original request, plan, current stage, verification check, latest checkpoint,
observed evidence and repair count. `/workflow off` restores direct mode, which
is the default. Changing modes clears conversation history. `/status` also
shows the active stage. These local commands do not require an API key.

`--approve` retains its existing meaning: authorize file changes, commands and
MCP calls for this run. The workflow itself grants no approvals. Without that
flag, the TUI asks for each protected action and headless runs deny it.
`--workflow agentic --chat-only` is invalid; `/tools off` disables the workflow.

## How the loop works

The coordinator gets a `workflow` tool in addition to the existing tools.
Stages are runtime state, included with every model request, even after history
compaction. The tool supports these actions:

| Action | Required work and resulting behavior |
| --- | --- |
| `status` | Return the current checkpoint without advancing it. |
| `plan` | First receive a successful discovery read, listing or command result. Record the design, acceptance criteria, small ordered tasks with paths, and verification method/check. A plan can be revised before review. |
| `implement` | Requires a recorded plan. File edits and mutating delegation are now allowed. |
| `review` | Enter review after implementation. Inspect the final files or diff, checking requirements and code quality. |
| `verify` | Requires a successful file read or command inspection result received during review. Record the review findings and enter verification. |
| `repair` | Return from review or verification to implementation. Clear earlier review and verification evidence while retaining the plan. |
| `blocked` | End without claiming completion. Report the specific missing prerequisite or question; later tools in the batch are skipped. |

For example, a plan tool call is:

```json
{
  "action": "plan",
  "summary": "Fix the empty-input parser case. Add a regression test in tests/test_parser.py, update src/parser.py, and review neighboring error paths. Acceptance: empty input produces the documented error and existing parser tests pass.",
  "verification": "command",
  "check": "python3 -m unittest discover -s tests -p test_parser.py -v"
}
```

All actions except `status` require a nonempty `summary` of at most 4,000 UTF-8
bytes. `plan` also requires `verification` and a nonempty `check` of at most
2,000 UTF-8 bytes. Invalid arguments or transitions leave the checkpoint intact.

During implementation, the instructions favor reproducing a bug or writing a
focused failing test, making the smallest useful change, and running appropriate
checks. Existing `delegate_tasks` can split independent work by exact owned paths.
Workers keep their existing permissions and four-request limit. They cannot
advance the coordinator's workflow. Read-only reviewer delegation is optional;
the coordinator still needs to inspect files or command output during review.

## Verification and completion

For `verification: "command"`, the coordinator must run the exact planned
`check` through `run_command` during verification. Only an actual exit code of
zero earns verification evidence. An earlier implementation test, a model's
claim that tests passed, a denied command, a timeout or a failed command does
not count. Multiple checks can be expressed in one command with `&&` so a
failure propagates. Choose checks that exercise the acceptance criteria.

For documentation and read-only tasks, `verification: "inspection"` uses
`check` to describe what to inspect. The coordinator must read the relevant
file content again during verification. This avoids unrelated builds and tests.
An unsuccessful file read does not count.

After receiving and assessing successful verification in a subsequent model
response, the coordinator calls `finish_task`. A failed verification returns to
implementation, retaining the failed result as evidence. Repairs require a new
review and verification. File writes, mutating delegation and MCP calls are
rejected outside implementation, including in parallel file batches. During
verification, other shell commands are rejected; changing the check requires
repairing and revising the plan.

These are execution and ordering gates, not a proof of semantic correctness or
a shell sandbox. The model still chooses appropriate tests, judges review
findings and checks that all requested files/ranges were covered. Shell commands
in discovery and review are intended for inspection but retain the existing
user-level execution privileges. MCP calls are conservatively restricted to
implementation because their effects are unknown.

Plain text alone does not end an agentic task: ZeroCode asks the model to
continue from its checkpoint or report a blocker. Three such responses without
an accepted stage transition stop the task as incomplete. `--max-turns` remains
the overall coordinator request budget (default 20, maximum 50); use a larger
budget for multi-step work. Existing identical-tool detection also applies.
None of these limits reports incomplete work as successful.

`Esc` cancels existing processes and workers. Completed edits remain on disk.
Each new user task starts with a fresh discovery checkpoint, so evidence from
an earlier task cannot authorize completion. `/clear` also resets the checkpoint.
Checkpoint state is in memory for the current task; it is not a resumable job
store. Existing session logs record stage changes, tool results and completion,
and existing project memory saves a recap only through the normal success path.
The workflow does not automatically commit, merge or publish changes.

## Output batches

`--max-output-tokens N` sets the budget for each model response, including tool
arguments, between 256 and 65536 tokens. Defaults remain 4096 for Claude and
8192 for other providers. Providers receive their existing `max_tokens` or
`max_completion_tokens` field. The same budget is passed to delegated agents.
Continuation is automatic even when this option is omitted. To override the
budget, include a number, for example `zero-code --max-output-tokens 4096`.

When the provider explicitly reports reaching its output limit, ZeroCode
retains the text already produced and requests continuation without repeating
it. For an unfinished tool batch, it discards every call in that batch and asks
for smaller complete operations. It never executes or concatenates truncated
tool JSON. Large file content can be generated through successive section edits;
the workflow still requires review and verification after all sections are done.

Continuation requests consume the existing `--max-turns` budget and preserve
the original task and workflow checkpoint across history compaction. Three
consecutive incomplete tool/empty responses stop with an error. Transport
failures or streams missing their completion marker still fail immediately.
Output batching does not disable the separate history compaction budget.
