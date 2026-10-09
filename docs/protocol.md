# Tactic ↔ server protocol

JSON over HTTP. The current protocol version is **2**. The Lean side lives in
[Clank/Client.lean](../Clank/Client.lean) and the Python side in
[server/clank_server/protocol.py](../server/clank_server/protocol.py). Keep all three in sync.

`clank_dump` logs the request `clank` would send for the current goal, which is handy for
debugging this protocol.

## `GET /health`

```json
{"status": "ok", "protocol": 2}
```

## `POST /v1/prove`

### Request

| field          | type                     | meaning |
|----------------|--------------------------|---------|
| `protocol`     | int                      | Protocol version. The server answers `400` if it doesn't match. |
| `goal`         | string                   | The goal as the infoview shows it (`ppGoal`). This is the text the model sees. |
| `hypotheses`   | array of `{name, type, value}` | Local context in order. `name` is `null` for inaccessible hypotheses (`h✝`). `value` is set for `let` variables, otherwise `null`. |
| `target`       | string                   | The goal's type. |
| `universes`    | array of string          | Universe level names in scope. |
| `imports`      | array of string          | Direct imports of the file containing the `clank` call, without `Clank` itself. |
| `opens`        | array of string          | Arguments of the `open` commands in scope, e.g. `"Real"` or `"Nat hiding add"`. |
| `lean_version` | string                   | `Lean.versionString` of the tactic's Lean. |
| `samples`      | int ≥ 1                  | Number of candidate proofs to sample per search round. The server may cap it. |

Hypothesis types and the target are printed with `pp.funBinderTypes`, so that binders such as
`∃ f : ℕ → ℕ, …` keep their types. Otherwise the restated goal would not typecheck.

```json
{
  "protocol": 2,
  "goal": "a b : Nat\n⊢ a + b = b + a",
  "hypotheses": [{"name": "a", "type": "Nat", "value": null},
                 {"name": "b", "type": "Nat", "value": null}],
  "target": "a + b = b + a",
  "universes": [],
  "imports": ["Init"],
  "opens": [],
  "lean_version": "4.23.0",
  "samples": 8
}
```

### Response

| field     | type                            | meaning |
|-----------|---------------------------------|---------|
| `proofs`  | array of `{tactic, verified}`   | Candidate tactic proofs (the body of a `by` block, without `by`), in the order the tactic should try them. |
| `message` | string or `null`                | A human-readable note, e.g. why no proof was found. The tactic shows it in its error. |
| `stats`   | object or `null`                | Diagnostics about the search: one entry per round (`kind`, `samples`, `candidates`, `verified`, `seconds`), `cleanup` (`before`/`after` length in characters), total `seconds`. Informational only. The tactic ignores it. |

`verified: true` means the server checked the candidate with Lean on the restated goal:

```lean
import …
set_option autoImplicit false
open …
universe u …

theorem clank_check_0 (h₁ : T₁) … :
    target := by
  <tactic>
#print axioms clank_check_0
```

A candidate passes if Lean reports no errors, no `sorry`, and only the axioms `propext`,
`Classical.choice` and `Quot.sound`. The server writes all candidates of a round into one file
(`clank_check_0`, `clank_check_1`, …) and checks them with a single Lean run.

When a proof is found, the first entry is the shortest verified proof after cleanup, which
removes redundant steps. The other verified proofs of that round follow, shortest first.

`verified: false` means the server could not restate the goal (for example, a hypothesis type
refers to an inaccessible name or does not round-trip through the pretty-printer). In that case
the server returns the first round's distinct candidates unchecked, without repair or cleanup.

**The tactic re-checks every candidate in the real file context no matter what `verified` says.**
Server-side checking only filters candidates. It never decides correctness.

Errors such as an unreachable inference endpoint come back as `200` with empty `proofs` and an
explanatory `message`. Malformed requests get FastAPI's usual `422` response.
