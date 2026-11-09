"""Prompt construction and extraction of tactic proofs from model output."""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass

from .protocol import ProveRequest

SYSTEM_PROMPT = """\
You are an expert in the Lean 4 theorem prover. Given a proof goal, write a tactic proof that \
closes it.
Reply with the tactic proof only, inside a single ```lean code block. Do not restate the theorem \
and do not start with `by`. Never use `sorry`, `admit` or `native_decide`."""

# `clank` in a candidate would make the tactic call the server again while re-checking it.
# `clank_check_<i>` are the other candidates of the batch, which all state the very same goal.
FORBIDDEN = re.compile(r"\b(sorry|admit|native_decide|clank|clank_dump|clank_check_\d+)\b")
# Commands that search for a proof and report it, rather than being one.
_SEARCH_TACTIC = re.compile(r"(?<![\w.'])(?:exact|apply|rw|simp|simpa|simp_all|dsimp|aesop|decide)\?")
_THINK = re.compile(r"<think>.*?(</think>|$)", re.DOTALL)
_FENCE = re.compile(r"```[ \t]*(?:lean4?|)[ \t]*\n(.*?)(?:```|$)", re.DOTALL)
_DECL = re.compile(r"^\s*(?:theorem|lemma|example)\b.*?:=\s*by\b", re.DOTALL | re.MULTILINE)
# A line that starts a declaration (or its docstring or attributes).
_DECL_START = re.compile(
    r"^(@\[|/--|((private|protected|noncomputable|nonrec|unsafe|partial) )*"
    r"(theorem|lemma|def|example|instance|abbrev|structure|inductive|class|opaque|axiom)\b)"
)

PROMPT_STYLES = ("prover", "chat")

# Above this the list costs too much context. Environments that large include Mathlib, which is
# what prover models are trained on, so they need the hint least.
MAX_LISTED_TACTICS = 400

# Prover models reach for these out of habit; Lean's `unknown tactic` error does not name them.
_HABITUAL_TACTICS = re.compile(
    r"(?<![\w.'])(aesop|linarith|nlinarith|norm_num|positivity|ring|ring_nf|field_simp|polyrith"
    r"|tauto|push_neg|use|nth_rw|nth_rewrite|contrapose|contrapose!|by_contra|interval_cases"
    r"|fin_cases|gcongr|bound|exacts|refine'|cases'|induction'|rcases|rintro|obtain"
    r"|simp_arith|norm_cast|push_cast|choose|lift|peel|absurd)(?![\w'])"
)

SKETCH_INSTRUCTION = (
    "Write a proof sketch: state the key intermediate steps as `have` statements whose proofs "
    "are `by sorry`, then finish the proof using them. Do not prove the `have` steps."
)


@dataclass
class PromptOptions:
    style: str = "prover"
    # Characters of file context to include (the request may carry more).
    context_chars: int = 1500
    max_premises: int = 8
    # Characters of project definitions to include.
    max_definition_chars: int = 4000


@dataclass
class FileContext:
    previous: str  # complete declarations before the one containing the goal
    current: str  # the declaration containing the goal, up to the `clank` call


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


def split_context(text: str, limit: int) -> FileContext:
    """Split the file text before the goal into earlier declarations and the current one, keeping
    at most about `limit` characters, preferring the text closest to the goal."""
    if limit <= 0:
        return FileContext("", "")
    lines = [l for l in text.splitlines() if not l.startswith("import ")]
    starts = [i for i, l in enumerate(lines) if _DECL_START.match(l)]
    cut = starts[-1] if starts else 0
    previous = "\n".join(lines[:cut]).strip("\n")
    current = "\n".join(lines[cut:]).rstrip()
    if len(current) > limit:
        current = "…\n" + current[-limit:].split("\n", 1)[-1]
    budget = limit - len(current)
    if len(previous) > budget:
        # Drop whole declarations from the front until the rest fits.
        kept = previous.splitlines()
        while kept and len("\n".join(kept)) > budget:
            nxt = next((i for i, l in enumerate(kept) if i > 0 and _DECL_START.match(l)), len(kept))
            kept = kept[nxt:]
        previous = "\n".join(kept).strip("\n")
    return FileContext(previous, current)


def _uncomment(text: str) -> str:
    """Make `text` safe to embed in a Lean block comment."""
    return text.replace("-/", "- /").replace("/-", "/ -")


def _premises(req: ProveRequest, n: int) -> list[str]:
    return [p.statement for p in req.premises[:n]]


def _definitions(req: ProveRequest, options: PromptOptions, ctx: FileContext) -> str:
    """The definitions the goal depends on as labelled Lean source, nearest first and within the
    budget. Source inside a namespace uses short names, hence the label with the full name."""
    shown = f"{ctx.previous}\n{ctx.current}"
    parts, used = [], 0
    for d in req.definitions:
        if d.source in shown or used + len(d.source) > options.max_definition_chars:
            continue
        used += len(d.source)
        parts.append(f"-- {d.name}\n{d.source}")
    return "\n\n".join(parts)


def tactics_note(req: ProveRequest) -> str | None:
    """A statement of which tactics exist in the file, or None if the list is unknown or too long."""
    if not req.tactics or len(req.tactics) > MAX_LISTED_TACTICS:
        return None
    return (
        "Only these tactics are available in this file, any other tactic is an error:\n"
        + ", ".join(req.tactics)
    )


