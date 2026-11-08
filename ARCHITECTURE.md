# clank

An AI-powered Lean 4 tactic that closes proof goals using **local LLMs**.

```lean
example (a b : Nat) : a + b = b + a := by clank
```

## Goal

`clank` sends the current proof goal to a locally running language model, gets back complete candidate proofs, verifies them in Lean, and repairs failed attempts using Lean's error messages. When a proof works, the tactic emits a "Try this" suggestion so the user can replace `clank` with the verified proof. Builds stay reproducible and never depend on a model.

The model only *proposes*. Lean's kernel decides correctness, so a wrong model output can never be accepted.

## Key design decisions

- **Local models first.** Inference runs on the user's machine through an OpenAI-compatible HTTP API (llama.cpp server, Ollama, vLLM, LM Studio). Hosted APIs work too, through the same interface.
- **Whole-proof generation with a repair loop.** The design does not use step-by-step tactic search:
  1. sample N complete proofs;
  2. check them in parallel;
  3. feed the best failures, with their Lean errors, back to the model to repair;
  4. repeat for a few rounds, then fall back to fresh samples.
- **Multiple agents only when needed.** If direct attempts fail, a decomposer agent writes a proof skeleton with `have hᵢ : … := sorry` steps. Sub-provers then solve each step in parallel, and the assembled proof is re-checked.
- **Two languages:**
  - **Lean 4**: a thin `clank` tactic, which acts as the editor frontend.
  - **Python**: the prover service (orchestration, agents, Lean worker pool).

## Architecture

```
Editor ── `clank` (Lean tactic, thin)
             │  HTTP/JSON: goals, theorem statement, file context
             ▼
       clank server (Python, asyncio)
        ├─ Supervisor: budget, timeouts, dedup, cache
        ├─ Agents: Prover (sampling), Repairer (fix from errors),
        │          Decomposer (sub-goals → sub-provers)
        ├─ Lean worker pool: warm Lean REPL processes with imports loaded
        └─ Local inference server (OpenAI-compatible API)
             │
             ▼
       Verified proof → re-checked by the tactic in the real file context → "Try this"
```

## Components

### Lean tactic (`Clank/`)
- Syntax `clank` (later possibly `clank?`, which only suggests).
- Pretty-prints goals (`ppGoal`), sends them to the server, receives a candidate proof.
- **Always re-checks** the returned proof:
  - parse it as a `by` block and run it with `evalTactic`;
  - require no remaining goals;
  - require no `sorry` in the instantiated proof term;
  - restore the saved state on failure.
- On success: `Lean.Meta.Tactic.TryThis.addSuggestion`.
- Options via `register_option` (e.g. `clank.endpoint`, `clank.samples`, `clank.timeout`).
- Fast path: try cheap tactics (`simp`, `omega`, `decide`, `aesop`, …) before calling the server.
- Respects heartbeats and `checkInterrupted`, so it never hangs the editor.

### Python server (`server/`)
- Python 3.12+, `asyncio`, with a small HTTP API (FastAPI or similar).
- Lean checking through a pool of Lean REPL processes. Candidate libraries: LeanInteract, PyPantograph, the Lean REPL itself. Pick the best-maintained one.
- Inference client for an OpenAI-compatible endpoint, with configurable model per agent role.
- SQLite cache keyed on goal hash + Lean/Mathlib version.

## Roadmap

1. **MVP:** tactic → server → sample + check → proof → "Try this". No agents yet.
2. Repair loop using Lean error feedback.
3. Caching and config. Cheap-tactic fast path.
4. Context improvements: theorem statement, surrounding file text, premise hints (`apply?` output).
5. Decomposer agent and parallel sub-provers.
6. Evaluation harness on a benchmark set (e.g. miniF2F) to measure success rate per configuration.

## Ideas (not built)

### Project-wide context via retrieval

Problem: the model only sees the current file before `clank`, truncated to `context_chars` (default 1500). Other project modules reach it only as `import` lines and as `apply?` premise hints. Same-file definitions also break the server-side restatement, so the search falls back to unverified candidates. Dumping the whole project into every prompt costs too much, because up to 32 samples each reprocess the prompt.

Approach: give the model a context block for each goal, with a fixed budget, built from three sources:
1. **Definitions the goal uses (deterministic).** Collect the project constants that appear in the target and hypotheses, and send their statements and definitions, following 1–2 levels of dependencies. This needs no model and never misses.
2. **Ranked lemmas from the project.** Search an index of project declarations, querying with the goal plus the current declaration. Rank with embeddings plus BM25. Fill the remaining budget.
3. **Live `apply?` premises.** Keep these: they find lemmas whose conclusion matches the goal, which text similarity misses.

Index:
- One entry per declaration: name, statement, docstring, and the body for short `def`s.
- Built from the Lean environment, not source text, so names and types are resolved.
- Covers only the project's own modules, filtered by module prefix.
- Updated incrementally: hash each declaration and re-embed only those that changed. Rebuild on file change or on demand.

Embedding model:
- General text embedders are weak on raw Lean. Add docstrings or informal descriptions to the entries (as LeanSearch does).
- Lean-specific retrievers, such as ReProver's ByT5 retriever from LeanDojo, were trained for premise selection.
- Start with a general embedder served over an OpenAI-compatible endpoint plus BM25, and measure on `bench` before switching models.

Budget:
- DeepSeek-Prover-V2-7B has about 32k tokens of context, while `context_chars` is about 400 tokens. Raise the default substantially.
- Small projects can skip retrieval and send every project statement without proofs.
- Put the project block at the start of the prompt and keep it stable across samples, so llama-server's prompt cache can reuse it.

Related fix: restate goals server-side against the file prefix (everything before the `clank` call), not just its imports, so goals that use same-file definitions still get repair and decompose.

Suggested order:
1. Raise `context_chars`, and have the Lean side send the definitions the goal uses.
2. Add the project declaration index, kept up to date incrementally, with BM25 + embedding ranking.
3. Evaluate on `bench`, then try a Lean-specific retriever.

## Environment notes

- Development machine: **Windows** (Git Bash / MINGW64). vLLM needs Linux/WSL2; llama.cpp and Ollama run natively on Windows.
- Lean is managed through `elan`. The Lake package is at the repo root.
- Lean version: **v4.29.0**, pinned in `lean-toolchain` and `bench/lean-toolchain`, with Mathlib `v4.29.0` in `bench`. The tactic uses Lean internals and the `String.Slice` API, so it does not compile on v4.23. A user project must compile Clank with its own toolchain, and the REPL must be built for the same version.

## Conventions

- Keep the Lean tactic thin: all search and agent logic lives in the Python server.
- Never accept a proof containing `sorry`, `admit`, or new axioms.
- The tactic/server protocol is JSON; keep `Clank/Client.lean` and `server/clank_server/protocol.py` in sync.
