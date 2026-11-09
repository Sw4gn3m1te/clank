"""Checking candidate proofs with Lean.

All candidates of a round are checked together: one `theorem clank_check_<i>` per candidate, each
followed by `#print axioms`. The source is split into a *header* (the imports, slow to load) and a
*body* (everything else). Two checkers run it:

- `LeanChecker` runs a fresh `lean --stdin --json` process per batch, paying the import cost every
  time (about 20 s for Mathlib).
- `ReplChecker` keeps a pool of Lean REPL processes (github.com/leanprover-community/repl) that
  load each header once and then check bodies against it in well under a second.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import subprocess
import textwrap
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .prompt import binder
from .protocol import ProveRequest

log = logging.getLogger(__name__)

DECL_PREFIX = "clank_check"
ALLOWED_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}
# The names are qualified when the check runs inside a namespace of the file prefix.
_AXIOMS_RE = re.compile(r"'(.+?)' depends on axioms: \[(.*)\]", re.DOTALL)
_NO_AXIOMS_RE = re.compile(r"'(.+?)' does not depend on any axioms")
_UNIVERSE_RE = re.compile(r"^\s*universe\s+([^\n]+)", re.MULTILINE)


@dataclass
class LeanError:
    # 1-based line within the candidate proof, or None if the error is not inside the proof
    # (e.g. "unsolved goals", which Lean reports at `by`).
    line: int | None
    message: str
    # The error only says that the proof uses `sorry` (expected in proof sketches).
    sorry: bool = False


@dataclass
class CheckResult:
    ok: bool
    errors: list[LeanError] = field(default_factory=list)
    # Other messages inside the proof, as (line, column, text), both relative to the proof
    # (1-based line, 0-based column), e.g. the output of `trace_state`.
    infos: list[tuple[int, int, str]] = field(default_factory=list)

    def first_error(self) -> str:
        return self.errors[0].message.strip().splitlines()[0] if self.errors else "unknown error"

    @property
    def only_sorry(self) -> bool:
        """The proof is correct except that it uses `sorry`."""
        return bool(self.errors) and all(e.sorry for e in self.errors)


@dataclass
class _Span:
    first: int  # first line of the theorem
    proof: int  # first line of the proof
    last: int  # line of `#print axioms`


@dataclass
class _Run:
    """Outcome of running one body: messages with body-relative lines (line < 1 means the
    header), or `timeout`/`failure` when Lean produced no usable result."""
    messages: list[dict] = field(default_factory=list)
    timeout: bool = False
    failure: str | None = None


def header(req: ProveRequest) -> str:
    # The tactic leaves itself out of `imports`, but a prefix can contain `set_option clank.*`.
    imports = [*req.imports, "Clank"] if req.prefix else req.imports
    return "\n".join(f"import {m}" for m in imports if m != "Init")


def render_body(req: ProveRequest, proofs: list[str]) -> tuple[str, list[_Span]]:
    """The goal stated once per proof, as `clank_check_<i>`. Span lines are 1-based within the body."""
    # Unknown identifiers in a restated goal must be errors, not auto-bound variables.
    out = [*req.prefix.splitlines(), "set_option autoImplicit false"]
    out += [f"open {o}" for o in req.opens]
    # Lean rejects declaring a universe that the prefix has already declared.
    declared = {u for names in _UNIVERSE_RE.findall(req.prefix) for u in names.split()}
    if universes := [u for u in req.universes if u not in declared]:
        out.append("universe " + " ".join(universes))
    binders = "".join(f" {binder(h.name, h.type)}" for h in req.hypotheses)
    spans = []
    for i, proof in enumerate(proofs):
        out.append("")
        first = len(out) + 1
        out.append(f"theorem {DECL_PREFIX}_{i}{binders} :")
        out += textwrap.indent(req.target, "    ").splitlines()
        out[-1] += " := by"
        start = len(out) + 1
        out += ["  " + l if l.strip() else "" for l in proof.splitlines()]
        out.append(f"#print axioms {DECL_PREFIX}_{i}")
        spans.append(_Span(first, start, len(out)))
    return "\n".join(out) + "\n", spans


def render(req: ProveRequest, proofs: list[str]) -> tuple[str, list[_Span]]:
    """A complete Lean file (header and body). Span lines are 1-based within the file."""
    head = header(req)
    body, spans = render_body(req, proofs)
    if not head:
        return body, spans
    n = len(head.splitlines())
    return head + "\n" + body, [_Span(s.first + n, s.proof + n, s.last + n) for s in spans]


def _well_formed(proof: str) -> bool:
    """Reject text that would break out of its theorem, e.g. an unterminated block comment that
    would swallow the candidates after it in the same file."""
    return proof.count("/-") == proof.count("-/")


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        # `lake env ...` spawns its command as a grandchild; kill the whole tree.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False
        )
    proc.kill()


def _results(run: _Run, spans: list[_Span], timeout: float, stubs: bool = False) -> list[CheckResult]:
    """Attribute the messages of a run to the candidates whose theorems they belong to. With
    `stubs`, a candidate may depend on `sorry` through the prefix's stubbed theorems. A `sorry`
    of its own is still an error, and the tactic's final check rejects both."""
    allowed = ALLOWED_AXIOMS | {"sorryAx"} if stubs else ALLOWED_AXIOMS
    errors: list[list[LeanError]] = [[] for _ in spans]
    infos: list[list[tuple[int, int, str]]] = [[] for _ in spans]
    axioms_ok = [False] * len(spans)
    global_errors: list[str] = []
    for m in run.messages:
        line = m.get("pos", {}).get("line", 0)
        data = m.get("data", "")
        k = next((k for k, s in enumerate(spans) if s.first <= line <= s.last), None)
        is_error = m.get("severity") == "error" or m.get("kind") == "hasSorry"
        if k is None:
            if m.get("severity") == "error":
                global_errors.append(data)
            continue
        rel = line - spans[k].proof + 1
        in_proof = line < spans[k].last and rel >= 1
        if is_error:
            sorry = m.get("kind") == "hasSorry" or data.startswith("declaration uses 'sorry'")
            errors[k].append(LeanError(rel if in_proof else None, data, sorry))
        elif ax := _AXIOMS_RE.match(data):
            used = {a.strip() for a in ax.group(2).split(",") if a.strip()}
            axioms_ok[k] = used <= allowed
            if not axioms_ok[k]:
                bad = sorted(used - allowed)
                errors[k].append(LeanError(None, f"disallowed axioms: {bad}", bad == ["sorryAx"]))
        elif _NO_AXIOMS_RE.match(data):
            axioms_ok[k] = True
        elif in_proof:
            # Proof lines are indented by two spaces in the rendered file.
            col = m.get("pos", {}).get("column", 2) - 2
            infos[k].append((rel, col, data))

    results = []
    for k in range(len(spans)):
        errs = errors[k]
        if run.timeout:
            errs = [LeanError(None, f"Lean timed out after {timeout}s")]
        elif global_errors:
            errs = [LeanError(None, e) for e in global_errors] + errs
        elif not errs and not axioms_ok[k]:
            errs = [LeanError(None, run.failure or "could not confirm the axioms used by the proof")]
        results.append(CheckResult(not errs, errs, infos[k]))
    return results


