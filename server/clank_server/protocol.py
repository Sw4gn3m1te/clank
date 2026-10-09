"""JSON protocol between the `clank` tactic and the server. Keep in sync with docs/protocol.md."""

from __future__ import annotations

from pydantic import BaseModel, Field

PROTOCOL_VERSION = 1


class Hypothesis(BaseModel):
    # None for inaccessible (hygienic) hypotheses such as `a✝`.
    name: str | None
    type: str
    value: str | None = None


class ProveRequest(BaseModel):
    protocol: int
    goal: str
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    target: str
    universes: list[str] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)
    lean_version: str | None = None
    samples: int = Field(default=8, ge=1)


class Candidate(BaseModel):
    tactic: str
    # True if the server checked the candidate with Lean. False means the server could not
    # reproduce the goal and the tactic must check the candidate itself.
    verified: bool


class ProveResponse(BaseModel):
    proofs: list[Candidate]
    message: str | None = None
