"""MVP proving strategy: sample N whole proofs, check them in parallel, return the ones that work."""

from __future__ import annotations

import asyncio
import logging

from . import prompt
from .config import Settings
from .lean import LeanChecker
from .llm import LLMClient, LLMError
from .protocol import Candidate, ProveRequest, ProveResponse

log = logging.getLogger(__name__)


def _first_line(errors: list[str]) -> str:
    return errors[0].strip().splitlines()[0] if errors and errors[0].strip() else "unknown error"


class Prover:
    def __init__(self, settings: Settings, llm: LLMClient, checker: LeanChecker):
        self._settings = settings
        self._llm = llm
        self._checker = checker

    async def prove(self, req: ProveRequest) -> ProveResponse:
        n = min(req.samples, self._settings.max_samples)
        # Check that the goal can be restated server-side while the model is sampling.
        statement = asyncio.create_task(self._checker.check_statement(req))
        try:
            replies = await self._llm.sample(prompt.messages(req, self._settings.prompt_style), n)
        except LLMError as e:
            statement.cancel()
            return ProveResponse(proofs=[], message=f"inference failed: {e}")

        candidates = list(dict.fromkeys(p for r in replies if (p := prompt.extract_proof(r))))
        log.info("%d samples, %d distinct candidates", len(replies), len(candidates))
        if not candidates:
            statement.cancel()
            return ProveResponse(proofs=[], message=f"the model produced no usable proof in {n} samples")

        stmt = await statement
        if not stmt.ok:
            # The tactic re-checks everything anyway, so hand over the unchecked candidates.
            log.warning("cannot restate goal server-side: %s", _first_line(stmt.errors))
            return ProveResponse(
                proofs=[Candidate(tactic=c, verified=False) for c in candidates],
                message=f"server could not restate the goal: {_first_line(stmt.errors)}",
            )

        results = await asyncio.gather(*(self._checker.check(req, c) for c in candidates))
        verified = sorted((c for c, r in zip(candidates, results) if r.ok), key=len)
        log.info("%d/%d candidates verified", len(verified), len(candidates))
        message = None
        if not verified:
            message = (
                f"none of {len(candidates)} distinct candidates passed; "
                f"first error: {_first_line(results[0].errors)}"
            )
        return ProveResponse(
            proofs=[Candidate(tactic=c, verified=True) for c in verified], message=message
        )
