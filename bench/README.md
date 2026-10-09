# Benchmark

40 problems from the miniF2F validation split ([Bench/MiniF2F.lean](Bench/MiniF2F.lean)), used to
compare search configurations of the clank server. This is a separate Lake project because it
depends on Mathlib, which the tactic itself does not.

## Setup

```sh
cd bench
lake exe cache get          # prebuilt Mathlib, about 5 GB
lake build Clank
```

Mathlib should live on an SSD. Every check batch imports Mathlib, which reads about 4.3 GB of
`.olean` files. From an SSD with a warm file cache that takes about 20 s. From a hard disk it
took 7 minutes. If the repo is on a hard disk, move `bench/.lake` to an SSD and link it back
(on Windows: `mklink /J bench\.lake <ssd-folder>`).

## Running

From `server/`, with an inference server running (see the top-level README):

```sh
python -m clank_server.bench extract                  # Bench/MiniF2F.lean -> requests.jsonl
python -m clank_server.bench run --label repair       # default schedule
python -m clank_server.bench run --label fresh4 --schedule fresh,fresh,fresh,fresh
python -m clank_server.bench summary results/fresh4.json results/repair.json
```

`extract` elaborates the problem file, whose proofs are all `clank_dump`. The requests in
[requests.jsonl](requests.jsonl) are therefore exactly what the `clank` tactic would send. `run`
accepts every server flag (`--schedule`, `--samples`, `--repair-width`, `--no-cleanup`, …) and
writes per-problem results, including the proofs found and per-round statistics, to
`results/<label>.json`. That directory is not committed.

A problem counts as solved only if the server verified the proof. One problem,
`mathd_algebra_89`, cannot be checked server-side because its statement does not survive
pretty-printing (`(-(3 : ℤ))` prints as `(-3)`), so it counts as unsolved in every configuration.

## Results

_Pending._