class LeanChecker:
    """Checks each batch with a fresh `lean` process."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._sem = asyncio.Semaphore(settings.max_parallel_checks)

    def _command(self) -> list[str]:
        if self._settings.lean_project is not None:
            return ["lake", "env", "lean", "--stdin", "--json"]
        return ["lean", "--stdin", "--json"]

    def _run_sync(self, source: str) -> tuple[int | None, str, str]:
        proc = subprocess.Popen(
            self._command(),
            cwd=self._settings.lean_project,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            out, err = proc.communicate(source.encode(), timeout=self._settings.lean_timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.communicate()
            return None, "", ""
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    async def _run(self, head: str, body: str) -> _Run:
        offset = len(head.splitlines()) if head else 0
        source = head + "\n" + body if head else body
        async with self._sem:
            code, out, err = await asyncio.to_thread(self._run_sync, source)
        if code is None:
            return _Run(timeout=True)
        messages, other = [], [l for l in err.splitlines() if l.strip()]
        for line in out.splitlines():
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                if line.strip():
                    other.append(line)
                continue
            if "pos" in m:
                m["pos"]["line"] -= offset
            messages.append(m)
        failure = "\n".join(other) or (f"Lean exited with code {code}" if code else None)
        return _Run(messages, failure=failure)

    async def check_many(self, req: ProveRequest, proofs: list[str]) -> list[CheckResult]:
        """Check each proof of the goal. Results are in the same order as `proofs`."""
        results: list[CheckResult | None] = [
            None if _well_formed(p) else CheckResult(False, [LeanError(None, "unbalanced comment")])
            for p in proofs
        ]
        todo = [i for i, r in enumerate(results) if r is None]
        if todo:
            body, spans = render_body(req, [proofs[i] for i in todo])
            run = await self._run(header(req), body)
            for i, r in zip(todo, _results(run, spans, self._settings.lean_timeout, bool(req.prefix))):
                results[i] = r
        return results  # type: ignore[return-value]

    async def check(self, req: ProveRequest, proof: str) -> CheckResult:
        return (await self.check_many(req, [proof]))[0]

    async def check_statement(self, req: ProveRequest) -> CheckResult:
        """Check that the goal can be restated as a theorem outside the user's file. If it cannot
        (e.g. pretty-printing does not round-trip), candidates cannot be checked server-side."""
        body, _ = render_body(req, ["sorry"])
        run = await self._run(header(req), body)
        if run.timeout:
            return CheckResult(False, [LeanError(None, f"Lean timed out after {self._settings.lean_timeout}s")])
        errors = [LeanError(None, m["data"]) for m in run.messages if m.get("severity") == "error"]
        if not run.messages and run.failure:
            errors.append(LeanError(None, run.failure))
        return CheckResult(not errors, errors)

    async def close(self) -> None:
        pass


class _ReplWorker:
    """One Lean REPL process. Not thread-safe: the pool hands it to one task at a time."""

    # Restart after this many commands: the REPL keeps every environment it has produced.
    MAX_COMMANDS = 500
    # Import sets kept loaded per worker.
    MAX_HEADERS = 4

    def __init__(self, repl: Path, settings: Settings):
        self._repl = repl
        self._settings = settings
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._headers: dict[str, int] = {}  # header source -> REPL environment
        self._commands = 0

    def _start(self) -> None:
        # The REPL needs `lake env` to find Lean's libraries. Without a project, run it from its
        # own package, which pins the matching toolchain.
        cwd = self._settings.lean_project or next(
            (d for d in self._repl.parents if (d / "lean-toolchain").exists()), None
        )
        self._proc = subprocess.Popen(
            ["lake", "env", str(self._repl)],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
        self._lines = queue.Queue()
        self._headers = {}
        self._commands = 0
        threading.Thread(target=self._read, args=(self._proc, self._lines), daemon=True).start()

    @staticmethod
    def _read(proc: subprocess.Popen, lines: queue.Queue) -> None:
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            _kill_tree(self._proc)
        self._proc = None

    def _ensure_running(self) -> None:
        """(Re)start the REPL if it is not running or has served enough commands. Restarting
        forgets the loaded headers."""
        if self._proc is None or self._proc.poll() is not None or self._commands >= self.MAX_COMMANDS:
            self.stop()
            self._start()

    def _send(self, cmd: dict, timeout: float) -> dict:
        """Send one command and return the REPL's JSON reply. Raises TimeoutError or RuntimeError,
        after which the worker is stopped."""
        self._commands += 1
        self._proc.stdin.write(json.dumps(cmd) + "\n\n")
        self._proc.stdin.flush()
        reply: list[str] = []
        while True:
            try:
                line = self._lines.get(timeout=timeout)
            except queue.Empty:
                self.stop()
                raise TimeoutError from None
            if line is None:
                self.stop()
                raise RuntimeError("the Lean REPL exited")
            if line.strip():
                reply.append(line)
            elif reply:
                return json.loads("".join(reply))

    def run(self, head: str, body: str) -> _Run:
        timeout = self._settings.lean_timeout
        try:
            self._ensure_running()
            env = self._headers.get(head)
            if env is None:
                r = self._send({"cmd": head} if head else {"cmd": ""}, timeout)
                if "env" not in r or any(m.get("severity") == "error" for m in r.get("messages", [])):
                    errs = [m["data"] for m in r.get("messages", []) if m.get("severity") == "error"]
                    return _Run([{"severity": "error", "pos": {"line": 0}, "data": e} for e in errs],
                                failure=r.get("message"))
                if len(self._headers) >= self.MAX_HEADERS:
                    self._headers.pop(next(iter(self._headers)))
                env = self._headers[head] = r["env"]
            r = self._send({"cmd": body, "env": env}, timeout)
        except TimeoutError:
            return _Run(timeout=True)
        except (RuntimeError, OSError, ValueError) as e:
            return _Run(failure=f"Lean REPL failed: {e}")
        if "messages" not in r and "message" in r:  # the REPL reports its own errors this way
            return _Run(failure=r["message"])
        return _Run(r.get("messages", []))


class ReplChecker(LeanChecker):
    """Checks batches with a pool of warm Lean REPL processes."""

    def __init__(self, settings: Settings, repl: Path):
        super().__init__(settings)
        self._idle: asyncio.Queue[_ReplWorker] = asyncio.Queue()
        self._workers = [_ReplWorker(repl, settings) for _ in range(max(1, settings.max_parallel_checks))]
        for w in self._workers:
            self._idle.put_nowait(w)

    async def _run(self, head: str, body: str) -> _Run:
        worker = await self._idle.get()
        try:
            return await asyncio.to_thread(worker.run, head, body)
        finally:
            self._idle.put_nowait(worker)

    async def warm(self, imports: list[str]) -> None:
        """Load `imports` in every worker ahead of the first request."""
        head = "\n".join(f"import {m}" for m in imports if m != "Init")
        await asyncio.gather(*(self._run(head, "") for _ in self._workers))

    async def close(self) -> None:
        for w in self._workers:
            await asyncio.to_thread(w.stop)


def make_checker(settings: Settings) -> LeanChecker:
    if settings.repl_path is not None:
        return ReplChecker(settings, settings.repl_path)
    return LeanChecker(settings)
