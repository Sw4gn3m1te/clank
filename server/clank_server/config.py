"""Server configuration: defaults, overridden by a TOML config file, environment variables and
command-line flags, in that order."""

from __future__ import annotations

import argparse
import dataclasses
import os
import tomllib
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
    # Characters of the user's file and number of lemma hints included in prompts. Keep the
    # prompt small enough for the model's context (llama.cpp splits -c across -np slots).
    context_chars: int = 1500
    max_premises: int = 8
    max_definition_chars: int = 4000
    temperature: float = 0.8
    max_tokens: int = 1024
    llm_timeout: float = 120.0
    # Upper bound on `samples` requested by the tactic.
    max_samples: int = 32

    # Search: one round per entry, each drawing `samples` proofs. "fresh" samples new proofs,
    # "repair" asks the model to fix the most promising failures so far, using Lean's errors,
    # "decompose" splits the goal into subgoals with a proof sketch (see prover.py).
    schedule: tuple[str, ...] = ("fresh", "repair", "repair", "decompose")
    # Valid sketches a decompose round tries to complete.
    max_sketches: int = 2
    # Number of failed attempts a repair round works on (the samples are split between them).
    repair_width: int = 4
    # Seconds the search (and cleanup) should take at most. A round or cleanup step is skipped
    # if, judging by the previous one, it would not finish in time. Requests that carry a
    # `timeout` get at most that minus 10 seconds.
    time_budget: float = 100.0
    # Remove redundant steps from the proof that is returned first.
    cleanup: bool = True
    # SQLite cache of verified proofs; None disables caching.
    cache_path: Path | None = Path.home() / ".cache" / "clank" / "proofs.sqlite"

    # Lake project whose environment is used to check candidates (`lake env lean`).
    # When unset, the `lean` on PATH is run directly, which only resolves core imports.
    lean_project: Path | None = None
    lean_timeout: float = 120.0
    # Concurrent Lean processes (or REPL workers). With Mathlib each needs a few GB of memory, so
    # keep this small.
    max_parallel_checks: int = 2
    # Lean REPL executable (github.com/leanprover-community/repl, built for the same Lean
    # version). When set, candidates are checked by warm REPL processes instead of a fresh `lean`
    # per batch, which avoids reloading the imports every time.
    repl_path: Path | None = None
    # Imports the REPL workers load at startup, e.g. ["Mathlib"].
    warm_imports: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, base: Settings | None = None) -> Settings:
        """`base` (default: the defaults) with the CLANK_* environment variables applied."""
        base = base or cls()
        env = os.environ
        values: dict = {}
        for var, field in [("CLANK_LLM_URL", "llm_url"), ("CLANK_MODEL", "model"),
                           ("CLANK_PROMPT_STYLE", "prompt_style")]:
            if env.get(var):
                values[field] = env[var]
        if env.get("CLANK_API_KEY"):
            values["api_key"] = env["CLANK_API_KEY"]
        if env.get("CLANK_LEAN_PROJECT"):
            values["lean_project"] = Path(env["CLANK_LEAN_PROJECT"])
        return dataclasses.replace(base, **values)

    @classmethod
    def from_file(cls, path: Path, base: Settings | None = None) -> Settings:
        """`base` (default: the defaults) with the settings of a TOML file applied. Keys are field
        names. Relative paths are relative to the file; an empty path disables the setting."""
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise SystemExit(f"cannot read config {path}: {e}") from e
        fields = {f.name for f in dataclasses.fields(cls)}
        if unknown := sorted(set(data) - fields):
            raise SystemExit(f"{path}: unknown settings {unknown}")
        for key in ("lean_project", "cache_path", "repl_path"):
            if key in data:
                data[key] = (path.parent / Path(data[key]).expanduser()).resolve() if data[key] else None
        for key in ("schedule", "warm_imports"):
            if key in data:
                data[key] = tuple(data[key])
        return dataclasses.replace(base or cls(), **data)

    @classmethod
    def load(cls, argv: list[str] | None = None) -> tuple[Settings, argparse.ArgumentParser]:
        """Defaults, then the config file (`--config` or CLANK_CONFIG), then the environment.
        Also returns a parser for `--config`, to use as a parent of the full parser."""
        pre = argparse.ArgumentParser(add_help=False)
        pre.add_argument("--config", type=Path, default=os.environ.get("CLANK_CONFIG") or None,
                         help="TOML file with settings (env CLANK_CONFIG); flags override it")
        known, _ = pre.parse_known_args(argv)
        base = cls.from_file(known.config) if known.config else cls()
        return cls.from_env(base), pre


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
    p.add_argument("--context-chars", type=int, default=env.context_chars,
                   help="characters of file context in prompts (0 disables)")
    p.add_argument("--max-premises", type=int, default=env.max_premises,
                   help="lemma hints in prompts (0 disables)")
    p.add_argument("--max-definition-chars", type=int, default=env.max_definition_chars,
                   help="characters of project definitions in prompts (0 disables)")
    p.add_argument("--temperature", type=float, default=env.temperature)
    p.add_argument("--max-tokens", type=int, default=env.max_tokens)
    p.add_argument(
        "--schedule",
        default=",".join(env.schedule),
        help="comma-separated search rounds: 'fresh', 'repair' or 'decompose' (default: %(default)s)",
    )
    p.add_argument("--repair-width", type=int, default=env.repair_width)
    p.add_argument("--time-budget", type=float, default=env.time_budget, help="seconds")
    p.add_argument("--no-cleanup", dest="cleanup", action="store_false", help="skip proof cleanup")
    p.add_argument("--cache", dest="cache_path", type=Path, default=env.cache_path,
                   help="SQLite file for cached proofs (default: %(default)s)")
    p.add_argument("--no-cache", dest="cache_path", action="store_const", const=None,
                   help="disable the proof cache")
    p.add_argument(
        "--lean-project",
        type=Path,
        default=env.lean_project,
        help="Lake project used to check candidates via `lake env lean` (env CLANK_LEAN_PROJECT)",
    )
    p.add_argument("--lean-timeout", type=float, default=env.lean_timeout, help="seconds per batch")
    p.add_argument("--parallel-checks", type=int, default=env.max_parallel_checks)
    p.add_argument("--repl", dest="repl_path", type=Path, default=env.repl_path,
                   help="Lean REPL executable; enables warm checking (see README)")
    p.add_argument("--warm", default=",".join(env.warm_imports),
                   help="comma-separated imports the REPL loads at startup, e.g. Mathlib")


def from_arguments(args: argparse.Namespace, env: Settings) -> Settings:
    schedule = tuple(r.strip() for r in args.schedule.split(",") if r.strip())
    if not schedule or any(r not in ("fresh", "repair", "decompose") for r in schedule):
        raise SystemExit(f"invalid --schedule {args.schedule!r}: use 'fresh', 'repair', 'decompose'")
    return dataclasses.replace(
        env,
        llm_url=args.llm_url,
        model=args.model,
        prompt_style=args.prompt_style,
        context_chars=args.context_chars,
        max_premises=args.max_premises,
        max_definition_chars=args.max_definition_chars,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        schedule=schedule,
        repair_width=args.repair_width,
        time_budget=args.time_budget,
        cleanup=args.cleanup,
        cache_path=args.cache_path,
        lean_project=args.lean_project,
        lean_timeout=args.lean_timeout,
        max_parallel_checks=args.parallel_checks,
        repl_path=args.repl_path,
        warm_imports=tuple(m.strip() for m in args.warm.split(",") if m.strip()),
    )
