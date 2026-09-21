#!/usr/bin/env python3
"""Compiler-backed program evolution. The Zero learner supplies the proposals.

Every accepted generation is a complete, independently buildable Zero package.
Only HEAD is mutable; it selects a graph, projection, executable and evaluation
as one unit. Candidate code never runs in the user's task workspace.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

_spec = importlib.util.spec_from_file_location("_zero_program_replay", Path(__file__).with_name("learning_benchmark.py"))
_replay = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _replay
_spec.loader.exec_module(_replay)
Case, Transport, cases, run_case = _replay.Case, _replay.Transport, _replay.cases, _replay.run_case


TARGETS = {"learningWindowValue", "learningRetryValue"}
VERSION = re.compile(r"^(base|g-[0-9a-f]{32})$")
EXPERIENCE = re.compile(r"^e-[0-9a-f]{32}\.jsonl$")
MAX_GENERATIONS = 32


class Rejected(Exception):
    pass


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def regular(path):
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or path.stat().st_nlink != 1:
        raise Rejected(f"Expected a regular, unlinked file: {path.name}")
    return path


def private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise Rejected(f"Unsafe program store: {path.name}")
    return path


def atomic_json(path, value):
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path):
    if regular(path).stat().st_size > 2 * 1024 * 1024:
        raise Rejected("Oversized program metadata")
    return json.loads(path.read_text())


def clean_environment(home):
    # No inherited provider keys, endpoint, preload hooks, proxy or learning flags.
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "SDKROOT") if key in os.environ}
    env.update(HOME=str(home), TMPDIR=str(home), XDG_CONFIG_HOME=str(home / "config"),
               XDG_STATE_HOME=str(home), ZERO_STALE="fail", PYTHONDONTWRITEBYTECODE="1",
               NO_PROXY="localhost,127.0.0.1", no_proxy="localhost,127.0.0.1")
    return env


def command(args, cwd, env, timeout=300):
    # Bounded, shell-free child processes. Kill the entire group on timeout.
    with subprocess.Popen([str(arg) for arg in args], cwd=cwd, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          start_new_session=True) as child:
        try:
            output, _ = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, 9)
            child.communicate()
            raise Rejected(f"Validation timed out: {Path(str(args[0])).name}") from None
    text = output.decode("utf-8", errors="replace")
    if child.returncode:
        phase = " ".join([Path(str(args[0])).name, *map(str, args[1:3])])
        raise Rejected(f"Validation failed ({child.returncode}) in {phase}: {text[-3000:]}")
    return text


def package_files(root):
    for name in ("zero.toml", "zero.graph"):
        yield regular(root / name)
    for directory in ("src", "native"):
        start = root / directory
        if start.is_symlink() or not start.is_dir():
            raise Rejected(f"Invalid package directory: {directory}")
        for path in sorted(start.rglob("*")):
            if path.is_symlink():
                raise Rejected("Symlinks are not allowed in program snapshots")
            if path.is_file():
                yield regular(path)


def hashes(root):
    return {str(path.relative_to(root)): digest(path) for path in package_files(root)}


def source_identity(root, compiler=None):
    identity = hashes(root)
    for name in ("scripts/learning_program.py", "scripts/learning_benchmark.py", ".tools/bin/zero"):
        identity[name] = digest(regular(compiler if name == ".tools/bin/zero" and compiler else root / name))
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def copy_package(source, destination):
    destination.mkdir(mode=0o700)
    for path in package_files(source):
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        target.chmod(0o600)


def install_bundle(root, destination):
    root = root.resolve(strict=True)
    compiler = Path(os.environ.get("ZERO_COMPILER", root / ".tools/bin/zero")).resolve(strict=True)
    identity = source_identity(root, compiler)
    destination = private_directory(destination)
    target = destination / ("p-" + identity[:24])
    if target.exists():
        if target.is_symlink() or source_identity(target) != identity:
            raise Rejected("Installed program bundle changed; existing installation retained")
        return target
    with tempfile.TemporaryDirectory(prefix=".bundle-", dir=destination) as temporary:
        folder = Path(temporary) / "program"
        copy_package(root, folder)
        (folder / ".tools/bin").mkdir(parents=True)
        shutil.copyfile(regular(compiler), folder / ".tools/bin/zero")
        (folder / ".tools/bin/zero").chmod(0o700)
        (folder / "scripts").mkdir()
        for name in ("learning_program.py", "learning_benchmark.py"):
            shutil.copyfile(root / "scripts" / name, folder / "scripts" / name)
        if source_identity(folder) != identity or source_identity(root, compiler) != identity:
            raise Rejected("Source changed while preparing the installed program bundle")
        os.rename(folder, target)
    return target


def validate_patches(patches):
    if not isinstance(patches, list) or not 1 <= len(patches) <= len(TARGETS):
        raise Rejected("Supply one or two function replacements")
    seen = set()
    for patch in patches:
        if not isinstance(patch, dict) or set(patch) != {"function", "body"}:
            raise Rejected("Invalid function replacement")
        name, body = patch["function"], patch["body"]
        if name not in TARGETS or name in seen:
            raise Rejected("Function is outside the evolution targets or is repeated")
        seen.add(name)
        if not isinstance(body, str) or not 1 <= len(body.encode()) <= 4096:
            raise Rejected("Invalid function body")
        # These scalar functions need no imports, calls, loops, strings or I/O.
        # Permit branches and arithmetic over policy, not executable capabilities.
        if re.search(r"[^A-Za-z0-9_\s{}();+*/%<>=!&|-]", body):
            raise Rejected("Only pure scalar function bodies can evolve")
        names = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", body))
        if not names <= {"policy", "return", "if", "else", "true", "false"}:
            raise Rejected("Calls, loops and declarations are outside the evolution vocabulary")
    return patches


def validation_cases():
    result = cases(20, 20260921, "program-holdout")
    result += [Case(f"boundary-{size}", "ascii_read", "x" * size, 6)
               for size in (0, 12000, 16384, 16385, 17000, 33000, 55000, 65000, 95000)]
    result += [Case("escaping", "unicode", 'żółw🐢\t"\\\n' * 3500),
               Case("explicit-min", "ascii_read", "abcdefghijkl", 20, 4),
               Case("explicit-max", "ascii_read", "x" * 55000, 20, 12000),
               Case("invalid", "invalid_arguments", "")]
    return result


def replay_gate(baseline, candidate, directory, env):
    roots = [directory / "baseline-work", directory / "candidate-work"]
    for root in roots:
        root.mkdir()
    pairs = []
    with Transport() as transport:
        for case in validation_cases():
            rows = [run_case(exe, root, case, transport, True, env_override=env)
                    for exe, root in zip((baseline, candidate), roots)]
            before, after = rows
            if before["oracle_errors"] or after["oracle_errors"]:
                raise Rejected(f"Byte-coverage oracle failed: {case.identity}")
            if before["success"] and not after["success"]:
                raise Rejected(f"Completion regression: {case.identity}")
            if after["tools"] > before["tools"] or after["requests"] > before["requests"]:
                raise Rejected(f"Execution regression: {case.identity}")
            pairs.append({"case": case.identity, "family": case.family,
                          "baseline": before, "candidate": after})
    totals = {arm: {metric: sum(row[arm][metric] for row in pairs)
                    for metric in ("tools", "requests", "cost_units", "success")}
              for arm in ("baseline", "candidate")}
    before, after = totals["baseline"], totals["candidate"]
    if after["tools"] >= before["tools"] or after["cost_units"] >= before["cost_units"]:
        raise Rejected("No measured reduction in tool calls and modeled request cost")
    return {"suite": "executable-graph-replay-v1", "cases": pairs, "totals": totals,
            "cost_model": "ceil-json-utf8-bytes/4; input=1, output=4"}


class ProgramStore:
    def __init__(self, root):
        self.root = root.resolve(strict=True)
        self.path = private_directory(private_directory(self.root / ".zero-agent") / "evolution")
        self.generations = private_directory(self.path / "generations")
        self.attempts = private_directory(self.path / "attempts")
        self.compiler = regular(self.root / ".tools/bin/zero")
        self.state_path = self.path / "head.json"

    @contextmanager
    def locked(self):
        fd = os.open(self.path / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise Rejected("Invalid program lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Rejected("Another program evaluation is in progress") from None
            yield
        finally:
            os.close(fd)

    def state(self):
        value = read_json(self.state_path)
        if value.get("schema") != 1 or not VERSION.fullmatch(value.get("head", "")):
            raise Rejected("Invalid program HEAD")
        if value.get("source") != source_identity(self.root):
            raise Rejected("Source checkout changed; use a fresh checkout for this program lineage")
        return value

    def generation(self, name):
        if not isinstance(name, str) or not VERSION.fullmatch(name):
            raise Rejected("Invalid program version")
        folder = self.generations / name
        if folder.is_symlink() or not folder.is_dir():
            raise Rejected("Unknown program generation")
        info = read_json(folder / "generation.json")
        if info.get("id") != name or info.get("files") != hashes(folder):
            raise Rejected("Program graph or projection changed after validation")
        if info.get("executable") != digest(regular(folder / "zero-code")):
            raise Rejected("Program executable changed after validation")
        if name != "base":
            evaluation = read_json(folder / "evaluation.json")
            if info.get("evaluation") != digest(folder / "evaluation.json") or evaluation.get("accepted") is not True:
                raise Rejected("Program evaluation is missing or changed")
        return folder

    def build(self, folder, env):
        command([self.compiler, "verify-projection", folder], folder, env)
        command([self.compiler, "build", "--target", "host", "--out", folder / "zero-code", folder], folder, env)
        command([folder / "zero-code", "--self-test"], folder, env, timeout=20)

    def seal(self, folder, identity, parent, source, evaluation=None):
        # Compiler caches are reproducible and are not part of an executable
        # generation. Retain source, evidence and the binary, not build debris.
        cache = folder / ".zero"
        if cache.exists():
            shutil.rmtree(cache)
        info = {"schema": 1, "id": identity, "parent": parent, "source": source,
                "files": hashes(folder), "executable": digest(folder / "zero-code"),
                "created": int(time.time())}
        if evaluation is not None:
            atomic_json(folder / "evaluation.json", evaluation)
            info["evaluation"] = digest(folder / "evaluation.json")
        atomic_json(folder / "generation.json", info)
        # Flush graph, projection and executable before HEAD can refer to them.
        for path in [*package_files(folder), folder / "zero-code"]:
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        for directory in [path for path in folder.rglob("*") if path.is_dir()] + [folder]:
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        destination = self.generations / identity
        os.rename(folder, destination)
        fd = os.open(self.generations, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return destination

    def initialize(self):
        if self.state_path.exists():
            return self.state()
        source = source_identity(self.root)
        # An orphan base from an interrupted first publication is reusable only
        # after its entire graph, binary and source identity have been verified.
        if (self.generations / "base").exists():
            base = self.generation("base")
            if read_json(base / "generation.json")["source"] != source:
                raise Rejected("An interrupted base belongs to a different checkout")
        else:
            with tempfile.TemporaryDirectory(prefix=".build-", dir=self.path) as temporary:
                temporary = Path(temporary)
                base = temporary / "package"
                copy_package(self.root, base)
                self.build(base, clean_environment(temporary))
                if source_identity(self.root) != source:
                    raise Rejected("Source changed while building the base")
                self.seal(base, "base", None, source)
        state = {"schema": 1, "head": "base", "source": source, "revision": 0}
        atomic_json(self.state_path, state)
        return state

    def evidence(self, request):
        identity = request.get("experience", "")
        if not isinstance(identity, str) or not EXPERIENCE.fullmatch(identity):
            raise Rejected("A completed experience is required")
        workspace = Path(request["workspace"]).resolve(strict=True)
        journal = workspace / ".zero-agent/program-learning/experiences" / identity
        current = workspace
        for part in journal.relative_to(workspace).parts:
            current /= part
            if current.is_symlink():
                raise Rejected("Symlinked experience")
        regular(journal)
        if journal.stat().st_size > 16 * 1024 * 1024:
            raise Rejected("Oversized experience")
        payload = journal.read_bytes()
        events = [json.loads(line) for line in payload.splitlines()]
        if not events or any(row.get("schema") != 1 or row.get("experience") != identity or row.get("seq") != i for i, row in enumerate(events)):
            raise Rejected("Incomplete experience journal")
        outcomes = [row["data"] for row in events if row.get("event") == "outcome"]
        if len(outcomes) != 1:
            raise Rejected("Exactly one final outcome is required")
        outcome = outcomes[0]
        if (outcome.get("complete") is not True or outcome.get("mode") != "adaptive"
                or outcome.get("status") not in ("completed", "failed")
                or outcome.get("program_version") != request["parent"]
                or outcome.get("metrics", {}).get("tests_failed", 0)):
            raise Rejected("Experience does not qualify for program evolution")
        for patch in request["patches"]:
            if patch["function"] == "learningWindowValue":
                if outcome.get("status") != "completed" or outcome.get("read_total", 0) <= 16384:
                    raise Rejected("No complete ranged-read evidence")
            elif outcome.get("invalid_repeats", 0) < 3:
                raise Rejected("No permanent invalid-argument repetition evidence")
        # The coordinator appends the evaluation after this call returns. Pin
        # the exact observed prefix, rather than hashing a still-open journal.
        return {"id": identity, "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload),
                "pattern": outcome["pattern"], "read_total": outcome["read_total"],
                "invalid_repeats": outcome["invalid_repeats"]}

    def evolve(self, request):
        validate_patches(request.get("patches"))
        state = self.state()
        if request.get("parent") != state["head"]:
            raise Rejected("Stale running program; restart before proposing another generation")
        evidence = self.evidence(request)
        parent = self.generation(state["head"])
        if len(list(self.generations.iterdir())) >= MAX_GENERATIONS:
            raise Rejected("Program history is full; no generations were pruned")
        identity = "g-" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix=".build-", dir=self.path) as temporary:
            temporary = Path(temporary)
            candidate = temporary / "package"
            copy_package(parent, candidate)
            env = clean_environment(temporary)
            changes = []
            for index, patch in enumerate(request["patches"]):
                body = temporary / f"body-{index}.0"
                body.write_text(patch["body"] + "\n")
                before = command([self.compiler, "view", "--fn", patch["function"], candidate], candidate, env)
                command([self.compiler, "patch", candidate, "--replace-fn", patch["function"],
                         "--body-file", body], candidate, env)
                after = command([self.compiler, "view", "--fn", patch["function"], candidate], candidate, env)
                changes.append({"function": patch["function"], "before": before, "after": after})
            command([self.compiler, "export", candidate], candidate, env)
            self.build(candidate, env)
            report = replay_gate(parent / "zero-code", candidate / "zero-code", temporary, env)
            evaluation = {"accepted": True, "parent": state["head"], "version": identity,
                          "experience": evidence, "changes": changes, "replay": report}
            # Compare the source and HEAD again at the publication boundary.
            if self.state() != state:
                raise Rejected("Program changed during evaluation")
            self.generation(state["head"])
            self.seal(candidate, identity, state["head"], state["source"], evaluation)
            atomic_json(self.state_path, {**state, "head": identity, "revision": state["revision"] + 1})
        return {"accepted": True, "version": identity, "parent": state["head"],
                "message": "Validated executable graph selected for the next launch.",
                "metrics": report["totals"]}

    def rollback(self, target):
        state = self.state()
        self.generation(target)
        record = {"action": "rollback", "from": state["head"], "to": target,
                  "revision": state["revision"] + 1}
        atomic_json(self.attempts / ("rollback-" + uuid.uuid4().hex + ".json"), record)
        atomic_json(self.state_path, {**state, "head": target, "revision": record["revision"]})
        return record

    def history(self):
        state = self.state()
        versions = [read_json(self.generation(path.name) / "generation.json")
                    for path in sorted(self.generations.iterdir())]
        return {**state, "versions": [{key: row[key] for key in ("id", "parent", "created")}
                                    for row in versions],
                "attempts": [read_json(path) for path in sorted(self.attempts.glob("*.json"))]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--request")
    mode.add_argument("--bundle", type=Path)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    store = None
    request = None
    try:
        if args.bundle is not None:
            print(install_bundle(args.root, args.bundle))
            return 0
        store = ProgramStore(args.root)
        with store.locked():
            if args.request:
                if len(args.request.encode()) > 16384:
                    raise Rejected("Oversized evolution request")
                request = json.loads(args.request)
                if not isinstance(request, dict) or request.get("action") != "evolve":
                    raise Rejected("Unknown evolution action")
                result = store.evolve(request)
                atomic_json(store.attempts / ("accepted-" + uuid.uuid4().hex + ".json"), result)
                print(json.dumps(result))
                return 0
            arguments = args.arguments
            if arguments[:1] == ["--"]:
                arguments = arguments[1:]
            state = store.initialize()
            if "--learning-history" in arguments:
                print(json.dumps(store.history(), indent=2))
                return 0
            if "--learning-rollback" in arguments:
                index = arguments.index("--learning-rollback")
                if index + 1 >= len(arguments):
                    raise Rejected("Supply a program generation or base")
                print(json.dumps(store.rollback(arguments[index + 1])))
                return 0
            executable = store.generation(state["head"]) / "zero-code"
            env = os.environ.copy()
            env["ZERO_LEARNING_PROGRAM_ROOT"] = str(store.root)
            env["ZERO_LEARNING_PROGRAM_VERSION"] = state["head"]
        # Release the lock before the task; its final experience can now evolve.
        os.execve(executable, [str(executable), *arguments], env)
    except (Rejected, OSError, ValueError, KeyError, TypeError, RuntimeError,
            subprocess.TimeoutExpired) as error:
        result = {"accepted": False, "reason": str(error)[:3500]}
        if request is not None and store is not None:
            result["parent"] = request.get("parent") if isinstance(request, dict) else None
            try:
                record = {**result, "proposal": request}
                atomic_json(store.attempts / ("rejected-" + uuid.uuid4().hex + ".json"), record)
            except OSError:
                pass
        print(json.dumps(result))
        # Rejections are data to the learner; launch errors are CLI failures.
        return 0 if args.request else 1


if __name__ == "__main__":
    raise SystemExit(main())
