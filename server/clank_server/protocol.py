"""JSON protocol between the `clank` tactic and the server. Keep in sync with docs/protocol.md."""

from __future__ import annotations

from pydantic import BaseModel, Field

PROTOCOL_VERSION = 3


class Hypothesis(BaseModel):
    # None for inaccessible (hygienic) hypotheses such as `a✝`.
    name: str | None
    type: str
    value: str | None = None


class Premise(BaseModel):
    name: str
    # The lemma's signature, e.g. "Nat.add_comm (n m : Nat) : n + m = m + n".
    statement: str


class Definition(BaseModel):
    name: str
    # The definition's source text, without its docstring.
    source: str


class ProveRequest(BaseModel):
    protocol: int
    goal: str
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    target: str
    universes: list[str] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)
    # Arguments of the `open` commands in scope, e.g. "Real" or "Nat hiding add".
    opens: list[str] = Field(default_factory=list)
    lean_version: str | None = None
    samples: int = Field(default=8, ge=1)
    # Seconds until the tactic gives up on the request. The server plans its search to finish
    # well before then.
    timeout: float | None = None
    # Source text of the file before the `clank` call, for the model to read.
    context: str = ""
    # Library lemmas matching the goal, as hints.
    premises: list[Premise] = Field(default_factory=list)
    # Tactics that parse in the file's environment (Lean core plus transitive imports).
    # Empty means unknown, as in requests recorded before this field existed.
    tactics: list[str] = Field(default_factory=list)
    # The file up to the current declaration with earlier proofs replaced by `sorry`, for
    # checking candidates in the file's own scope. Empty means check against the imports only.
    prefix: str = ""
    # Project definitions the goal depends on, nearest first.
    definitions: list[Definition] = Field(default_factory=list)


class Candidate(BaseModel):
    tactic: str
    # True if the server checked the candidate with Lean. False means the server could not
    # reproduce the goal and the tactic must check the candidate itself.
    verified: bool


class ProveResponse(BaseModel):
    proofs: list[Candidate]
    message: str | None = None
    # The failed candidate that got furthest, when the search found no proof.
    best_attempt: str | None = None
    # Diagnostics about the search (rounds, samples, timings). Informational only.
    stats: dict | None = None
