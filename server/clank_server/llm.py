"""Client for an OpenAI-compatible chat completions endpoint."""

from __future__ import annotations

import asyncio
import logging

import httpx

from .config import Settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        headers = {"Authorization": f"Bearer {settings.api_key}"} if settings.api_key else {}
        self._settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.llm_url.rstrip("/") + "/",
            headers=headers,
            timeout=settings.llm_timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def complete(self, messages: list[dict[str, str]]) -> str:
        s = self._settings
        body = {
            "model": s.model,
            "messages": messages,
            "temperature": s.temperature,
            "max_tokens": s.max_tokens,
        }
        try:
            resp = await self._http.post("chat/completions", json=body)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"] or ""
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            raise LLMError(f"{type(e).__name__}: {e}") from e

    async def sample(self, messages: list[dict[str, str]], n: int) -> list[str]:
        """Draw `n` independent completions. Many local servers ignore the `n` parameter, so this
        issues `n` concurrent requests instead. Raises LLMError only if every request fails."""
        results = await asyncio.gather(
            *(self.complete(messages) for _ in range(n)), return_exceptions=True
        )
        texts = [r for r in results if isinstance(r, str)]
        errors = [r for r in results if isinstance(r, BaseException)]
        for e in errors:
            log.warning("sample failed: %s", e)
        if not texts and errors:
            raise LLMError(f"all {n} samples failed; first error: {errors[0]}")
        return texts
