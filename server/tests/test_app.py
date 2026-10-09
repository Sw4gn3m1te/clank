import httpx
from fastapi.testclient import TestClient

from clank_server.app import create_app
from clank_server.config import Settings

from conftest import fake_llm, needs_lean

REQ = {
    "protocol": 2,
    "goal": "a b : Nat\n⊢ a + b = b + a",
    "hypotheses": [{"name": "a", "type": "Nat"}, {"name": "b", "type": "Nat"}],
    "target": "a + b = b + a",
    "universes": [],
    "imports": ["Init"],
    "opens": [],
    "lean_version": "4.23.0",
    "samples": 4,
}


def client(replies, **settings):
    return TestClient(create_app(Settings(**settings), llm_transport=fake_llm(replies)))


def test_health():
    with client(["rfl"]) as c:
        assert c.get("/health").json()["status"] == "ok"


def test_protocol_mismatch():
    with client(["rfl"]) as c:
        assert c.post("/v1/prove", json={**REQ, "protocol": 99}).status_code == 400


@needs_lean
def test_prove_returns_verified_proofs():
    replies = ["```lean\nrfl\n```", "```lean\nomega\n```", "```lean\nsorry\n```",
               "```lean\nexact Nat.add_comm a b\n```"]
    with client(replies) as c:
        body = c.post("/v1/prove", json=REQ).json()
    assert body["proofs"] == [{"tactic": "omega", "verified": True},
                              {"tactic": "exact Nat.add_comm a b", "verified": True}]
    assert body["message"] is None
    [round0] = body["stats"]["rounds"]
    assert round0 | {"seconds": 0} == {
        "kind": "fresh", "samples": 4, "candidates": 3, "verified": 2, "seconds": 0
    }


@needs_lean
def test_cleanup_removes_redundant_steps():
    with client(["```lean\nskip\nhave h := a\nomega\n```"]) as c:
        body = c.post("/v1/prove", json=REQ).json()
    assert body["proofs"][0] == {"tactic": "omega", "verified": True}
    assert body["proofs"][1] == {"tactic": "skip\nhave h := a\nomega", "verified": True}
    assert body["stats"]["cleanup"]["after"] == len("omega")


@needs_lean
def test_repair_round_uses_lean_errors():
    def reply(prompt):
        if "previous proof attempt" in prompt:
            assert "Lean errors:" in prompt and "rfl" in prompt
            return "```lean\nomega\n```"
        return "```lean\nrfl\n```"

    with client(reply, schedule=("fresh", "repair")) as c:
        body = c.post("/v1/prove", json=REQ).json()
    assert body["proofs"] == [{"tactic": "omega", "verified": True}]
    assert [r["kind"] for r in body["stats"]["rounds"]] == ["fresh", "repair"]


@needs_lean
def test_prove_reports_failure():
    with client(["```lean\nrfl\n```"]) as c:
        body = c.post("/v1/prove", json=REQ).json()
    assert body["proofs"] == []
    assert "no proof found in 4 rounds (1 distinct candidates)" in body["message"]
    assert "rfl" in body["message"]


@needs_lean
def test_timeout_limits_rounds():
    # 10 s are reserved for the tactic, so a 10.5 s timeout leaves room for one round only.
    with client(["```lean\nrfl\n```"]) as c:
        body = c.post("/v1/prove", json={**REQ, "timeout": 10.5}).json()
    assert len(body["stats"]["rounds"]) == 1


@needs_lean
def test_unrestatable_goal_returns_unverified():
    req = {**REQ, "target": "a + b = b + a✝"}
    with client(["```lean\nomega\n```"]) as c:
        body = c.post("/v1/prove", json=req).json()
    assert body["proofs"] == [{"tactic": "omega", "verified": False}]
    assert "could not restate" in body["message"]


def test_llm_failure():
    transport = httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
    with TestClient(create_app(Settings(), llm_transport=transport)) as c:
        body = c.post("/v1/prove", json=REQ).json()
    assert body["proofs"] == [] and "inference failed" in body["message"]
