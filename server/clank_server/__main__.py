"""Entry point: `python -m clank_server` or `clank-server`."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from .app import create_app
from .config import Settings, add_arguments, from_arguments


def main() -> None:
    env = Settings.from_env()
    p = argparse.ArgumentParser(prog="clank-server", description="Prover server for the clank tactic.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    add_arguments(p, env)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(from_arguments(args, env)), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
