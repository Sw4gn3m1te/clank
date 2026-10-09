"""Proof search: rounds of sampling and checking, with repair of failed attempts, then cleanup.

Each round draws `samples` proofs from the model and checks all new ones with Lean in a single
batch. A "fresh" round samples from the plain goal. A "repair" round picks the most promising
failures so far and asks the model to fix them, showing Lean's errors. The search stops at the
first round that produces a verified proof, which is then shortened by `cleanup.minimize`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from . import prompt
from .cleanup import minimize
from .config import Settings
from .lean import CheckResult, LeanChecker
from .llm import LLMClient, LLMError
from .protocol import Candidate, ProveRequest, ProveResponse

log = logging.getLogger(__name__)

ROUND_KINDS = ("fresh", "repair")


@dataclass
class Failure:
    proof: str
    result: CheckResult

    def score(self) -> tuple[float, int]:
        """Higher is more promising: how far into the proof the first error is, then fewer errors.
        A proof that runs to the end but leaves goals open scores highest."""
        n = len(self.proof.splitlines())
        lines = [e.line for e in self.result.errors if e.line is not None]
        if lines:
            progress = (min(lines) - 1) / n
        elif any(e.message.startswith("unsolved goals") for e in self.result.errors):
            progress = 1.0
        else:  # timeout, disallowed axioms, ...
            progress = -1.0
        return progress, -len(self.result.errors)


def format_errors(result: CheckResult, limit: int = 3, width: int = 600) -> str:
    parts = []
    for e in result.errors[:limit]:
        msg = e.message.strip()
        if len(msg) > width:
            msg = msg[:width] + " …"
        parts.append(f"line {e.line}: {msg}" if e.line is not None else msg)
    return "\n".join(parts)


def _split(n: int, k: int) -> list[int]:
    """Split `n` samples into `k` near-equal positive parts (fewer parts if n < k)."""
    k = min(n, k)
    return [n // k + (1 if i < n % k else 0) for i in range(k)]


class Prover:
    def __init__(self, settings: Settings, llm: LLMClient, checker: LeanChecker):
        self._settings = settings
        self._llm = llm
        self._checker = checker

    async def _sample_repairs(self, req: ProveRequest, targets: list[Failure], n: int) -> list[str]:
        style = self._settings.prompt_style
        jobs = [
            self._llm.sample(prompt.messages(req, style, (f.proof, format_errors(f.result))), k)
            for f, k in zip(targets, _split(n, len(targets)))
        ]
        results = await asyncio.gather(*jobs, return_exceptions=True)
        replies = [r for rs in results if isinstance(rs, list) for r in rs]
        if not replies and results and isinstance(results[0], BaseException):
            raise results[0]
        return replies

    async def prove(self, req: ProveRequest) -> ProveResponse:
        s = self._settings
        start = time.monotonic()
        n = min(req.samples, s.max_samples)
        # Check that the goal can be restated server-side while the model is sampling.
        statement = asyncio.create_task(self._checker.check_statement(req))
        seen: set[str] = set()
        failures: list[Failure] = []
        repaired: set[str] = set()
        rounds: list[dict] = []
        verified: list[str] = []
        error: str | None = None
        statement_checked = False
        # Leave the tactic time to re-check the proof before its request times out.
        budget = s.time_budget if req.timeout is None else min(s.time_budget, req.timeout - 10)
        last_check = 0.0  # duration of the latest Lean batch, to predict the next one

        def fits(cost: float) -> bool:
            return time.monotonic() - start + cost <= budget

        try:
            for i, kind in enumerate(s.schedule):
                if i > 0 and not fits(rounds[-1]["seconds"]):
                    break
                t0 = time.monotonic()
                targets = []
                if kind == "repair":
                    pool = sorted(
                        (f for f in failures if f.proof not in repaired),
                        key=Failure.score,
                        reverse=True,
                    )
                    targets = pool[: s.repair_width]
                    repaired.update(f.proof for f in targets)
                try:
                    if targets:
                        replies = await self._sample_repairs(req, targets, n)
                    else:
                        kind = "fresh"  # nothing to repair yet
                        replies = await self._llm.sample(prompt.messages(req, s.prompt_style), n)
                except LLMError as e:
                    error = f"inference failed: {e}"
                    break

                candidates = []
                for r in replies:
                    p = prompt.extract_proof(r)
                    if p and p not in seen:
                        seen.add(p)
                        candidates.append(p)

                if candidates and not statement_checked:
                    statement_checked = True
                    stmt = await statement
                    if not stmt.ok:
                        # The tactic re-checks everything anyway, so hand over unchecked candidates.
                        log.warning("cannot restate goal server-side: %s", stmt.first_error())
                        return ProveResponse(
                            proofs=[Candidate(tactic=c, verified=False) for c in candidates],
                            message=f"server could not restate the goal: {stmt.first_error()}",
                            stats={"rounds": rounds, "seconds": time.monotonic() - start},
                        )

                t1 = time.monotonic()
                results = await self._checker.check_many(req, candidates) if candidates else []
                if candidates:
                    last_check = time.monotonic() - t1
                ok = [c for c, r in zip(candidates, results) if r.ok]
                failures += [Failure(c, r) for c, r in zip(candidates, results) if not r.ok]
                rounds.append(
                    {
                        "kind": kind,
                        "samples": len(replies),
                        "candidates": len(candidates),
                        "verified": len(ok),
                        "seconds": round(time.monotonic() - t0, 2),
                    }
                )
                log.info("round %d (%s): %s", i, kind, rounds[-1])
                if ok:
                    verified = sorted(ok, key=len)
                    break
        finally:
            statement.cancel()

        stats: dict = {"rounds": rounds}
        if not verified:
            stats["seconds"] = round(time.monotonic() - start, 2)
            if error is None:
                best = max(failures, key=Failure.score, default=None)
                error = f"no proof found in {len(rounds)} rounds ({len(seen)} distinct candidates)"
                if best:
                    error += f"; most promising attempt failed with: {best.result.first_error()}"
            return ProveResponse(proofs=[], message=error, stats=stats)

        best = verified[0]
        if s.cleanup and fits(last_check):
            t0 = time.monotonic()

            async def still_ok(proofs: list[str]) -> list[bool]:
                return [r.ok for r in await self._checker.check_many(req, proofs)]

            cleaned = await minimize(best, still_ok, keep_going=lambda: fits(last_check))
            stats["cleanup"] = {
                "before": len(best),
                "after": len(cleaned),
                "seconds": round(time.monotonic() - t0, 2),
            }
            best = cleaned
        stats["seconds"] = round(time.monotonic() - start, 2)
        proofs = [best] + [p for p in verified if p != best]
        return ProveResponse(
            proofs=[Candidate(tactic=p, verified=True) for p in proofs], message=None, stats=stats
        )
