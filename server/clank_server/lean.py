"""Checking candidate proofs with Lean.

MVP implementation: every check runs a fresh `lean --stdin --json` process on a small file that
restates the goal as a theorem. This pays the import cost per check; a pool of warm Lean REPL
processes will replace it later behind the same interface.
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

DECL_NAME = "clank_check"
ALLOWED_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}
_AXIOMS_RE = re.compile(rf"'{DECL_NAME}' depends on axioms: \[(.*)\]", re.DOTALL)
_NO_AXIOMS = f"'{DECL_NAME}' does not depend on any axioms"


@dataclass
class CheckResult:
    ok: bool
    errors: list[str] = field(default_factory=list)


def render(req: ProveRequest, proof: str) -> str:
    """A self-contained Lean file stating the goal as `clank_check`, proved by `proof`."""
    lines = [f"import {m}" for m in req.imports if m != "Init"]
    # Unknown identifiers in a restated goal must be errors, not auto-bound variables.
    lines.append("set_option autoImplicit false")
    if req.universes:
        lines.append("universe " + " ".join(req.universes))
    binders = "".join(f" {binder(h.name, h.type)}" for h in req.hypotheses)
    lines.append(f"theorem {DECL_NAME}{binders} :")
    lines.append(textwrap.indent(req.target, "    "))
    lines[-1] += " := by"
    lines.append(textwrap.indent(proof, "  "))
    lines.append(f"#print axioms {DECL_NAME}")
    return "\n".join(lines) + "\n"


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        # `lake env lean` spawns lean as a grandchild; kill the whole tree.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False
        )
    proc.kill()


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

    async def check(self, req: ProveRequest, proof: str) -> CheckResult:
        code, messages, other = await self.run(render(req, proof))
        if code is None:
            return CheckResult(False, [f"Lean timed out after {self._settings.lean_timeout}s"])
        errors = [m["data"] for m in messages if m.get("severity") == "error"]
        errors += [m["data"] for m in messages if m.get("kind") == "hasSorry"]
        axioms_ok = False
        for m in messages:
            data = m.get("data", "")
            if data.startswith(_NO_AXIOMS):
                axioms_ok = True
            elif ax := _AXIOMS_RE.match(data):
                used = {a.strip() for a in ax.group(1).split(",") if a.strip()}
                axioms_ok = used <= ALLOWED_AXIOMS
                if not axioms_ok:
                    errors.append(f"disallowed axioms: {sorted(used - ALLOWED_AXIOMS)}")
        if not errors and not axioms_ok:
            errors.append("could not confirm the axioms used by the proof")
        if code != 0 and not errors:
            errors.append("\n".join(other) or f"Lean exited with code {code}")
        return CheckResult(not errors, errors)

    async def check_statement(self, req: ProveRequest) -> CheckResult:
        """Check that the goal can be restated as a theorem outside the user's file. If it cannot
        (e.g. pretty-printing does not round-trip), candidates cannot be checked server-side."""
        code, messages, other = await self.run(render(req, "sorry"))
        if code is None:
            return CheckResult(False, [f"Lean timed out after {self._settings.lean_timeout}s"])
        errors = [m["data"] for m in messages if m.get("severity") == "error"]
        if not messages and other:
            errors += other
        return CheckResult(not errors, errors)
