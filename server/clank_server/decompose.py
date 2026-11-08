"""Proof sketches: decompose a goal into subgoals and assemble their proofs.

A sketch is a proof whose hard steps are `sorry`, typically `have h : T := by sorry` lines
followed by the steps that finish the proof from them. To find the subgoals, every `sorry` is
replaced by `(trace_state; sorry)`. One Lean check then both validates the sketch (the only
errors must be about `sorry`) and reports the exact goal, with its local context, at each `sorry`.
Each goal becomes a request of its own, and the proofs found are spliced back into the sketch.
"""

from __future__ import annotations

import re
import textwrap

from .protocol import Hypothesis, ProveRequest

_SORRY = re.compile(r"\bsorry\b")
_TERM_SORRY = re.compile(r":=\s*sorry\b")
_EXACT_SORRY = re.compile(r"\bexact\s+sorry\b")


def normalize(sketch: str) -> str:
    """Put every `sorry` in tactic position, so that it can be traced and replaced by a tactic
    proof: `:= sorry` becomes `:= by sorry`, and `exact sorry` becomes `sorry`."""
    sketch = _TERM_SORRY.sub(":= by sorry", sketch)
    return _EXACT_SORRY.sub("sorry", sketch)


def count_sorries(sketch: str) -> int:
    return len(_SORRY.findall(sketch))


def traced(sketch: str) -> str:
    """The sketch with each `sorry` reporting its goal."""
    return _SORRY.sub("(trace_state; sorry)", sketch)


def parse_goal(text: str) -> tuple[list[Hypothesis], str] | None:
    """Parse the first goal of a pretty-printed goal state (`trace_state` output) into its
    hypotheses and target. Returns None if the text is not a single readable goal."""
    goal = text.strip().split("\n\n")[0]
    lines = goal.splitlines()
    if lines and lines[0].startswith("case "):
        lines = lines[1:]
    entries: list[str] = []
    for line in lines:
        if line[:1].isspace() and entries:  # continuation of a long hypothesis or target
            entries[-1] += "\n" + line
        else:
            entries.append(line)
    targets = [i for i, e in enumerate(entries) if e.startswith("⊢")]
    if len(targets) != 1 or targets[0] != len(entries) - 1:
        return None
    target = entries[-1][1:].strip()
    hypotheses = []
    for e in entries[:-1]:
        names, sep, rest = e.partition(" : ")
        if not sep or not names.strip():
            return None
        type_, _, value = rest.partition(" := ")
        for name in names.split():
            hypotheses.append(
                Hypothesis(name=None if "✝" in name else name, type=type_.strip(), value=value.strip() or None)
            )
    return hypotheses, target


def subgoal_request(parent: ProveRequest, goal_text: str) -> ProveRequest | None:
    parsed = parse_goal(goal_text)
    if parsed is None:
        return None
    hypotheses, target = parsed
    return parent.model_copy(
        update={"goal": goal_text.strip().split("\n\n")[0], "hypotheses": hypotheses, "target": target}
    )


def assemble(sketch: str, proofs: list[str]) -> str | None:
    """Replace the i-th `sorry` of `sketch` by `proofs[i]`. A multi-line proof continues at the
    column of the `sorry` it replaces, which keeps it in the same tactic block. Returns None if a
    multi-line proof would have to go where text follows the `sorry` on its line."""
    matches = list(_SORRY.finditer(sketch))
    if len(matches) != len(proofs):
        return None
    out = sketch
    for m, proof in reversed(list(zip(matches, proofs))):
        lines = proof.splitlines() or [""]
        if len(lines) > 1:
            line_end = out.find("\n", m.end())
            rest = out[m.end() : line_end if line_end >= 0 else len(out)]
            if rest.strip():
                return None
        column = m.start() - (out.rfind("\n", 0, m.start()) + 1)
        text = lines[0] + "".join("\n" + textwrap.indent(l, " " * column) if l.strip() else "\n" for l in lines[1:])
        out = out[: m.start()] + text + out[m.end() :]
    return out
