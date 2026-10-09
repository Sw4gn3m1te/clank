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


def preamble(req: ProveRequest) -> str:
    """Imports and `open`s of the user's file, as Lean source."""
    lines = [f"import {m}" for m in req.imports if m != "Init"]
    if req.opens:
        lines += ["", *(f"open {o}" for o in req.opens)]
    return "\n".join(lines).strip("\n")


def _uncomment(text: str) -> str:
    """Make `text` safe to embed in a Lean block comment."""
    return text.replace("-/", "- /").replace("/-", "/ -")


def messages(
    req: ProveRequest, style: str = "prover", attempt: tuple[str, str] | None = None
) -> list[dict[str, str]]:
    """Prompt for a proof of `req`. `attempt` is a failed proof and Lean's errors on it, which the
    model is asked to repair."""
    if style == "prover":
        return prover_messages(req, attempt)
    code = "\n\n".join(p for p in (preamble(req), statement(req)) if p)
    user = (
        f"Prove the following Lean 4 goal.\n\n```lean\n{req.goal}\n```\n\n"
        f"Equivalently, complete this theorem:\n\n```lean\n{code}\n```"
    )
    if req.lean_version:
        user += f"\n\nLean version: {req.lean_version}."
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
    if attempt:
        proof, errors = attempt
        msgs += [
            {"role": "assistant", "content": f"```lean\n{proof}\n```"},
            {
                "role": "user",
                "content": f"Lean rejected this proof:\n\n{errors}\n\n"
                "Write a corrected proof. Reply with the tactic proof only, in a ```lean block.",
            },
        ]
    return msgs


def prover_messages(
    req: ProveRequest, attempt: tuple[str, str] | None = None
) -> list[dict[str, str]]:
    """The "complete this file" format that Lean prover models (DeepSeek-Prover, Goedel-Prover,
    Kimina-Prover) are trained on: no system prompt, a Lean file ending in `sorry`. A failed attempt
    to repair goes into a comment above the theorem."""
    parts = [preamble(req), f"/- Goal:\n{textwrap.indent(req.goal, '  ')}\n-/"]
    if attempt:
        proof, errors = attempt
        parts.append(
            "/- A previous proof attempt failed.\nAttempt:\n"
            f"{textwrap.indent(_uncomment(proof), '  ')}\n"
            f"Lean errors:\n{textwrap.indent(_uncomment(errors), '  ')}\n"
            "Write a corrected proof. -/"
        )
    code = "\n\n".join(p for p in parts if p) + f"\n{statement(req)}\n  sorry"
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
