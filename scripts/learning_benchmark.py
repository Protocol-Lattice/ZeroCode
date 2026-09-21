#!/usr/bin/env python3
"""Paired, offline learning benchmark against the actual ZeroCode executable.

Only the model transport is scripted. Tools, policy selection, experience,
replay, promotion, persistence and restarts are production code. The transport
does not inspect the policy or arm. Success is checked against fixture bytes.
Synthetic usage is explicitly a cost model, not a provider invoice/tokenizer.
"""
import argparse
from dataclasses import dataclass
import hashlib
import http.server
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import threading


ROOT = Path(__file__).resolve().parents[1]
SUITE = "learning-trajectory-v1"
KEYS = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY", "GOOGLE_API_KEY")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


@dataclass(frozen=True)
class Case:
    identity: str
    family: str
    content: str
    budget: int = 20
    limit: int | None = None

    @property
    def path(self):
        return self.identity + ".txt"

    @property
    def prompt(self):
        if self.family == "invalid_arguments":
            return f"{self.identity}: read the file using numeric path 7."
        instruction = f"Read {self.path} completely, continuing next_offset until all bytes are read."
        if self.limit:
            instruction += f" Use an explicit limit of {self.limit} bytes in every read."
        return instruction


def cases(count, seed, split):
    """Balanced blocks; disjoint prompts, contents and size ranges per split."""
    rng = random.Random(f"{SUITE}:{seed}:{split}")
    schedule = []
    while len(schedule) < count:
        block = list(range(10))
        rng.shuffle(block)
        schedule.extend(block)
    output = []
    for index, kind in enumerate(schedule[:count]):
        identity = f"{split}-{index:04d}"
        jitter = rng.randrange(1000) + (1000 if split == "holdout" else 0)
        size = (24000, 43000, 81000, 52000, 55000, 32000, 32000, 8000, 0, 18000)[kind] + jitter
        family = "ascii_read"
        budget, limit = 20, None
        if kind == 3:
            family, budget = "budgeted_read", 6
        elif kind == 5:
            family = "escaped_unicode"
        elif kind == 6:
            family, limit = "explicit_limit", 4096
        elif kind == 7:
            family = "small_read"
        elif kind == 8:
            family = "invalid_arguments"
        alphabet = "żółw🐢\t\"\\\n" if family == "escaped_unicode" else f"{seed}|{identity}|abcXYZ0123456789"
        content = (alphabet * (size // len(alphabet) + 1))[:size]
        output.append(Case(identity, family, content, budget, limit))
    return output


class Replay:
    """A fixed next_offset client plus an independent byte-coverage oracle."""

    def __init__(self, case):
        self.case = case
        self.expected = case.content.encode()
        self.covered = bytearray()
        self.seen = set()
        self.errors = []
        self.requests = self.input_tokens = self.output_tokens = 0

    def observe(self, observation):
        identity = observation["tool_call_id"]
        if identity in self.seen:
            raise ValueError("Transport received the same tool result twice")
        self.seen.add(identity)
        text = observation["content"]
        if text.startswith("Error:"):
            self.errors.append(text)
            return None
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            row = None
        if not isinstance(row, dict) or "next_offset" not in row:
            fragment, start, end, total = text.encode(), 0, len(text.encode()), len(text.encode())
            row = {"truncated": False}
        else:
            fragment = row["content"].encode()
            start, end, total = row["offset"], row["next_offset"], row["total_bytes"]
        if (start != len(self.covered) or end != start + len(fragment)
                or total != len(self.expected) or fragment != self.expected[start:end]):
            self.errors.append("Read coverage/content differs from the fixture")
        self.covered.extend(fragment)
        return row

    def respond(self, body):
        self.requests += 1
        observations = [m for m in body["messages"] if m.get("role") == "tool"]
        last = self.observe(observations[-1]) if observations else None
        arguments = {"path": self.case.path}
        done = bool(observations and (last is None or not last["truncated"]))
        if self.case.family == "invalid_arguments":
            arguments, done = {"path": 7}, False
        elif last and last.get("truncated"):
            arguments["offset"] = last["next_offset"]
        if self.case.limit:
            arguments["limit"] = self.case.limit
        message = {"role": "assistant", "content": "Read complete." if done else None}
        if not done:
            message["tool_calls"] = [{"id": f"read_{self.requests}", "type": "function",
                                      "function": {"name": "read_file", "arguments": json.dumps(arguments)}}]
        # A reproducible byte-based proxy, deliberately independent of policy.
        # Includes accumulated context, tool definitions and learned guidance.
        input_tokens = math.ceil(len(encoded(body)) / 4)
        output_tokens = math.ceil(len(encoded(message)) / 4)
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        return {"choices": [{"message": message, "finish_reason": "stop" if done else "tool_calls"}],
                "usage": {"prompt_tokens": input_tokens, "completion_tokens": output_tokens,
                          "total_tokens": input_tokens + output_tokens}}

    def success(self, returncode):
        return (returncode == 0 and self.case.family != "invalid_arguments" and not self.errors
                and bytes(self.covered) == self.expected)


class Transport:
    def __init__(self):
        transport = self
        self.replay = None
        self.errors = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    response, status = transport.replay.respond(body), 200
                except Exception as exc:
                    transport.errors.append(str(exc))
                    response, status = {"error": {"message": str(exc)}}, 500
                data = encoded(response)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/chat/completions"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def graph(root):
    path = root / ".zero-agent/learning/graph.json"
    return json.loads(path.read_text()) if path.exists() else {"head": "base", "nodes": []}


def run_case(executable, root, case, transport, frozen, env_override=None):
    (root / case.path).write_text(case.content, encoding="utf-8")
    before = graph(root)["head"]
    replay = transport.replay = Replay(case)
    env = dict(os.environ if env_override is None else env_override)
    for key in ("ZERO_LEARNING_PROGRAM_ROOT", "ZERO_LEARNING_PROGRAM_VERSION"):
        env.pop(key, None)
    for key in (*KEYS, "ZERO_API_URL"):
        env.pop(key, None)
    env["OPENROUTER_API_KEY"] = "offline-replay-no-provider-credentials"
    env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1"
    command = [str(executable), "--cwd", str(root), "--provider", "openrouter", "--model", SUITE,
               "--endpoint", transport.url, "--no-global-learning", "--no-memory", "--no-skills",
               "--no-session-logs", "--parallel", "1", "--max-turns", str(case.budget)]
    if frozen:
        command.append("--learning-frozen")
    result = subprocess.run([*command, "--prompt", case.prompt], env=env, capture_output=True, text=True, timeout=60)
    if transport.errors:
        raise RuntimeError(f"Replay transport failed: {transport.errors}")
    document = graph(root)
    experiences = [n for n in document["nodes"] if n["kind"] == "experience"]
    if not experiences or experiences[-1]["pattern"] != digest(case.prompt.encode()):
        raise RuntimeError(f"Missing experience for {case.identity}: {result.stderr} {result.stdout[-2000:]}")
    experience = experiences[-1]
    metrics = experience["metrics"]
    if (not experience["complete"] or not metrics["usage_complete"]
            or metrics["requests"] != replay.requests or metrics["input_tokens"] != replay.input_tokens
            or metrics["output_tokens"] != replay.output_tokens):
        raise RuntimeError(f"Incomplete or inconsistent measurements for {case.identity}")
    if frozen and document["head"] != before:
        raise RuntimeError("Frozen evaluation changed the strategy version")
    success = replay.success(result.returncode)
    return {"success": success, "status": experience["status"], "tools": metrics["tools"],
            "requests": replay.requests, "input_tokens": replay.input_tokens, "output_tokens": replay.output_tokens,
            "cost_units": replay.input_tokens + 4 * replay.output_tokens,
            "elapsed_ms": metrics["elapsed_ms"], "coverage_bytes": len(replay.covered),
            "policy": experience["selected_policy"], "version_before": before, "version_after": document["head"],
            "experience": experience["id"], "oracle_errors": replay.errors if case.family != "invalid_arguments" else []}


def aggregate(rows):
    count = len(rows)
    successes = sum(row["success"] for row in rows)
    totals = {key: sum(row[key] for row in rows)
              for key in ("tools", "requests", "input_tokens", "output_tokens", "cost_units", "elapsed_ms")}
    return {"tasks": count, "successes": successes, "success_rate": successes / count if count else None,
            **totals, "tools_per_task": totals["tools"] / count if count else None,
            "cost_units_per_task": totals["cost_units"] / count if count else None,
            "cost_units_per_success": totals["cost_units"] / successes if successes else None}


def compare(pairs):
    baseline = aggregate([p["baseline"] for p in pairs])
    adaptive = aggregate([p["adaptive"] for p in pairs])
    reduction = lambda key: 1 - adaptive[key] / baseline[key] if baseline[key] else None
    return {"baseline": baseline, "adaptive": adaptive,
            "tool_reduction": reduction("tools"), "request_reduction": reduction("requests"),
            "cost_reduction": reduction("cost_units"),
            "success_rate_delta": adaptive["success_rate"] - baseline["success_rate"] if pairs else None,
            "wins": sum(p["adaptive"]["success"] and not p["baseline"]["success"] for p in pairs),
            "regressions": sum(p["baseline"]["success"] and not p["adaptive"]["success"] for p in pairs)}


def benchmark(executable, tasks=100, holdout=40, seed=20260920, progress=None):
    if tasks < 1 or holdout < 1:
        raise ValueError("Task and holdout counts must be positive")
    training = cases(tasks, seed, "online")
    evaluation = cases(holdout, seed, "holdout")
    fingerprints = [{digest(case.prompt.encode()) for case in group} for group in (training, evaluation)]
    if fingerprints[0] & fingerprints[1]:
        raise ValueError("Training and holdout overlap")
    pairs = []
    with tempfile.TemporaryDirectory(prefix="zero-learning-benchmark-") as directory, Transport() as transport:
        roots = {arm: Path(directory) / arm for arm in ("baseline", "adaptive")}
        for root in roots.values():
            root.mkdir()
        trained_head = None
        for split, group in (("online", training), ("holdout", evaluation)):
            for index, case in enumerate(group):
                pair = {"task": case.identity, "split": split, "family": case.family,
                        "prompt_sha256": digest(case.prompt.encode()), "content_sha256": digest(case.content.encode()),
                        "file_bytes": len(case.content.encode()), "max_requests": case.budget, "explicit_limit": case.limit}
                # Alternate order so timing is not always biased to one arm.
                arms = ("baseline", "adaptive") if index % 2 == 0 else ("adaptive", "baseline")
                for arm in arms:
                    pair[arm] = run_case(executable, roots[arm], case, transport,
                                         frozen=arm == "baseline" or split == "holdout")
                if pair["baseline"]["policy"] != 0 or pair["baseline"]["version_after"] != "base":
                    raise RuntimeError("Baseline was contaminated by a learned policy")
                pairs.append(pair)
                if progress and ((index + 1) % 10 == 0 or index + 1 == len(group)):
                    progress(f"{split}: {index + 1}/{len(group)} paired tasks")
            if split == "online":
                trained_head = graph(roots["adaptive"])["head"]
        document = graph(roots["adaptive"])
        if document["head"] != trained_head:
            raise RuntimeError("Holdout leaked into strategy selection")
        # Retain exact promotion/evaluation records, not just aggregate wins.
        audit = [node for node in document["nodes"]
                 if node["kind"] in ("strategy_version", "evaluation", "improvement_candidate")]
    online_pairs = [p for p in pairs if p["split"] == "online"]
    held_pairs = [p for p in pairs if p["split"] == "holdout"]
    result = compare(held_pairs)
    families = {family: compare([p for p in held_pairs if p["family"] == family])
                for family in sorted({p["family"] for p in held_pairs})}
    gates = {"fewer_tools": result["tool_reduction"] > 0,
             "lower_modeled_cost": result["cost_reduction"] > 0,
             "higher_success_rate": result["success_rate_delta"] > 0,
             "no_success_regressions": result["regressions"] == 0,
             "no_oracle_errors": not any(p[a]["oracle_errors"] for p in pairs for a in roots),
             "promoted_strategy": trained_head != "base"}
    return {"schema": 1, "suite": SUITE, "seed": seed, "training_tasks": tasks, "holdout_tasks": holdout,
            "executable_sha256": digest(Path(executable).read_bytes()),
            "cost_model": {"kind": "synthetic-token-proxy", "tokenizer": "ceil(UTF-8 JSON bytes / 4)",
                           "input_weight": 1, "output_weight": 4, "paid_provider_calls": 0,
                           "scope": "coordinator requests including context and learned guidance; not a provider invoice"},
            "success_oracle": "Exit zero AND exact contiguous fixture-byte coverage; invalid-argument tasks remain failures",
            "online": compare(online_pairs), "holdout": result, "families": families,
            "checkpoints": [{"after_tasks": end, **compare(online_pairs[max(0, end - 20):end])}
                            for end in sorted(set([min(tasks, n) for n in (10, 20, 40, 60, 80, 100)] + [tasks]))],
            "gates": gates, "passed": all(gates.values()), "trained_version": trained_head,
            "audit": audit, "pairs": pairs}


def markdown(report):
    lines = [f"# ZeroCode learning benchmark: {report['training_tasks']} tasks", "",
             f"Suite `{report['suite']}`, seed `{report['seed']}`. Real executable and tools; scripted local model.",
             f"After {report['training_tasks']} online tasks, both strategies were frozen for {report['holdout_tasks']} unseen paired tasks.",
             "", "| Held-out metric | Frozen base | Learned | Change |", "|---|---:|---:|---:|"]
    result = report["holdout"]
    for title, key, change in (("Tool calls", "tools", "tool_reduction"),
                               ("Model requests", "requests", "request_reduction"),
                               ("Modeled cost units", "cost_units", "cost_reduction")):
        lines.append(f"| {title} | {result['baseline'][key]:,} | {result['adaptive'][key]:,} | {result[change]:+.1%} reduction |")
    lines.append(f"| Verified success | {result['baseline']['success_rate']:.1%} | {result['adaptive']['success_rate']:.1%} | {result['success_rate_delta'] * 100:+.1f} pp |")
    cost_per_success = [result[arm]["cost_units_per_success"] for arm in ("baseline", "adaptive")]
    costs = [f"{value:,.1f}" if value is not None else "unavailable" for value in cost_per_success]
    lines.append(f"| Cost units per verified success (including failures) | {costs[0]} | {costs[1]} | |")
    lines += ["", "Cost uses ceil(UTF-8 JSON bytes / 4), with weights 1 for input and 4 for output. "
              "It includes repeated context and policy guidance. No paid model calls; these are not billed tokens or dollars.",
              "Success requires exit zero and exact file-byte coverage under identical request budgets. "
              "Invalid-argument fixtures count as failures in both arms. Budgeted read successes measure resource efficiency, not reasoning accuracy.",
              "", "| Family | Tasks | Base success | Learned success | Tool reduction | Cost reduction |",
              "|---|---:|---:|---:|---:|---:|"]
    for family, row in report["families"].items():
        lines.append(f"| {family} | {row['baseline']['tasks']} | {row['baseline']['success_rate']:.0%} | {row['adaptive']['success_rate']:.0%} | {row['tool_reduction']:.1%} | {row['cost_reduction']:.1%} |")
    lines += ["", "| Online window ending at task | Tool reduction vs paired base | Cost reduction | Success delta |",
              "|---|---:|---:|---:|"]
    for row in report["checkpoints"]:
        lines.append(f"| {row['after_tasks']} | {row['tool_reduction']:.1%} | {row['cost_reduction']:.1%} | {row['success_rate_delta'] * 100:+.1f} pp |")
    lines += ["", f"All acceptance gates passed: **{report['passed']}**. Success regressions: {result['regressions']}.",
              "", "This demonstrates the bounded read/retry mutation vocabulary. It does not establish improvement "
              "on arbitrary coding tasks or live providers. The paired JSON retains every failure, selected version, "
              "measurement and admission evaluation; elapsed time is diagnostic and is not an acceptance gate.", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, default=ROOT / "dist/zero-code")
    parser.add_argument("--tasks", type=int, default=100)
    parser.add_argument("--holdout", type=int, default=40)
    parser.add_argument("--seed", type=int, default=20260920)
    parser.add_argument("--output", type=Path, help="Write JSON and a sibling Markdown report")
    parser.add_argument("--require-improvement", action="store_true", help="Fail unless all held-out gates pass")
    args = parser.parse_args()
    if not 1 <= args.tasks <= 200 or not 1 <= args.holdout <= 100:
        parser.error("Use 1–200 training tasks and 1–100 holdout tasks to stay within the bounded learning store")
    if args.output and args.output.suffix != ".json":
        parser.error("--output must have the .json extension")
    report = benchmark(args.executable.resolve(), args.tasks, args.holdout, args.seed,
                       progress=lambda message: print(message, file=sys.stderr, flush=True))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        args.output.with_suffix(".md").write_text(markdown(report))
    print(markdown(report))
    return 1 if args.require_improvement and not report["passed"] else 0


if __name__ == "__main__":
    sys.exit(main())
