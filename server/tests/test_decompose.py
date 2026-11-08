from fastapi.testclient import TestClient

from clank_server.app import create_app
from clank_server.config import Settings
from clank_server.decompose import assemble, count_sorries, normalize, parse_goal, subgoal_request, traced
from clank_server.protocol import Hypothesis, ProveRequest

from conftest import fake_llm, needs_lean


def test_normalize_and_trace():
    sketch = "have h : p := sorry\nhave g : q := by sorry\nexact sorry"
    assert normalize(sketch) == "have h : p := by sorry\nhave g : q := by sorry\nsorry"
    assert count_sorries(normalize(sketch)) == 3
    assert traced("have h : p := by sorry") == "have h : p := by (trace_state; sorry)"


def test_parse_goal():
    text = (
        "case h\nx y : ℝ\nh₀ : 0 < x ∧\n    0 < y\nn✝ : ℕ\nk : ℕ := 3\n⊢ x * y >\n    0\n\n"
        "case g\n⊢ True"
    )
    hyps, target = parse_goal(text)
    assert [(h.name, h.type, h.value) for h in hyps] == [
        ("x", "ℝ", None), ("y", "ℝ", None), ("h₀", "0 < x ∧\n    0 < y", None),
        (None, "ℕ", None), ("k", "ℕ", "3"),
    ]
    assert target == "x * y >\n    0"
    assert parse_goal("no goal here") is None
    assert parse_goal("x : ℕ") is None


def test_subgoal_request_keeps_imports():
    parent = ProveRequest(protocol=3, goal="g", target="t", imports=["Mathlib"], opens=["Real"],
                          hypotheses=[Hypothesis(name="z", type="ℕ")])
    sub = subgoal_request(parent, "a : ℕ\n⊢ a = a")
    assert sub.imports == ["Mathlib"] and sub.opens == ["Real"]
    assert sub.goal == "a : ℕ\n⊢ a = a" and sub.target == "a = a" and sub.hypotheses[0].name == "a"


def test_assemble():
    sketch = "have h : p := by sorry\n  have g : q := by\n    sorry\nexact h"
    assert assemble(sketch, ["simp", "intro x\nexact x"]) == (
        "have h : p := by simp\n  have g : q := by\n    intro x\n    exact x\nexact h"
    )
    # A multi-line proof continues at the column of the `sorry` it replaces.
    assert assemble("have h : p := by sorry\nexact h", ["intro x\nexact x"]) == (
        "have h : p := by intro x\n                 exact x\nexact h"
    )
    # Text after the `sorry` leaves no room for more lines.
    assert assemble("have h : p := by sorry; simp", ["a\nb"]) is None
    assert assemble("sorry", []) is None


REQ = {
    "protocol": 3,
    "goal": "a b : Nat\n⊢ a + b + 0 = b + a",
    "hypotheses": [{"name": "a", "type": "Nat"}, {"name": "b", "type": "Nat"}],
    "target": "a + b + 0 = b + a",
    "imports": ["Init"],
    "samples": 2,
}


@needs_lean
def test_decompose_round_solves_subgoals_and_assembles():
    prompts = []

    def reply(prompt):
        prompts.append(prompt)
        if "proof sketch" in prompt:
            return "```lean\nhave h : a + b = b + a := by sorry\nrw [Nat.add_zero]\nexact h\n```"
        if "⊢ a + b = b + a" in prompt:  # the subgoal
            return "```lean\nhave := Nat.add_comm a b\nexact this\n```"
        return "```lean\nrfl\n```"  # wrong for the main goal

    settings = Settings(cache_path=None, cleanup=False, schedule=("fresh", "decompose"))
    with TestClient(create_app(settings, llm_transport=fake_llm(reply))) as c:
        body = c.post("/v1/prove", json=REQ).json()
    assert body["proofs"][0] == {
        "tactic": "have h : a + b = b + a := by have := Nat.add_comm a b\n"
                  "                             exact this\nrw [Nat.add_zero]\nexact h",
        "verified": True,
    }
    fresh, dec = body["stats"]["rounds"]
    assert fresh["verified"] == 0
    assert dec["kind"] == "decompose" and dec["sketches"] == 1 and dec["subgoals"] == dec["solved"] == 1


@needs_lean
def test_decompose_rejects_broken_sketch():
    def reply(prompt):
        if "proof sketch" in prompt:  # `nonsense` is an error that is not about `sorry`
            return "```lean\nhave h : a + b = b + a := by sorry\nnonsense\n```"
        return "```lean\nrfl\n```"

    settings = Settings(cache_path=None, schedule=("fresh", "decompose"))
    with TestClient(create_app(settings, llm_transport=fake_llm(reply))) as c:
        body = c.post("/v1/prove", json=REQ).json()
    assert body["proofs"] == []
    assert body["stats"]["rounds"][1] | {"seconds": 0} == {
        "kind": "decompose", "samples": 2, "sketches": 0, "subgoals": 0, "solved": 0,
        "verified": 0, "seconds": 0,
    }
