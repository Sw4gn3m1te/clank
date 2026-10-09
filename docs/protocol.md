# Tactic ↔ server protocol

JSON over HTTP. The current protocol version is **1**. The Lean side lives in
[Clank/Client.lean](../Clank/Client.lean) and the Python side in
[server/clank_server/protocol.py](../server/clank_server/protocol.py). Keep all three in sync.

## `GET /health`

```json
{"status": "ok", "protocol": 1}
```

## `POST /v1/prove`

### Request

| field          | type                     | meaning |
|----------------|--------------------------|---------|
| `protocol`     | int                      | Protocol version. The server answers `400` if it doesn't match. |
| `goal`         | string                   | The goal as the infoview shows it (`ppGoal`). This is the text the model sees. |
| `hypotheses`   | array of `{name, type, value}` | Local context in order. `name` is `null` for inaccessible hypotheses (`h✝`). `value` is set for `let` variables, otherwise `null`. Types are printed with `pp.fullNames`. |
| `target`       | string                   | The goal's type, printed with `pp.fullNames`. |
| `universes`    | array of string          | Universe level names in scope. |
| `imports`      | array of string          | Direct imports of the file containing the `clank` call. |
| `lean_version` | string                   | `Lean.versionString` of the tactic's Lean. |
| `samples`      | int ≥ 1                  | Number of candidate proofs to sample. The server may cap it. |

```json
{
  "protocol": 1,
  "goal": "a b : Nat\n⊢ a + b = b + a",
  "hypotheses": [{"name": "a", "type": "Nat", "value": null},
                 {"name": "b", "type": "Nat", "value": null}],
  "target": "a + b = b + a",
  "universes": [],
  "imports": ["Init"],
  "lean_version": "4.23.0",
  "samples": 8
}
```

### Response

| field     | type                            | meaning |
|-----------|---------------------------------|---------|
| `proofs`  | array of `{tactic, verified}`   | Candidate tactic proofs (the body of a `by` block, without `by`), in the order the tactic should try them. |
| `message` | string or `null`                | A human-readable note, e.g. why no proof was found. The tactic shows it in its error. |

`verified: true` means the server checked the candidate with Lean on the restated goal:

```lean
set_option autoImplicit false
universe u …
theorem clank_check (h₁ : T₁) … : target := by
  <tactic>
#print axioms clank_check
```

A candidate passes if Lean reports no errors, no `sorry`, and only the axioms `propext`,
`Classical.choice` and `Quot.sound`. The server returns only candidates that pass, shortest first.

`verified: false` means the server could not restate the goal (for example, a hypothesis type
refers to an inaccessible name or does not round-trip through the pretty-printer). In that case
the server returns every distinct candidate unchecked.

**The tactic re-checks every candidate in the real file context no matter what `verified` says.**
Server-side checking only filters candidates. It never decides correctness.

Errors such as an unreachable inference endpoint come back as `200` with empty `proofs` and an
explanatory `message`. Malformed requests get FastAPI's usual `422` response.
