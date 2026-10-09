"""Entry point: `python -m clank_server` or `clank-server`."""

from __future__ import annotations

import argparse
import dataclasses
import logging
from pathlib import Path

import uvicorn

from .app import create_app
from .config import Settings
from .prompt import PROMPT_STYLES


def main() -> None:
    env = Settings.from_env()
    p = argparse.ArgumentParser(prog="clank-server", description=__doc__)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--llm-url", default=env.llm_url, help="OpenAI-compatible base URL (env CLANK_LLM_URL)")
    p.add_argument("--model", default=env.model, help="model name sent to the endpoint (env CLANK_MODEL)")
    p.add_argument(
        "--prompt-style",
        choices=PROMPT_STYLES,
        default=env.prompt_style,
        help="'prover' for Lean prover models (default), 'chat' for general instruct models",
    )
    p.add_argument("--temperature", type=float, default=env.temperature)
    p.add_argument("--max-tokens", type=int, default=env.max_tokens)
    p.add_argument(
        "--lean-project",
        type=Path,
        default=env.lean_project,
        help="Lake project used to check candidates via `lake env lean` (env CLANK_LEAN_PROJECT)",
    )
    p.add_argument("--lean-timeout", type=float, default=env.lean_timeout)
    p.add_argument("--parallel-checks", type=int, default=env.max_parallel_checks)
    args = p.parse_args()

    settings = dataclasses.replace(
        env,
        llm_url=args.llm_url,
        model=args.model,
        prompt_style=args.prompt_style,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        lean_project=args.lean_project,
        lean_timeout=args.lean_timeout,
        max_parallel_checks=args.parallel_checks,
    )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(settings), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
