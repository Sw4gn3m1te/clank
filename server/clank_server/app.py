"""HTTP API. See docs/protocol.md."""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException

from .config import Settings
from .lean import LeanChecker
from .llm import LLMClient
from .protocol import PROTOCOL_VERSION, ProveRequest, ProveResponse
from .prover import Prover


def create_app(
    settings: Settings | None = None, llm_transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    settings = settings or Settings.from_env()
    llm = LLMClient(settings, transport=llm_transport)
    prover = Prover(settings, llm, LeanChecker(settings))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await llm.aclose()

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
