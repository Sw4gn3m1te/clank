"""HTTP API. See docs/protocol.md."""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException

from .cache import ProofCache
from .config import Settings
from .lean import ReplChecker, make_checker
from .llm import LLMClient
from .protocol import PROTOCOL_VERSION, ProveRequest, ProveResponse
from .prover import Prover


def create_app(
    settings: Settings | None = None, llm_transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    settings = settings or Settings.from_env()
    llm = LLMClient(settings, transport=llm_transport)
    cache = ProofCache(settings.cache_path) if settings.cache_path else None
    checker = make_checker(settings)
    prover = Prover(settings, llm, checker, cache)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if isinstance(checker, ReplChecker) and settings.warm_imports:
            await checker.warm(list(settings.warm_imports))
        yield
        await llm.aclose()
        await checker.close()
        if cache:
            cache.close()

    app = FastAPI(title="clank server", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "protocol": PROTOCOL_VERSION}

    @app.post("/v1/prove")
    async def prove(req: ProveRequest) -> ProveResponse:
        if req.protocol != PROTOCOL_VERSION:
            raise HTTPException(
                400, f"protocol version {req.protocol} not supported (server: {PROTOCOL_VERSION})"
            )
        return await prover.prove(req)

    return app
