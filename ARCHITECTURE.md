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
  4. run these as a schedule of rounds, by default `fresh`, `repair`, `repair`, `decompose`, stopping at the first verified proof.
- **Multiple agents only when needed.** If direct attempts fail, a decomposer agent writes a proof skeleton with `have hᵢ : … := sorry` steps. Sub-provers then solve each step in parallel, and the assembled proof is re-checked.
- **Two languages:**
  - **Lean 4**: a thin `clank` tactic, which acts as the editor frontend.
  - **Python**: the prover service (orchestration, agents, Lean worker pool).

## Architecture

```
Editor ── `clank` (Lean tactic, thin)
             │  HTTP/JSON: goal, file context, stubbed file prefix,
             │  available tactics, project definitions, premise hints
             ▼
       clank server (Python, asyncio)
        ├─ Supervisor: budget, timeouts, dedup, cache
        ├─ Agents: Prover (sampling), Repairer (fix from errors),
        │          Decomposer (sub-goals → sub-provers)
        ├─ Lean checker: one `lake env lean` per batch, or a pool of warm Lean REPLs
        └─ Inference server (OpenAI-compatible API)
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
  - require no `sorry` in the instantiated proof term and no axioms beyond `propext`, `Classical.choice` and `Quot.sound`;
  - restore the saved state on failure.
- On success: `Lean.Meta.Tactic.TryThis.addSuggestion`.
- On failure: logs the error with the most promising attempt, offers that attempt through the same suggestion mechanism, and admits the goal. Inside `first`/`try` it throws instead.
- Options via `register_option`: `clank.endpoint`, `clank.samples`, `clank.timeout`, `clank.fastPath`, `clank.premises`, `clank.context`, `clank.definitions`.
- Fast path: try `rfl`, `decide`, `omega`, `simp`, `norm_num`, `linarith` and `positivity` before calling the server. Tactics the file does not have fail to parse and are skipped.
- Besides the goal, each request carries:
  - **Available tactics**, read from the environment's parser table, which covers exactly Lean core and the file's transitive imports.
  - **File prefix**: the file up to the current declaration, with earlier `theorem`/`lemma`/`example` proofs replaced by `sorry`. Definitions, namespaces, `variable`s and attributes stay. `#` commands are dropped, earlier `clank` calls become `sorry`, and proofs with `where` clauses are kept because later proofs may use their helpers.
  - **Project definitions** the goal uses: source text without docstrings, up to three steps from the goal, nearest first. Library modules (`Init`, `Std`, `Lean`, `Mathlib`, `Batteries`, `Aesop`) and theorems are skipped.
  - **File context** (the text before `clank`) and **premise hints** from `apply?`.
- Respects heartbeats and `checkInterrupted`, so it never hangs the editor.

### Python server (`server/`)
- Python 3.12+, `asyncio`, FastAPI.
- One server serves one Lake project, set at startup with `--lean-project`. The tactic sends no project path.
- Lean checking: all candidates of a round go in one file, as `theorem clank_check_<i>`. By default each batch runs `lake env lean`; `--repl` switches to a pool of warm Lean REPL processes.
- Inference client for one OpenAI-compatible endpoint and model, with `prover` and `chat` prompt styles.
- SQLite cache keyed on the restated goal, imports, opens, Lean version, dependency revisions and file prefix. Entries that use forbidden tactics are ignored on read.

#### Checking against the file
- Candidates are checked after the file prefix, inside its namespaces and scope, so same-file lemmas and attributes work. If the prefix does not compile, the server falls back to imports only.
- With a prefix, the server also imports `Clank` (for `set_option clank.*`) and skips universes the prefix already declares.
- With a prefix, `sorryAx` is allowed in a candidate's axioms because stubbed theorems depend on it. A candidate's own `sorry` is still an error, and the tactic's final check rejects both. A candidate can therefore pass on the server and still be rejected in the file.

