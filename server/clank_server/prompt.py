"""Prompt construction and extraction of tactic proofs from model output."""

from __future__ import annotations

import re
import textwrap

from .protocol import ProveRequest

SYSTEM_PROMPT = """\
You are an expert in the Lean 4 theorem prover. Given a proof goal, write a tactic proof that \
closes it.
Reply with the tactic proof only, inside a single ```lean code block. Do not restate the theorem \
and do not start with `by`. Never use `sorry`, `admit` or `native_decide`."""

FORBIDDEN = re.compile(r"\b(sorry|admit|native_decide)\b")
_THINK = re.compile(r"<think>.*?(</think>|$)", re.DOTALL)
_FENCE = re.compile(r"```[ \t]*(?:lean4?|)[ \t]*\n(.*?)(?:```|$)", re.DOTALL)
_DECL = re.compile(r"^\s*(?:theorem|lemma|example)\b.*?:=\s*by\b", re.DOTALL | re.MULTILINE)

PROMPT_STYLES = ("prover", "chat")


def binder(name: str | None, type_: str) -> str:
    return f"({name or '_'} : {type_})"


def statement(req: ProveRequest, name: str = "clank_goal") -> str:
    """The goal as a Lean declaration header, without the proof."""
    binders = " ".join(binder(h.name, h.type) for h in req.hypotheses)
    return f"theorem {name}{' ' + binders if binders else ''} :\n    {req.target} := by"


def messages(req: ProveRequest, style: str = "prover") -> list[dict[str, str]]:
    if style == "prover":
        return prover_messages(req)
    user = (
        f"Prove the following Lean 4 goal.\n\n```lean\n{req.goal}\n```\n\n"
        f"Equivalently, complete this theorem:\n\n```lean\n{statement(req)}\n```"
    )
    if req.imports:
        user += f"\n\nThe file imports: {', '.join(req.imports)}."
    if req.lean_version:
        user += f"\nLean version: {req.lean_version}."
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def prover_messages(req: ProveRequest) -> list[dict[str, str]]:
    """The "complete this file" format that Lean prover models (DeepSeek-Prover, Goedel-Prover,
    Kimina-Prover) are trained on: no system prompt, a Lean file ending in `sorry`."""
    imports = "".join(f"import {m}\n" for m in req.imports if m != "Init")
    goal = textwrap.indent(req.goal, "  ")
    code = f"{imports}\n/- Goal:\n{goal}\n-/\n{statement(req)}\n  sorry".lstrip()
    content = f"Complete the following Lean 4 code:\n\n```lean4\n{code}\n```"
    return [{"role": "user", "content": content}]


def extract_proof(text: str) -> str | None:
    """Extract a tactic proof from a model reply. Returns None if nothing usable is found."""
    text = _THINK.sub("", text)
    blocks = _FENCE.findall(text)
    proof = blocks[0] if blocks else text
    # Models often restate the theorem (prover models restate the whole file, imports included);
    # keep only what follows its `:= by`.
    if m := _DECL.search(proof):
        proof = proof[m.end():]
    proof = textwrap.dedent(proof.strip("\n")).strip()
    if proof.startswith("by") and (len(proof) == 2 or proof[2].isspace()):
        proof = textwrap.dedent(proof[2:].strip("\n")).strip()
    proof = textwrap.dedent("\n".join(line.rstrip() for line in proof.splitlines()))
    if not proof or FORBIDDEN.search(proof):
        return None
    return proof
