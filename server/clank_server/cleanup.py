"""Removal of redundant steps from a verified proof.

Models pad proofs with steps that do nothing (`have h' := h`, trailing `<;> simp_all`, long simp
lemma lists). Cleanup proposes simpler variants of a proof, checks them all in one Lean run, keeps
the shortest variant that still checks, and repeats until no variant passes.
"""

from __future__ import annotations

import re
import textwrap
from collections.abc import Awaitable, Callable

_SIMP_ARGS = re.compile(r"\b(simp_all|simp|norm_num|field_simp)(?! only)(\s*\[[^\]]*\])")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _belongs_to(line: str, step: str) -> bool:
    """Whether `line`, somewhere after `step`, is part of it: more indented, or a match
    alternative (`| zero => ...`) at the same indentation."""
    if not line.strip() or _indent(line) > _indent(step):
        return True
    return _indent(line) == _indent(step) and line.lstrip().startswith("|")


def variants(proof: str) -> list[str]:
    """Single-step simplifications of `proof`:
    - drop one step together with the more-indented lines that belong to it;
    - drop the last `<;> tac` of a line;
    - drop the lemma list of a `simp`-like call."""
    lines = proof.splitlines()
    out: list[list[str]] = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        # Dropping a match alternative or the first line of a block never helps.
        if not line.lstrip().startswith("|"):
            j = i + 1
            while j < len(lines) and _belongs_to(lines[j], line):
                j += 1
            out.append(lines[:i] + lines[j:])
        if " <;> " in line:
            out.append(lines[:i] + [line[: line.rindex(" <;> ")]] + lines[i + 1 :])
        for m in _SIMP_ARGS.finditer(line):
            out.append(lines[:i] + [line[: m.start(2)] + line[m.end(2) :]] + lines[i + 1 :])
    result = []
    for v in out:
        text = textwrap.dedent("\n".join(v)).strip("\n")
        if text.strip() and text != proof and text not in result:
            result.append(text)
    return result


async def minimize(
    proof: str,
    check_many: Callable[[list[str]], Awaitable[list[bool]]],
    max_rounds: int = 3,
    max_variants: int = 48,
    keep_going: Callable[[], bool] = lambda: True,
) -> str:
    """Greedily shorten `proof`. `check_many` reports which of the given proofs still check.
    `keep_going` is asked before every round after the first (e.g. to respect a time budget)."""
    for i in range(max_rounds):
        if i > 0 and not keep_going():
            break
        vs = sorted(variants(proof), key=len)[:max_variants]
        if not vs:
            break
        ok = [v for v, passed in zip(vs, await check_many(vs)) if passed]
        if not ok:
            break
        proof = min(ok, key=len)
    return proof