def unavailable_tactics(proof: str, req: ProveRequest) -> list[str]:
    """Commonly used tactics that `proof` calls but the file's environment does not provide."""
    if not req.tactics:
        return []
    known = set(req.tactics)
    return sorted({m.group(1) for m in _HABITUAL_TACTICS.finditer(proof)} - known)


def messages(
    req: ProveRequest,
    options: PromptOptions | None = None,
    attempt: tuple[str, str] | None = None,
    sketch: bool = False,
) -> list[dict[str, str]]:
    """Prompt for a proof of `req`. `attempt` is a failed proof and Lean's errors on it, which the
    model is asked to repair. With `sketch`, the model is asked for a proof sketch instead."""
    options = options or PromptOptions()
    if options.style == "prover":
        return prover_messages(req, options, attempt, sketch)
    ctx = split_context(req.context, options.context_chars)
    code = "\n\n".join(p for p in (preamble(req), ctx.previous, statement(req)) if p)
    user = f"Prove the following Lean 4 goal.\n\n```lean\n{req.goal}\n```\n\n"
    if definitions := _definitions(req, options, ctx):
        user += f"Definitions used in the goal:\n\n```lean\n{definitions}\n```\n\n"
    if ctx.current:
        user += f"It arises in this proof:\n\n```lean\n{ctx.current}\n```\n\n"
    if premises := _premises(req, options.max_premises):
        user += "Possibly useful lemmas:\n" + "\n".join(f"- `{p}`" for p in premises) + "\n\n"
    if note := tactics_note(req):
        user += f"{note}\n\n"
    user += f"Complete this theorem:\n\n```lean\n{code}\n```"
    if sketch:
        user += f"\n\n{SKETCH_INSTRUCTION}"
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
    req: ProveRequest,
    options: PromptOptions | None = None,
    attempt: tuple[str, str] | None = None,
    sketch: bool = False,
) -> list[dict[str, str]]:
    """The "complete this file" format that Lean prover models (DeepSeek-Prover, Goedel-Prover,
    Kimina-Prover) are trained on: no system prompt, a Lean file ending in `sorry`. Earlier
    declarations of the user's file appear as code; everything else goes into comments."""
    options = options or PromptOptions()
    ctx = split_context(req.context, options.context_chars)
    parts = [preamble(req)]
    if definitions := _definitions(req, options, ctx):
        parts.append(f"/- Definitions used in the goal:\n\n{_uncomment(definitions)}\n-/")
    parts.append(ctx.previous)
    if premises := _premises(req, options.max_premises):
        parts.append(
            "/- Possibly useful lemmas:\n"
            + "\n".join(textwrap.indent(_uncomment(p), "  ") for p in premises)
            + "\n-/"
        )
    if note := tactics_note(req):
        parts.append(f"/- {_uncomment(note)} -/")
    goal = f"/- Goal:\n{textwrap.indent(req.goal, '  ')}"
    if ctx.current:
        goal += f"\nIt arises in this proof:\n{textwrap.indent(_uncomment(ctx.current), '  ')}"
    parts.append(goal + "\n-/")
    if attempt:
        proof, errors = attempt
        parts.append(
            "/- A previous proof attempt failed.\nAttempt:\n"
            f"{textwrap.indent(_uncomment(proof), '  ')}\n"
            f"Lean errors:\n{textwrap.indent(_uncomment(errors), '  ')}\n"
            "Write a corrected proof. -/"
        )
    if sketch:
        parts.append(f"/- {SKETCH_INSTRUCTION} -/")
    code ="\n\n".join(p for p in parts if p) + f"\n{statement(req)}\n  sorry"
    content = f"Complete the following Lean 4 code:\n\n```lean4\n{code}\n```"
    return [{"role": "user", "content": content}]


def extract_proof(text: str, allow_sorry: bool = False) -> str | None:
    """Extract a tactic proof from a model reply. Returns None if nothing usable is found.
    `allow_sorry` admits `sorry` (for proof sketches); the other forbidden words never pass."""
    text = _THINK.sub("", text)
    blocks = _FENCE.findall(text)
    proof = blocks[0] if blocks else text
    # Models often restate the theorem (prover models restate the whole file, imports and earlier
    # declarations included); keep only what follows the goal theorem's `:= by`.
    decls = list(_DECL.finditer(proof))
    if decls:
        m = next((d for d in decls if "clank_goal" in d.group(0)), decls[-1])
        proof = proof[m.end():]
    proof = textwrap.dedent(proof.strip("\n")).strip()
    if proof.startswith("by") and (len(proof) == 2 or proof[2].isspace()):
        proof = textwrap.dedent(proof[2:].strip("\n")).strip()
    proof = textwrap.dedent("\n".join(line.rstrip() for line in proof.splitlines()))
    return None if not proof or is_forbidden(proof, allow_sorry) else proof


def is_forbidden(proof: str, allow_sorry: bool = False) -> bool:
    """Whether `proof` uses something a finished proof must not. `allow_sorry` admits `sorry`."""
    bad = {m.group(1) for m in FORBIDDEN.finditer(proof)}
    if allow_sorry:
        bad.discard("sorry")
    return bool(bad) or _SEARCH_TACTIC.search(proof) is not None
