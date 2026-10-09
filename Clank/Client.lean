import Lean

/-!
# Client for the clank prover server

Speaks the JSON protocol described in `docs/protocol.md`. Lean core has no HTTP client, so requests
go through the `curl` executable (shipped with Windows 10+, macOS and most Linux distributions).
-/

namespace Clank
open Lean

/-- Version of the tactic/server protocol. Bump together with `docs/protocol.md`. -/
def protocolVersion : Nat := 1

/-- A local hypothesis of the goal. `name` is `none` for inaccessible (hygienic) hypotheses. -/
structure Hypothesis where
  name : Option String
  type : String
  value : Option String := none

/-- Body of `POST /v1/prove`. -/
structure ProveRequest where
  /-- The goal as shown in the infoview. This is what the model sees. -/
  goal : String
  hypotheses : Array Hypothesis
  target : String
  universes : Array String
  imports : Array String
  leanVersion : String
  samples : Nat

structure Candidate where
  tactic : String
  /-- Whether the server managed to check the candidate with Lean. -/
  verified : Bool

structure ProveResponse where
  proofs : Array Candidate
  message : Option String := none

def Hypothesis.toJson (h : Hypothesis) : Json :=
  Json.mkObj [("name", Lean.toJson h.name), ("type", h.type), ("value", Lean.toJson h.value)]

def ProveRequest.toJson (r : ProveRequest) : Json :=
  Json.mkObj [
    ("protocol", protocolVersion),
    ("goal", r.goal),
    ("hypotheses", Json.arr (r.hypotheses.map Hypothesis.toJson)),
    ("target", r.target),
    ("universes", Lean.toJson r.universes),
    ("imports", Lean.toJson r.imports),
    ("lean_version", r.leanVersion),
    ("samples", r.samples)]

def Candidate.fromJson? (j : Json) : Except String Candidate := do
  return { tactic := ← j.getObjValAs? String "tactic", verified := ← j.getObjValAs? Bool "verified" }

def ProveResponse.fromJson? (j : Json) : Except String ProveResponse := do
  let proofs ← (← j.getObjValAs? (Array Json) "proofs").mapM Candidate.fromJson?
  let message := (j.getObjValAs? String "message").toOption
  return { proofs, message }

/--
POST `body` to `url` and return the response body. Polls for completion so that the request can be
cancelled from the editor (`Core.checkInterrupted`), in which case `curl` is killed.
-/
def postJson (url : String) (body : Json) (timeout : Nat) : CoreM String := do
  let child ← IO.Process.spawn {
    cmd := "curl"
    args := #["--silent", "--show-error", "--fail-with-body", "--max-time", toString timeout,
      "--header", "Content-Type: application/json", "--data-binary", "@-", url]
    stdin := .piped, stdout := .piped, stderr := .piped }
  let (stdin, child) ← child.takeStdin
  stdin.putStr body.compress
  stdin.flush
  -- `stdin` is not used past this point, so the handle is dropped and closed, signalling EOF.
  let stdout ← IO.asTask child.stdout.readToEnd .dedicated
  let stderr ← IO.asTask child.stderr.readToEnd .dedicated
  let mut exitCode? := none
  while exitCode?.isNone do
    exitCode? ← child.tryWait
    if exitCode?.isNone then
      try Core.checkInterrupted catch e => child.kill; throw e
      IO.sleep 20
  let out ← IO.ofExcept stdout.get
  let err ← IO.ofExcept stderr.get
  if exitCode? != some 0 then
    throwError "clank: request to {url} failed: {err.trim}{if out.isEmpty then "" else "\n" ++ out}"
  return out

/-- Ask the server at `endpoint` for candidate proofs. -/
def prove (endpoint : String) (req : ProveRequest) (timeout : Nat) : CoreM ProveResponse := do
  let url := endpoint.dropRightWhile (· == '/') ++ "/v1/prove"
  let out ← postJson url req.toJson timeout
  let json ← match Json.parse out with
    | .ok j => pure j
    | .error e => throwError "clank: server returned invalid JSON ({e}):\n{out}"
  match ProveResponse.fromJson? json with
  | .ok r => return r
  | .error e => throwError "clank: unexpected server response ({e}):\n{out}"

end Clank
