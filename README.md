# clank

A Lean 4 tactic that closes proof goals using local LLMs.

```lean
example (a b : Nat) : a + b = b + a := by clank
-- Try this: omega
```

`clank` sends the goal to a small Python server. The server samples candidate proofs from an
OpenAI-compatible endpoint (llama.cpp, Ollama, vLLM, LM Studio, …) and checks them with Lean.
The tactic then re-checks the working candidates in your file and suggests the first one that
passes. The model only proposes proofs. Lean decides whether they are correct.

## Setup

Requirements: `elan`, Python 3.12+, and `curl` on PATH (Windows 10+ ships it).

```sh
lake build                                   # the Lean tactic

cd server
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # .venv/bin/python on Linux/macOS
```

Start an inference server. The tested setup is llama.cpp with
[DeepSeek-Prover-V2-7B](https://huggingface.co/unsloth/DeepSeek-Prover-V2-7B-GGUF) at Q8_0. It
fits in 16 GB of VRAM and proves typical goals in about 10 s:

```sh
llama-server -hf unsloth/DeepSeek-Prover-V2-7B-GGUF:Q8_0 --port 8080 \
  -np 8 -c 16384 -ngl 99 -fa on -ctk q8_0 -ctv q8_0 --jinja
```

`-np 8` lets the 8 samples per `clank` call run in parallel, and `-c` is split across the slots.
For general instruction-tuned models, pass `--prompt-style chat` to clank.

Then start clank:

```sh
# llama.cpp (default endpoint http://127.0.0.1:8080/v1)
.venv/Scripts/python -m clank_server --lean-project ..

# Ollama
.venv/Scripts/python -m clank_server --llm-url http://127.0.0.1:11434/v1 --model qwen2.5-coder:7b --lean-project ..
```

`--lean-project` should point at the Lake project whose files use `clank`. The server checks
candidates with `lake env lean` there, so the file's imports resolve. Without it, the server
runs the `lean` on PATH, which only resolves core imports. If the server can't check a goal,
it passes unchecked candidates to the tactic, which still re-checks them. Run
`python -m clank_server --help` to see all flags.

Settings can also go in a TOML file passed with `--config` (or `CLANK_CONFIG`). See
[server/clank.example.toml](server/clank.example.toml). Command-line flags override the file.

### Warm Lean workers (recommended with Mathlib)

By default every check starts a fresh `lean` process, which reloads the imports each time: about
20 s for Mathlib. With the [Lean REPL](https://github.com/leanprover-community/repl), the server
keeps worker processes that load the imports once and then check a round of candidates in under
a second. Build the REPL at the commit for your Lean version (for v4.23.0: `2f8073af`):

```sh
git clone https://github.com/leanprover-community/repl && cd repl
git checkout 2f8073af0a5e3a141fee075652790a2c19132516 && lake build
```

Then pass `--repl path/to/repl/.lake/build/bin/repl` and optionally `--warm Mathlib`, which
loads Mathlib at startup instead of on the first request. `--parallel-checks` sets the number of
workers; each holds its own copy of the imports in memory.

### Proof cache

Verified proofs are cached in `~/.cache/clank/proofs.sqlite`, keyed on the exact goal, its
imports and `open`s, the Lean version and the dependency versions in the project's
`lake-manifest.json`. Asking for the same goal again returns the cached proof without sampling.
Use `--cache PATH` to move the cache, or `--no-cache` to disable it.

## Using it

```lean
import Clank

set_option clank.endpoint "http://127.0.0.1:8765"  -- default
set_option clank.samples 8                         -- default
set_option clank.timeout 120                       -- seconds, default
set_option clank.fastPath true                     -- default
set_option clank.premises 8                        -- lemma hints, default
set_option clank.context 4000                      -- characters of file context, default

theorem foo : ∀ n : Nat, 0 + n = n := by
  clank
```

When `clank` succeeds, it closes the goal and shows a "Try this" suggestion. Replace `clank`
with the suggested proof so your builds don't depend on a model.

Before contacting the server, `clank` tries a few cheap tactics: `rfl`, `decide`, `omega`,
`simp`, `norm_num`, `linarith` and `positivity`. Tactics that aren't available, such as
`norm_num` without Mathlib, are skipped. If one of them closes the goal, it is suggested right
away. Turn this off with `set_option clank.fastPath false`.

## How the search works

The server searches in rounds, each drawing `clank.samples` proofs from the model and checking
all new ones in one Lean run. The default schedule is `fresh,repair,repair,decompose`:

- **fresh**: sample proofs for the goal.
- **repair**: pick the most promising failed attempts so far, i.e. those whose first error comes
  latest, and ask the model to fix them, showing Lean's error messages.
- **decompose**: ask for proof sketches, i.e. `have` steps proved by `sorry` plus the steps that
  finish the proof. Lean checks each sketch and reports the exact goal at each `sorry`. Those
  subgoals are solved in parallel by sub-provers running `fresh,repair` rounds, and the proofs
  are put back into the sketch. A decompose round runs several Lean checks per subgoal, so it is
  only practical with warm Lean workers (`--repl`).

The search stops at the first round that finds a verified proof. **Cleanup** then removes
redundant steps: it tries deleting each step, trailing `<;> tac` or simp lemma list, and keeps
the shortest version that still checks. The search plans its rounds to finish within
`clank.timeout`. Change the schedule with `--schedule`, e.g. `--schedule fresh` for a single
round.

### What the model sees

Besides the goal, prompts include:

- **File context**: the text of your file before the `clank` call, up to `clank.context`
  characters from the tactic and `--context-chars` (default 1500) on the server. Earlier
  definitions and lemmas appear as Lean code, and the proof so far as a comment.
- **Lemma hints**: the library lemmas `apply?` would try for the goal, found by the tactic
  (`clank.premises`, default 8). At most `--max-premises` are shown.

## Benchmark

[bench/](bench/) holds 40 miniF2F problems and the results of comparing search configurations.
See [bench/README.md](bench/README.md).

## Layout

- [Clank/](Clank/): the Lean tactic (options, HTTP client, tactic)
- [server/](server/): the Python prover server (`pytest` in `server/` runs its tests)
- [docs/protocol.md](docs/protocol.md): the JSON protocol between the tactic and the server
- [examples/Demo.lean](examples/Demo.lean): sample uses (needs a running server)
- [bench/](bench/): benchmark problems and harness (a separate Lake project with Mathlib)
