import itertools
import json
import shutil

import httpx
import pytest

needs_lean = pytest.mark.skipif(shutil.which("lean") is None, reason="lean not on PATH")


def fake_llm(replies) -> httpx.MockTransport:
    """An OpenAI-compatible endpoint. `replies` is a list to cycle through, or a function from the
    last user message to a reply."""
    if not callable(replies):
        cycle = itertools.cycle(replies)
        replies = lambda _: next(cycle)  # noqa: E731

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        last = json.loads(request.content)["messages"][-1]
        assert last["role"] == "user"
        return httpx.Response(200, json={"choices": [{"message": {"content": replies(last["content"])}}]})

    return httpx.MockTransport(handler)
