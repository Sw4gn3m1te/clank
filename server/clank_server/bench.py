"""Benchmark harness.

    python -m clank_server.bench extract          # bench/Bench/MiniF2F.lean -> bench/requests.jsonl
    python -m clank_server.bench run --label X    # run the prover, write bench/results/X.json
    python -m clank_server.bench summary A B ...  # compare result files

`extract` elaborates the problem file, whose proofs are all `clank_dump`, so the requests are
exactly what the `clank` tactic would send. `run` calls the prover in-process with the given
settings (same flags as the server) and checks against the `bench/` Lake project.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

from .config import Settings, add_arguments, from_arguments
from .lean import LeanChecker
from .llm import LLMClient
from .protocol import ProveRequest
from .prover import Prover

BENCH = Path(__file__).resolve().parents[2] / "bench"


def extract(args: argparse.Namespace) -> None:
    src = args.project / args.file
    proc = subprocess.run(
        ["lake", "env", "lean", "--json", str(args.file)],
        cwd=args.project,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    lines = src.read_text(encoding="utf-8").splitlines()
    problems = []
    for raw in proc.stdout.splitlines():
        msg = json.loads(raw)
        if msg.get("severity") == "error":
            sys.exit(f"{src}:{msg['pos']['line']}: {msg['data']}")
        try:
            req = json.loads(msg.get("data", ""))
        except json.JSONDecodeError:
            continue
        if not isinstance(req, dict) or "protocol" not in req:
            continue
        line = msg["pos"]["line"]
        decl = next(l for l in reversed(lines[:line]) if l.startswith("theorem "))
        problems.append({"name": decl.split()[1], "request": req})
    if proc.returncode != 0 and not problems:
        sys.exit(proc.stderr or proc.stdout)
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        for p in problems:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    print(f"wrote {len(problems)} requests to {args.out}")


async def _run(args: argparse.Namespace, settings: Settings) -> dict:
    problems = [json.loads(l) for l in open(args.requests, encoding="utf-8")]
    if args.only:
        problems = [p for p in problems if p["name"] in args.only]
    problems = problems[: args.limit]
    llm = LLMClient(settings)
    prover = Prover(settings, llm, LeanChecker(settings))
    results = []
    width = max(len(p["name"]) for p in problems)
    try:
        for i, p in enumerate(problems, 1):
            req = ProveRequest.model_validate({**p["request"], "samples": args.samples})
            t0 = time.monotonic()
            resp = await prover.prove(req)
            secs = time.monotonic() - t0
            stats = resp.stats or {}
            rounds = stats.get("rounds", [])
            solved = bool(resp.proofs) and resp.proofs[0].verified
            r = {
                "name": p["name"],
                "solved": solved,
                "round": len(rounds) - 1 if solved else None,
                "seconds": round(secs, 2),
                "proof": resp.proofs[0].tactic if resp.proofs else None,
                "message": resp.message,
                "stats": stats,
            }
            results.append(r)
            if solved:
                c = stats.get("cleanup")
                tidy = f"  cleanup {c['before']}->{c['after']} chars" if c else ""
                status = f"solved in round {r['round']} ({rounds[-1]['kind']}){tidy}"
            else:
                status = f"failed: {(resp.message or '')[:90]}"
            print(f"[{i:2}/{len(problems)}] {p['name']:<{width}} {secs:6.1f}s  {status}", flush=True)
    finally:
        await llm.aclose()
    return {
        "label": args.label,
        "settings": {
            "model": settings.model,
            "prompt_style": settings.prompt_style,
            "samples": args.samples,
            "schedule": list(settings.schedule),
            "repair_width": settings.repair_width,
            "temperature": settings.temperature,
            "time_budget": settings.time_budget,
            "cleanup": settings.cleanup,
        },
        "results": results,
    }


def run(args: argparse.Namespace) -> None:
    settings = from_arguments(args, Settings.from_env())
    report = asyncio.run(_run(args, settings))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"{args.label}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print()
    _print_summary([report])
    print(f"\nwrote {out}")


def _print_summary(reports: list[dict]) -> None:
    rows = []
    for rep in reports:
        res = rep["results"]
        solved = [r for r in res if r["solved"]]
        by_round = [sum(1 for r in solved if r["round"] <= k) for k in range(4)]
        cleaned = [r["stats"]["cleanup"] for r in solved if r["stats"].get("cleanup")]
        shrink = (
            f"{100 * (1 - sum(c['after'] for c in cleaned) / sum(c['before'] for c in cleaned)):.0f}%"
            if cleaned
            else "-"
        )
        rows.append(
            (
                rep["label"],
                ",".join(rep["settings"]["schedule"]),
                f"{len(solved)}/{len(res)}",
                " ".join(str(n) for n in by_round),
                f"{sum(r['seconds'] for r in res) / len(res):.1f}s",
                shrink,
            )
        )
    head = ("label", "schedule", "solved", "by round 0..3", "avg time", "cleanup shrink")
    widths = [max(len(str(x)) for x in col) for col in zip(head, *rows)]
    for row in (head, *rows):
        print("  ".join(str(x).ljust(w) for x, w in zip(row, widths)))


def summary(args: argparse.Namespace) -> None:
    reports = [json.loads(Path(f).read_text(encoding="utf-8")) for f in args.files]
    _print_summary(reports)
    if len(reports) > 1:
        names = [r["name"] for r in reports[0]["results"]]
        solved = [{r["name"] for r in rep["results"] if r["solved"]} for rep in reports]
        print("\nper problem (x = solved):")
        for n in names:
            marks = " ".join("x" if n in s else "." for s in solved)
            if any(n in s for s in solved):
                print(f"  {marks}  {n}")


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m clank_server.bench", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract", help="build requests.jsonl from the problem file")
    e.add_argument("--project", type=Path, default=BENCH)
    e.add_argument("--file", type=Path, default=Path("Bench/MiniF2F.lean"))
    e.add_argument("--out", type=Path, default=BENCH / "requests.jsonl")
    e.set_defaults(func=extract)

    r = sub.add_parser("run", help="run the prover on every request")
    r.add_argument("--label", required=True, help="name of this run (results file name)")
    r.add_argument("--requests", type=Path, default=BENCH / "requests.jsonl")
    r.add_argument("--samples", type=int, default=8, help="samples per round")
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--only", nargs="*", help="problem names to run")
    r.add_argument("--out-dir", type=Path, default=BENCH / "results")
    add_arguments(r, Settings.from_env())
    r.set_defaults(func=run, lean_project=BENCH)

    s = sub.add_parser("summary", help="compare result files")
    s.add_argument("files", nargs="+")
    s.set_defaults(func=summary)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
