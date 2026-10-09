"""Server configuration, read from command-line flags with environment-variable fallbacks."""

from __future__ import annotations

import argparse
import dataclasses
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    # OpenAI-compatible inference endpoint (llama.cpp server, Ollama, vLLM, LM Studio, ...).
    llm_url: str = "http://127.0.0.1:8080/v1"
    model: str = "default"
    api_key: str | None = None
    # "prover": the file-completion format of Lean prover models; "chat": instructions for
    # general instruction-tuned models. See prompt.py.
    prompt_style: str = "prover"
    temperature: float = 0.8
    max_tokens: int = 1024
    llm_timeout: float = 120.0
    # Upper bound on `samples` requested by the tactic.
    max_samples: int = 32

    # Search: one round per entry, each drawing `samples` proofs. "fresh" samples new proofs,
    # "repair" asks the model to fix the most promising failures so far, using Lean's errors.
    schedule: tuple[str, ...] = ("fresh", "repair", "repair", "fresh")
    # Number of failed attempts a repair round works on (the samples are split between them).
    repair_width: int = 4
    # Seconds the search (and cleanup) should take at most. A round or cleanup step is skipped
    # if, judging by the previous one, it would not finish in time. Requests that carry a
    # `timeout` get at most that minus 10 seconds.
    time_budget: float = 100.0
    # Remove redundant steps from the proof that is returned first.
    cleanup: bool = True

    # Lake project whose environment is used to check candidates (`lake env lean`).
    # When unset, the `lean` on PATH is run directly, which only resolves core imports.
    lean_project: Path | None = None
    lean_timeout: float = 120.0
    # Concurrent Lean processes. Each checks a whole batch using all cores, and with Mathlib each
    # needs a few GB of memory, so keep this small.
    max_parallel_checks: int = 2

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        project = env.get("CLANK_LEAN_PROJECT")
        return cls(
            llm_url=env.get("CLANK_LLM_URL", cls.llm_url),
            model=env.get("CLANK_MODEL", cls.model),
            api_key=env.get("CLANK_API_KEY") or None,
            prompt_style=env.get("CLANK_PROMPT_STYLE", cls.prompt_style),
            lean_project=Path(project) if project else None,
        )


def add_arguments(p: argparse.ArgumentParser, env: Settings) -> None:
    """Command-line flags for every setting, defaulting to `env`."""
    p.add_argument("--llm-url", default=env.llm_url, help="OpenAI-compatible base URL (env CLANK_LLM_URL)")
    p.add_argument("--model", default=env.model, help="model name sent to the endpoint (env CLANK_MODEL)")
    p.add_argument(
        "--prompt-style",
        choices=("prover", "chat"),
        default=env.prompt_style,
        help="'prover' for Lean prover models (default), 'chat' for general instruct models",
    )
    p.add_argument("--temperature", type=float, default=env.temperature)
    p.add_argument("--max-tokens", type=int, default=env.max_tokens)
    p.add_argument(
        "--schedule",
        default=",".join(env.schedule),
        help="comma-separated search rounds, each 'fresh' or 'repair' (default: %(default)s)",
    )
    p.add_argument("--repair-width", type=int, default=env.repair_width)
    p.add_argument("--time-budget", type=float, default=env.time_budget, help="seconds")
    p.add_argument("--no-cleanup", dest="cleanup", action="store_false", help="skip proof cleanup")
    p.add_argument(
        "--lean-project",
        type=Path,
        default=env.lean_project,
        help="Lake project used to check candidates via `lake env lean` (env CLANK_LEAN_PROJECT)",
    )
    p.add_argument("--lean-timeout", type=float, default=env.lean_timeout, help="seconds per batch")
    p.add_argument("--parallel-checks", type=int, default=env.max_parallel_checks)


def from_arguments(args: argparse.Namespace, env: Settings) -> Settings:
    schedule = tuple(r.strip() for r in args.schedule.split(",") if r.strip())
    if not schedule or any(r not in ("fresh", "repair") for r in schedule):
        raise SystemExit(f"invalid --schedule {args.schedule!r}: use 'fresh' and 'repair'")
    return dataclasses.replace(
        env,
        llm_url=args.llm_url,
        model=args.model,
        prompt_style=args.prompt_style,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        schedule=schedule,
        repair_width=args.repair_width,
        time_budget=args.time_budget,
        cleanup=args.cleanup,
        lean_project=args.lean_project,
        lean_timeout=args.lean_timeout,
        max_parallel_checks=args.parallel_checks,
    )
