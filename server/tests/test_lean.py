import asyncio
import os
import time
from pathlib import Path

import pytest

from clank_server.config import Settings
from clank_server.cache import cache_key
from clank_server.lean import (
    LeanChecker,
    ReplChecker,
    _results,
    _Run,
    _Span,
    header,
    render,
    render_body,
)
from clank_server.protocol import Hypothesis, ProveRequest

from conftest import needs_lean

REQ = ProveRequest(
    protocol=3,
    goal="a b : Nat\n⊢ a + b = b + a",
    hypotheses=[Hypothesis(name="a", type="Nat"), Hypothesis(name="b", type="Nat")],
    target="a + b = b + a",
    imports=["Init"],
)

REPL = Path(
    os.environ.get("CLANK_TEST_REPL")
    or Path.home() / ".cache/clank/repl-v4.29.0/.lake/build/bin" / ("repl.exe" if os.name == "nt" else "repl")
)
CHECKERS = [
    "batch",
    pytest.param("repl", marks=pytest.mark.skipif(not REPL.exists(), reason="no Lean REPL build")),
]


@pytest.fixture(params=CHECKERS)
def checker(request):
    if request.param == "batch":
        yield LeanChecker(Settings())
    else:
        c = ReplChecker(Settings(max_parallel_checks=1), REPL)
        yield c
        asyncio.run(c.close())


def run(coro):
    return asyncio.run(coro)


def test_render():
    req = REQ.model_copy(update={"opens": ["Nat", "List hiding map"], "imports": ["Init", "Foo"]})
    src, spans = render(req, ["intro\nomega", "rfl"])
    assert src.startswith("import Foo\nset_option autoImplicit false\nopen Nat\nopen List hiding map\n")
    lines = src.splitlines()
    assert lines[spans[0].first - 1] == "theorem clank_check_0 (a : Nat) (b : Nat) :"
    assert lines[spans[0].proof - 1 : spans[0].last] == ["  intro", "  omega", "#print axioms clank_check_0"]
    assert lines[spans[1].proof - 1] == "  rfl"


def test_render_with_prefix():
    prefix = "universe u\nnamespace A\ntheorem t : True := sorry\n"
    req = REQ.model_copy(update={"prefix": prefix, "universes": ["u", "v"]})
    assert header(req) == "import Clank"
    assert header(REQ) == ""
    body, [span] = render_body(req, ["omega"])
    lines = body.splitlines()
    assert lines[:3] == prefix.splitlines()
    assert "universe v" in lines and "universe u v" not in lines
    assert lines[span.proof - 1] == "  omega"


def test_stubbed_theorems_do_not_fail_candidates():
    spans = [_Span(first=10, proof=12, last=13)]
    stub_warning = {"severity": "warning", "kind": "hasSorry", "pos": {"line": 3}, "data": "sorry"}
    axioms = {"severity": "information", "pos": {"line": 13},
              "data": "'A.clank_check_0' depends on axioms: [propext, sorryAx]"}
    run = _Run([stub_warning, axioms])
    assert _results(run, spans, 1.0, stubs=True)[0].ok
    without_stubs = _results(run, spans, 1.0)[0]
    assert not without_stubs.ok and without_stubs.only_sorry
    prefix_error = {"severity": "error", "pos": {"line": 3}, "data": "bad"}
    assert not _results(_Run([prefix_error, axioms]), spans, 1.0, stubs=True)[0].ok


def test_cache_key_depends_on_prefix():
    edited = REQ.model_copy(update={"prefix": "theorem t : True := sorry\n"})
    assert cache_key(REQ, "env") != cache_key(edited, "env")


@needs_lean
def test_accepts_valid_proof(checker):
    assert run(checker.check(REQ, "omega")).ok
    assert run(checker.check(REQ, "exact Nat.add_comm a b")).ok


@needs_lean
def test_rejects_wrong_proof_sorry_and_axioms(checker):
    r = run(checker.check(REQ, "rfl"))
    assert not r.ok and r.errors
    assert not run(checker.check(REQ, "sorry")).ok
    req = REQ.model_copy(update={"hypotheses": [], "target": "2 + 2 = 4"})
    assert run(checker.check(req, "decide")).ok
    assert not run(checker.check(req, "native_decide")).ok


@needs_lean
def test_batch_attributes_errors(checker):
    results = run(checker.check_many(REQ, ["omega", "skip\nexact foo", "rfl", "skip", "/- open comment"]))
    assert [r.ok for r in results] == [True, False, False, False, False]
    # Errors are reported at their line within the proof...
    assert results[1].errors[0].line == 2 and "foo" in results[1].errors[0].message
    # ... except unsolved goals, which Lean reports at `by`, outside the proof.
    assert results[3].errors[0].line is None
    assert results[3].errors[0].message.startswith("unsolved goals")
    assert results[4].errors[0].message == "unbalanced comment"


@needs_lean
def test_statement_check(checker):
    assert run(checker.check_statement(REQ)).ok
    bad = REQ.model_copy(update={"target": "a + c = b"})
    assert not run(checker.check_statement(bad)).ok


@needs_lean
def test_bad_import_fails_every_candidate(checker):
    req = REQ.model_copy(update={"imports": ["Init", "No.Such.Module"]})
    results = run(checker.check_many(req, ["omega", "rfl"]))
    assert not any(r.ok for r in results)
    assert not run(checker.check_statement(req)).ok


@pytest.mark.skipif(not REPL.exists(), reason="no Lean REPL build")
def test_repl_reuses_loaded_imports():
    async def go():
        c = ReplChecker(Settings(max_parallel_checks=1), REPL)
        try:
            await c.check(REQ, "omega")  # starts the worker
            t = time.monotonic()
            results = await asyncio.gather(*(c.check(REQ, "omega") for _ in range(5)))
            return results, time.monotonic() - t
        finally:
            await c.close()

    results, seconds = run(go())
    assert all(r.ok for r in results)
    assert seconds < 5  # no process start or import per check


@pytest.mark.skipif(not REPL.exists(), reason="no Lean REPL build")
def test_repl_timeout_restarts_worker():
    async def go():
        c = ReplChecker(Settings(max_parallel_checks=1, lean_timeout=3), REPL)
        try:
            slow = REQ.model_copy(update={"hypotheses": [], "target": "True"})
            timed_out = await c.check(slow, "sleep 10000\ntrivial")
            after = await c.check(REQ, "omega")
            return timed_out, after
        finally:
            await c.close()

    timed_out, after = run(go())
    assert not timed_out.ok and "timed out" in timed_out.errors[0].message
    assert after.ok