#### Candidate filtering and ranking
- Dropped on extraction: `sorry`, `admit`, `native_decide`, `clank`, references to `clank_check_<i>`, and search commands (`exact?`, `apply?`, `rw?`, `simp?`, `simpa?`, `simp_all?`, `dsimp?`, `aesop?`, `decide?`). Candidates must not see each other: batch siblings state the same goal, so `exact?` would close it with one.
- Dropped before checking: candidates calling a common tactic that the file's tactic list lacks. A round reports these as `out_of_scope`.
- Failures rank by the number of distinct lines before the first error, relative to proof length, so repeated no-ops earn nothing. `unknown tactic` ranks last. Only an in-scope attempt that got past its first step is offered as the most promising attempt.

## Status

Built: tactic → server → sample + check → "Try this", repair loop, decomposer with parallel sub-provers, caching and config, fast path, file and premise context, the evaluation harness in `bench`.

Next, cheapest first:
1. Add the plan paragraph from DeepSeek-Prover-V2's training prompt ("Before producing the Lean 4 code…").
2. Stop rewarding a `have` that restates the goal in the ranking.
3. Compare a hosted frontier model with the local one on project goals.
4. Longer repair loops, repair with `sorry` goal states as feedback, and a rule-based memory of failed identifiers. [AxProverBase](https://arxiv.org/html/2602.24273v1) found iterative refinement to be the largest factor.
5. Retrieve project and library lemmas (see below).

## Model notes

Checked November 2026.

- The prompt shares its slot with the reply, and llama.cpp splits `-c` across `-np` slots. `start-clank.ps1` uses `-np 4 -c 32768` with `--max-tokens 4096`, leaving about 4,096 tokens for the prompt.
- DeepSeek-Prover-V2-7B trained on Mathlib competition problems, so it reaches for `aesop` and Mathlib tactics and handles project-specific goals poorly. On [VeriSoftBench](https://arxiv.org/html/2602.18307v1), which is repository-style like this use case, Mathlib-tuned provers transfer poorly.
- Claude models reject `temperature`, which `llm.py` always sends, so they need an Anthropic SDK path and a larger `max_tokens` for thinking.
- Leanstral 1.5: open weights (Apache 2.0, 112.6 GB; 4-bit about 62–67 GB). Mistral's hosted API was retired on 30 September 2026, so it needs self-hosting.

## Ideas (not built)

### Project-wide context via retrieval

Problem: the model sees the definitions the goal uses and the current file before `clank`, truncated to `context_chars` (default 1500). Lemmas from other project modules reach it only as `apply?` premise hints, and those miss lemmas that are useful as intermediate steps, such as `Set.ext`. Dumping the whole project into every prompt costs too much, because up to 32 samples each reprocess the prompt.

Approach: give the model a context block for each goal, with a fixed budget, built from three sources:
1. **Definitions the goal uses (deterministic).** Built, see the tactic section.
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
- DeepSeek-Prover-V2-7B has about 32k tokens of context in total, shared across slots (see Model notes), and the prompt already uses most of a slot's budget. The tactic list alone is about 3,000 characters in a project without Mathlib.
- Small projects can skip retrieval and send every project statement without proofs.
- The project block sits at the start of the prompt and stays stable across samples, so llama-server's prompt cache can reuse it.

Suggested order:
1. Add the project declaration index, kept up to date incrementally, with BM25 + embedding ranking.
2. Evaluate on `bench`, then try a Lean-specific retriever.

## Environment notes

- Development machine: **Windows** (Git Bash / MINGW64). vLLM needs Linux/WSL2; llama.cpp and Ollama run natively on Windows.
- Lean is managed through `elan`. The Lake package is at the repo root.
- Lean version: **v4.29.0**, pinned in `lean-toolchain` and `bench/lean-toolchain`, with Mathlib `v4.29.0` in `bench`. The tactic uses Lean internals and the `String.Slice` API, so it does not compile on v4.23. A user project must compile Clank with its own toolchain, and the REPL must be built for the same version.
- A user project adds Clank as a Lake dependency and `import Clank`.
- `Test-Projects/` is untracked and holds separate git repositories. `start-clank.ps1` currently points the server at `Test-Projects/Existential-Rules-in-Lean-Core`, so it needs editing to serve another project.

## Conventions

- Keep the Lean tactic thin: all search and agent logic lives in the Python server.
- Never accept a proof containing `sorry`, `admit`, new axioms, or a search command such as `exact?`.
- The tactic/server protocol is JSON; keep `Clank/Client.lean` and `server/clank_server/protocol.py` in sync.
