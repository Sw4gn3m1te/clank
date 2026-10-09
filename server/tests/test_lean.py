import asyncio

from clank_server.config import Settings
from clank_server.lean import LeanChecker, render
from clank_server.protocol import Hypothesis, ProveRequest

from conftest import needs_lean

REQ = ProveRequest(
    protocol=2,
    goal="a b : Nat\n⊢ a + b = b + a",
    hypotheses=[Hypothesis(name="a", type="Nat"), Hypothesis(name="b", type="Nat")],
    target="a + b = b + a",
    imports=["Init"],
)


def check(proof: str, req: ProveRequest = REQ):
    return asyncio.run(LeanChecker(Settings()).check(req, proof))


def check_many(proofs: list[str], req: ProveRequest = REQ):
    return asyncio.run(LeanChecker(Settings()).check_many(req, proofs))


def test_render():
    req = REQ.model_copy(update={"opens": ["Nat", "List hiding map"]})
    src, spans = render(req, ["intro\nomega", "rfl"])
    assert "import" not in src
    assert "open Nat\nopen List hiding map\n" in src
    lines = src.splitlines()
    assert lines[spans[0].first - 1] == "theorem clank_check_0 (a : Nat) (b : Nat) :"
    assert lines[spans[0].proof - 1 : spans[0].last] == ["  intro", "  omega", "#print axioms clank_check_0"]
    assert lines[spans[1].proof - 1] == "  rfl"


@needs_lean
def test_accepts_valid_proof():
    assert check("omega").ok
    assert check("exact Nat.add_comm a b").ok


@needs_lean
def test_rejects_wrong_proof():
    r = check("rfl")
    assert not r.ok and r.errors


@needs_lean
def test_rejects_sorry_and_axioms():
    assert not check("sorry").ok
    req = REQ.model_copy(update={"hypotheses": [], "target": "2 + 2 = 4"})
    assert check("decide", req).ok
    assert not check("native_decide", req).ok


@needs_lean
def test_batch_attributes_errors():
    results = check_many(["omega", "skip\nexact foo", "rfl", "skip", "/- open comment"])
    assert [r.ok for r in results] == [True, False, False, False, False]
    # Errors are reported at their line within the proof...
    assert results[1].errors[0].line == 2 and "foo" in results[1].errors[0].message
    # ... except unsolved goals, which Lean reports at `by`, outside the proof.
    assert results[3].errors[0].line is None
    assert results[3].errors[0].message.startswith("unsolved goals")
    assert results[4].errors[0].message == "unbalanced comment"


@needs_lean
def test_statement_check():
    checker = LeanChecker(Settings())
    assert asyncio.run(checker.check_statement(REQ)).ok
    bad = REQ.model_copy(update={"target": "a + c = b"})
    assert not asyncio.run(checker.check_statement(bad)).ok
