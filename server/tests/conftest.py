import itertools
import json
import shutil

import httpx
import pytest

needs_lean = pytest.mark.skipif(shutil.which("lean") is None, reason="lean not on PATH")


def fake_llm(replies: list[str]) -> httpx.MockTransport:
    """An OpenAI-compatible endpoint that cycles through `replies`."""
    cycle = itertools.cycle(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        assert json.loads(request.content)["messages"][-1]["role"] == "user"
        return httpx.Response(200, json={"choices": [{"message": {"content": next(cycle)}}]})

    return httpx.MockTransport(handler)
