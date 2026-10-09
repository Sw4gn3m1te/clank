import asyncio

from clank_server.cleanup import minimize, variants


def test_variants():
    proof = "intro n\nhave h := n\ninduction n with\n| zero => rfl\n| succ n ih =>\n  simp_all [Nat.add_comm] <;> omega"
    vs = variants(proof)
    # Dropping a step drops the lines that belong to it.
    assert "intro n\nhave h := n" in vs
    assert "intro n\ninduction n with\n| zero => rfl\n| succ n ih =>\n  simp_all [Nat.add_comm] <;> omega" in vs
    # Trailing `<;> tac` and simp lemma lists.
    assert any(v.endswith("simp_all [Nat.add_comm]") for v in vs)
    assert any(v.endswith("simp_all <;> omega") for v in vs)
    # Match alternatives are never dropped on their own.
    assert not any("| zero" not in v and "induction" in v for v in vs)


def test_variants_keep_only():
    assert all("simp only" in v for v in variants("simp only [foo]") if "simp" in v)


def test_minimize_greedy():
    needed = {"intro n", "omega"}

    async def check(proofs):
        return [set(p.splitlines()) >= needed for p in proofs]

    proof = "intro n\nhave h := n\nskip\nomega"
    assert asyncio.run(minimize(proof, check)) == "intro n\nomega"


def test_minimize_keeps_proof_when_nothing_passes():
    async def check(proofs):
        return [False] * len(proofs)

    assert asyncio.run(minimize("a\nb", check)) == "a\nb"
