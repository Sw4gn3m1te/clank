import asyncio

from clank_server.config import Settings
from clank_server.lean import LeanChecker, render
from clank_server.protocol import Hypothesis, ProveRequest

from conftest import needs_lean

REQ = ProveRequest(
    protocol=1,
    goal="a b : Nat\n⊢ a + b = b + a",
    hypotheses=[Hypothesis(name="a", type="Nat"), Hypothesis(name="b", type="Nat")],
    target="a + b = b + a",
    imports=["Init"],
)


def check(proof: str, req: ProveRequest = REQ):
    return asyncio.run(LeanChecker(Settings()).check(req, proof))


def test_render():
    src = render(REQ, "intro\nomega")
    assert "import" not in src
    assert "theorem clank_check (a : Nat) (b : Nat) :\n    a + b = b + a := by\n  intro\n  omega\n" in src
    assert src.endswith("#print axioms clank_check\n")


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
def test_statement_check():
    checker = LeanChecker(Settings())
    assert asyncio.run(checker.check_statement(REQ)).ok
    bad = REQ.model_copy(update={"target": "a + c = b"})
    assert not asyncio.run(checker.check_statement(bad)).ok
