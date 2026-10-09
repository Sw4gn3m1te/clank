from clank_server.prompt import extract_proof, messages, statement
from clank_server.protocol import Hypothesis, ProveRequest


def test_extract_fenced():
    assert extract_proof("Sure:\n```lean\nomega\n```\nDone.") == "omega"


def test_extract_strips_think_and_by():
    reply = "<think>maybe ```lean\nsimp\n```</think>\n```lean4\nby\n  intro h\n  exact h\n```"
    assert extract_proof(reply) == "intro h\nexact h"


def test_extract_restated_theorem():
    reply = "```lean\ntheorem foo (a b : Nat) : a + b = b + a := by\n  omega\n```"
    assert extract_proof(reply) == "omega"


def test_extract_keeps_relative_indentation():
    reply = "```lean\n  induction n with\n  | zero => rfl\n  | succ n ih =>\n    simp [ih]\n```"
    assert extract_proof(reply) == "induction n with\n| zero => rfl\n| succ n ih =>\n  simp [ih]"


def test_extract_unfenced_and_rejects_sorry():
    assert extract_proof("rfl") == "rfl"
    assert extract_proof("```lean\nsorry\n```") is None
    assert extract_proof("```lean\nnative_decide\n```") is None
    assert extract_proof("") is None


def test_statement_and_messages():
    req = ProveRequest(
        protocol=1,
        goal="a b : Nat\nh✝ : a = b\n⊢ b = a",
        hypotheses=[Hypothesis(name="a", type="Nat"), Hypothesis(name="b", type="Nat"),
                    Hypothesis(name=None, type="a = b")],
        target="b = a",
        imports=["Init"],
    )
    assert statement(req) == "theorem clank_goal (a : Nat) (b : Nat) (_ : a = b) :\n    b = a := by"
    chat = messages(req, "chat")
    assert chat[0]["role"] == "system"
    assert req.goal in chat[1]["content"] and statement(req) in chat[1]["content"]
    [prover] = messages(req, "prover")
    assert prover["content"].startswith("Complete the following Lean 4 code:")
    assert prover["content"].endswith(statement(req) + "\n  sorry\n```")
    assert "import" not in prover["content"]


def test_extract_full_file_reply():
    reply = (
        "```lean4\nimport Mathlib\n\n/- Goal: ... -/\ntheorem clank_goal (n : Nat) :\n"
        "    0 + n = n := by\n  induction n with\n  | zero => rfl\n  | succ n ih => omega\n```"
    )
    assert extract_proof(reply) == "induction n with\n| zero => rfl\n| succ n ih => omega"
