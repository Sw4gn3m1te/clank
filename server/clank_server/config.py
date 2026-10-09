"""Server configuration, read from command-line flags with environment-variable fallbacks."""

from __future__ import annotations

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

    # Lake project whose environment is used to check candidates (`lake env lean`).
    # When unset, the `lean` on PATH is run directly, which only resolves core imports.
    lean_project: Path | None = None
    lean_timeout: float = 60.0
    max_parallel_checks: int = max(1, (os.cpu_count() or 2) // 2)

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
