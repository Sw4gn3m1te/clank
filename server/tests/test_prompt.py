from clank_server.prompt import PromptOptions, extract_proof, messages, split_context, statement
from clank_server.protocol import Hypothesis, Premise, ProveRequest

REQ = ProveRequest(
    protocol=3,
    goal="a b : Nat\nh✝ : a = b\n⊢ b = a",
    hypotheses=[Hypothesis(name="a", type="Nat"), Hypothesis(name="b", type="Nat"),
                Hypothesis(name=None, type="a = b")],
    target="b = a",
    imports=["Init"],
)

CONTEXT = """import Mathlib

open Nat

def double (n : Nat) : Nat := n + n

/-- A lemma. -/
theorem double_eq (n : Nat) : double n = 2 * n := by
  unfold double
  omega

theorem uses_it (a b : Nat) (h : a = b) : b = a := by
  have := double_eq a
  """


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


def test_extract_rejects_forbidden_words():
    assert extract_proof("rfl") == "rfl"
    assert extract_proof("```lean\nsorry\n```") is None
    assert extract_proof("```lean\nnative_decide\n```") is None
    assert extract_proof("```lean\nclank\n```") is None  # would recurse in the tactic
    assert extract_proof("") is None
    assert extract_proof("```lean\nhave h : 1 = 1 := by sorry\nexact h\n```", allow_sorry=True)
    assert extract_proof("```lean\nhave h : 1 = 1 := by admit\n```", allow_sorry=True) is None


def test_extract_full_file_reply():
    reply = (
        "```lean4\nimport Mathlib\n\n/- Goal: ... -/\ntheorem clank_goal (n : Nat) :\n"
        "    0 + n = n := by\n  induction n with\n  | zero => rfl\n  | succ n ih => omega\n```"
    )
    assert extract_proof(reply) == "induction n with\n| zero => rfl\n| succ n ih => omega"


def test_extract_picks_goal_theorem_when_context_is_restated():
    reply = (
        "```lean4\ntheorem double_eq (n : Nat) : double n = 2 * n := by\n  unfold double\n  omega\n\n"
        "theorem clank_goal (a b : Nat) :\n    b = a := by\n  exact h.symm\n```"
    )
    assert extract_proof(reply) == "exact h.symm"


def test_split_context():
    ctx = split_context(CONTEXT, 10_000)
    assert ctx.current.startswith("theorem uses_it") and ctx.current.endswith("have := double_eq a")
    assert ctx.previous.startswith("open Nat") and "import" not in ctx.previous
    assert "theorem double_eq" in ctx.previous
    # Under a tight limit whole earlier declarations are dropped, the current one is kept.
    small = split_context(CONTEXT, len(ctx.current) + 60)
    assert small.current == ctx.current
    assert small.previous.startswith("/-- A lemma. -/") or small.previous == ""
    assert "def double" not in small.previous


def test_statement_and_messages():
    assert statement(REQ) == "theorem clank_goal (a : Nat) (b : Nat) (_ : a = b) :\n    b = a := by"
    chat = messages(REQ, PromptOptions(style="chat"))
    assert chat[0]["role"] == "system"
    assert REQ.goal in chat[1]["content"] and statement(REQ) in chat[1]["content"]
    [prover] = messages(REQ)
    assert prover["content"].startswith("Complete the following Lean 4 code:")
    assert prover["content"].endswith(statement(REQ) + "\n  sorry\n```")
    assert "import" not in prover["content"]


def test_messages_include_context_and_premises():
    req = REQ.model_copy(update={
        "context": CONTEXT,
        "premises": [Premise(name="Eq.symm", statement="Eq.symm {a b : α} (h : a = b) : b = a"),
                     Premise(name="Nat.add_comm", statement="Nat.add_comm (n m : Nat) : n + m = m + n")],
    })
    [prover] = messages(req, PromptOptions(max_premises=1))
    text = prover["content"]
    # Earlier declarations as code, before the goal; the current proof in the goal comment.
    assert text.index("def double (n : Nat)") < text.index("/- Goal:") < text.index("theorem clank_goal")
    assert "It arises in this proof:\n  theorem uses_it" in text
    assert "Eq.symm {a b : α}" in text and "Nat.add_comm" not in text  # max_premises=1
    chat = messages(req, PromptOptions(style="chat", context_chars=0, max_premises=0))[1]["content"]
    assert "double" not in chat and "Eq.symm" not in chat
