"""Proof search: rounds of sampling and checking, repair of failed attempts, decomposition into
subgoals, then cleanup.

Each round draws `samples` proofs from the model and checks all new ones with Lean in a single
batch. Round kinds:

- "fresh" samples proofs of the plain goal.
- "repair" picks the most promising failures so far and asks the model to fix them, showing
  Lean's errors.
- "decompose" asks for proof sketches (`have` steps proved by `sorry`), finds the goal of each
  `sorry` with Lean, solves those subgoals in parallel with fresh and repair rounds, and checks
  the assembled proof. See decompose.py.

The search stops at the first round that produces a verified proof, which is then shortened by
`cleanup.minimize`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import decompose, prompt
from .cache import ProofCache, cache_key, environment_id
from .cleanup import minimize
from .config import Settings
from .lean import CheckResult, LeanChecker
from .llm import LLMClient, LLMError
from .protocol import Candidate, ProveRequest, ProveResponse

log = logging.getLogger(__name__)

ROUND_KINDS = ("fresh", "repair", "decompose")
# How subgoals of a sketch are searched.
SUBGOAL_SCHEDULE = ("fresh", "repair")


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


@dataclass
class Search:
    """Outcome of the rounds for one goal."""

    verified: list[str] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    rounds: list[dict] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    # Set when the goal could not be restated server-side: the candidates of the first round.
    unverified: list[str] | None = None
    statement_error: str | None = None
    error: str | None = None
    # Duration of the latest Lean batch, to predict the next one.
    last_check: float = 0.0


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
    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        checker: LeanChecker,
        cache: ProofCache | None = None,
    ):
        self._settings = settings
        self._llm = llm
        self._checker = checker
        self._cache = cache
        self._env_id = environment_id(settings.lean_project)

    def _options(self) -> prompt.PromptOptions:
        s = self._settings
        return prompt.PromptOptions(s.prompt_style, s.context_chars, s.max_premises)

    async def _sample_repairs(self, req: ProveRequest, targets: list[Failure], n: int) -> list[str]:
        opts = self._options()
        jobs = [
            self._llm.sample(prompt.messages(req, opts, (f.proof, format_errors(f.result))), k)
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
        key = cache_key(req, self._env_id) if self._cache else None
        if key and (cached := self._cache.get(key)):
            log.info("cache hit")
            return ProveResponse(
                proofs=[Candidate(tactic=p, verified=True) for p in cached],
                stats={"cache": "hit", "seconds": round(time.monotonic() - start, 2)},
            )
        # Leave the tactic time to re-check the proof before its request times out.
        budget = s.time_budget if req.timeout is None else min(s.time_budget, req.timeout - 10)

        def fits(cost: float) -> bool:
            return time.monotonic() - start + cost <= budget

        n = min(req.samples, s.max_samples)
        search = await self._search(req, s.schedule, n, fits, decompose_allowed=True)
        stats: dict = {"rounds": search.rounds}

        if search.unverified is not None:
            stats["seconds"] = round(time.monotonic() - start, 2)
            return ProveResponse(
                proofs=[Candidate(tactic=c, verified=False) for c in search.unverified],
                message=f"server could not restate the goal: {search.statement_error}",
                stats=stats,
            )
        if not search.verified:
            stats["seconds"] = round(time.monotonic() - start, 2)
            error = search.error
            if error is None:
                best = max(search.failures, key=Failure.score, default=None)
                error = (
                    f"no proof found in {len(search.rounds)} rounds "
                    f"({len(search.seen)} distinct candidates)"
                )
                if best:
                    error += f"; most promising attempt failed with: {best.result.first_error()}"
            return ProveResponse(proofs=[], message=error, stats=stats)

        best = search.verified[0]
        if s.cleanup and fits(search.last_check):
            t0 = time.monotonic()

            async def still_ok(proofs: list[str]) -> list[bool]:
                return [r.ok for r in await self._checker.check_many(req, proofs)]

            cleaned = await minimize(best, still_ok, keep_going=lambda: fits(search.last_check))
            stats["cleanup"] = {
                "before": len(best),
                "after": len(cleaned),
                "seconds": round(time.monotonic() - t0, 2),
            }
            best = cleaned
        stats["seconds"] = round(time.monotonic() - start, 2)
        proofs = [best] + [p for p in search.verified if p != best]
        if key:
            self._cache.put(key, proofs)
        return ProveResponse(
            proofs=[Candidate(tactic=p, verified=True) for p in proofs], message=None, stats=stats
        )

    async def _search(
        self,
        req: ProveRequest,
        schedule: tuple[str, ...],
        n: int,
        fits: Callable[[float], bool],
        decompose_allowed: bool,
    ) -> Search:
        """Run the rounds of `schedule` on `req` until one verifies a proof or time runs out."""
        s = self._settings
        out = Search()
        repaired: set[str] = set()
        # Check that the goal can be restated server-side while the model is sampling.
        statement = asyncio.create_task(self._checker.check_statement(req))
        statement_checked = False
        try:
            for i, kind in enumerate(schedule):
                if i > 0 and not fits(out.rounds[-1]["seconds"]):
                    break
                t0 = time.monotonic()
                if kind == "decompose" and not decompose_allowed:
                    kind = "fresh"
                if kind == "decompose":
                    if not statement_checked:
                        statement_checked = True
                        stmt = await statement
                        if not stmt.ok:
                            out.statement_error = stmt.first_error()
                            out.unverified = []
                            return out
                    proofs, info = await self._decompose(req, n, fits)
                    out.verified = proofs
                    out.rounds.append({"kind": kind, **info, "verified": len(proofs),
                                       "seconds": round(time.monotonic() - t0, 2)})
                    log.info("round %d (%s): %s", i, kind, out.rounds[-1])
                    if proofs:
                        break
                    continue

                targets: list[Failure] = []
                if kind == "repair":
                    pool = sorted(
                        (f for f in out.failures if f.proof not in repaired),
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
                        replies = await self._llm.sample(prompt.messages(req, self._options()), n)
                except LLMError as e:
                    out.error = f"inference failed: {e}"
                    break

                candidates = []
                for r in replies:
                    p = prompt.extract_proof(r)
                    if p and p not in out.seen:
                        out.seen.add(p)
                        candidates.append(p)

                if candidates and not statement_checked:
                    statement_checked = True
                    stmt = await statement
                    if not stmt.ok:
                        # The tactic re-checks everything anyway, so hand over unchecked candidates.
                        log.warning("cannot restate goal server-side: %s", stmt.first_error())
                        out.statement_error = stmt.first_error()
                        out.unverified = candidates
                        return out

                t1 = time.monotonic()
                results = await self._checker.check_many(req, candidates) if candidates else []
                if candidates:
                    out.last_check = time.monotonic() - t1
                ok = [c for c, r in zip(candidates, results) if r.ok]
                out.failures += [Failure(c, r) for c, r in zip(candidates, results) if not r.ok]
                out.rounds.append(
                    {
                        "kind": kind,
                        "samples": len(replies),
                        "candidates": len(candidates),
                        "verified": len(ok),
                        "seconds": round(time.monotonic() - t0, 2),
                    }
                )
                log.info("round %d (%s): %s", i, kind, out.rounds[-1])
                if ok:
                    out.verified = sorted(ok, key=len)
                    break
        finally:
            statement.cancel()
        return out

    async def _decompose(
        self, req: ProveRequest, n: int, fits: Callable[[float], bool]
    ) -> tuple[list[str], dict]:
        """One decompose round. Returns verified proofs (possibly none) and round statistics."""
        s = self._settings
        info: dict = {"samples": 0, "sketches": 0, "subgoals": 0, "solved": 0}
        try:
            replies = await self._llm.sample(prompt.messages(req, self._options(), sketch=True), n)
        except LLMError:
            return [], info
        info["samples"] = len(replies)
        sketches = []
        for r in replies:
            p = prompt.extract_proof(r, allow_sorry=True)
            if p and (p := decompose.normalize(p)) not in sketches and decompose.count_sorries(p):
                sketches.append(p)
        if not sketches:
            return [], info

        # Valid sketches: correct up to `sorry`, with a goal reported for every `sorry`.
        results = await self._checker.check_many(req, [decompose.traced(p) for p in sketches])
        valid = [
            (p, r) for p, r in zip(sketches, results)
            if r.only_sorry and len(r.infos) == decompose.count_sorries(p)
        ]
        info["sketches"] = len(valid)
        # Fewer subgoals first: less to prove.
        valid.sort(key=lambda pr: decompose.count_sorries(pr[0]))

        for sketch, result in valid[: s.max_sketches]:
            if not fits(0):
                break
            goals = [text for _, _, text in sorted(result.infos)]
            parsed = [decompose.subgoal_request(req, g) for g in goals]
            if any(r is None for r in parsed):
                continue
            subreqs = [r.model_copy(update={"premises": []}) for r in parsed]
            info["subgoals"] += len(subreqs)
            sub_n = max(2, n // len(subreqs))
            subsearches = await asyncio.gather(
                *(self._search(r, SUBGOAL_SCHEDULE, sub_n, fits, decompose_allowed=False)
                  for r in subreqs)
            )
            solved = [x.verified[0] if x.verified else None for x in subsearches]
            info["solved"] += sum(p is not None for p in solved)
            if any(p is None for p in solved):
                continue
            proof = decompose.assemble(sketch, solved)
            if proof and (await self._checker.check(req, proof)).ok:
                return [proof], info
        return [], info
