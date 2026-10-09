"""Checking candidate proofs with Lean.

Each batch of candidates is written to one file, one `theorem clank_check_<i>` per candidate, and
checked by a single `lean --stdin --json` process. Lean elaborates the theorems in parallel, and the
imports (e.g. Mathlib) are loaded once per batch instead of once per candidate. A pool of warm Lean
REPL processes can replace this later behind the same interface.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import textwrap
from dataclasses import dataclass, field

from .config import Settings
from .prompt import binder
from .protocol import ProveRequest

DECL_PREFIX = "clank_check"
ALLOWED_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}
_AXIOMS_RE = re.compile(r"'(\w+)' depends on axioms: \[(.*)\]", re.DOTALL)
_NO_AXIOMS_RE = re.compile(r"'(\w+)' does not depend on any axioms")


@dataclass
class LeanError:
    # 1-based line within the candidate proof, or None if the error is not inside the proof
    # (e.g. "unsolved goals", which Lean reports at `by`).
    line: int | None
    message: str


@dataclass
class CheckResult:
    ok: bool
    errors: list[LeanError] = field(default_factory=list)

    def first_error(self) -> str:
        return self.errors[0].message.strip().splitlines()[0] if self.errors else "unknown error"


@dataclass
class _Span:
    first: int  # first line of the theorem
    proof: int  # first line of the proof
    last: int  # line of `#print axioms`


def header(req: ProveRequest) -> list[str]:
    lines = [f"import {m}" for m in req.imports if m != "Init"]
    # Unknown identifiers in a restated goal must be errors, not auto-bound variables.
    lines.append("set_option autoImplicit false")
    lines += [f"open {o}" for o in req.opens]
    if req.universes:
        lines.append("universe " + " ".join(req.universes))
    return lines


def render(req: ProveRequest, proofs: list[str]) -> tuple[str, list[_Span]]:
    """A Lean file stating the goal once per proof, as `clank_check_<i>`."""
    out = header(req)  # one entry per physical line
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


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        # `lake env lean` spawns lean as a grandchild; kill the whole tree.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False
        )
    proc.kill()


def _well_formed(proof: str) -> bool:
    """Reject text that would break out of its theorem, e.g. an unterminated block comment that
    would swallow the candidates after it in the same file."""
    return proof.count("/-") == proof.count("-/")


class LeanChecker:
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

    async def run(self, source: str) -> tuple[int | None, list[dict], list[str]]:
        """Run Lean on `source`. Returns (exit code or None on timeout, messages, non-JSON output)."""
        async with self._sem:
            code, out, err = await asyncio.to_thread(self._run_sync, source)
        messages, other = [], [l for l in err.splitlines() if l.strip()]
        for line in out.splitlines():
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                if line.strip():
                    other.append(line)
        return code, messages, other

    async def check_many(self, req: ProveRequest, proofs: list[str]) -> list[CheckResult]:
        """Check each proof of the goal. Results are in the same order as `proofs`."""
        results: list[CheckResult | None] = [
            None if _well_formed(p) else CheckResult(False, [LeanError(None, "unbalanced comment")])
            for p in proofs
        ]
        todo = [i for i, r in enumerate(results) if r is None]
        if not todo:
            return results  # type: ignore[return-value]
        source, spans = render(req, [proofs[i] for i in todo])
        code, messages, other = await self.run(source)

        errors: list[list[LeanError]] = [[] for _ in todo]
        axioms_ok = [False] * len(todo)
        global_errors: list[str] = []
        for m in messages:
            line = m.get("pos", {}).get("line", 0)
            data = m.get("data", "")
            k = next((k for k, s in enumerate(spans) if s.first <= line <= s.last), None)
            is_error = m.get("severity") == "error" or m.get("kind") == "hasSorry"
            if k is None:
                if is_error:
                    global_errors.append(data)
                continue
            if is_error:
                rel = line - spans[k].proof + 1
                errors[k].append(LeanError(rel if line < spans[k].last and rel >= 1 else None, data))
            elif ax := _AXIOMS_RE.match(data):
                used = {a.strip() for a in ax.group(2).split(",") if a.strip()}
                axioms_ok[k] = used <= ALLOWED_AXIOMS
                if not axioms_ok[k]:
                    errors[k].append(
                        LeanError(None, f"disallowed axioms: {sorted(used - ALLOWED_AXIOMS)}")
                    )
            elif _NO_AXIOMS_RE.match(data):
                axioms_ok[k] = True

        for k, i in enumerate(todo):
            errs = errors[k]
            if code is None:
                errs = [LeanError(None, f"Lean timed out after {self._settings.lean_timeout}s")]
            elif global_errors:
                errs = [LeanError(None, e) for e in global_errors] + errs
            elif not errs and not axioms_ok[k]:
                msg = "\n".join(other) or "could not confirm the axioms used by the proof"
                errs = [LeanError(None, msg)]
            results[i] = CheckResult(not errs, errs)
        return results  # type: ignore[return-value]

    async def check(self, req: ProveRequest, proof: str) -> CheckResult:
        return (await self.check_many(req, [proof]))[0]

    async def check_statement(self, req: ProveRequest) -> CheckResult:
        """Check that the goal can be restated as a theorem outside the user's file. If it cannot
        (e.g. pretty-printing does not round-trip), candidates cannot be checked server-side."""
        source, _ = render(req, ["sorry"])
        code, messages, other = await self.run(source)
        if code is None:
            return CheckResult(False, [LeanError(None, f"Lean timed out after {self._settings.lean_timeout}s")])
        errors = [LeanError(None, m["data"]) for m in messages if m.get("severity") == "error"]
        if not messages and other:
            errors += [LeanError(None, o) for o in other]
        return CheckResult(not errors, errors)
