import Lean

/-!
# Client for the clank prover server

Speaks the JSON protocol described in `docs/protocol.md`. Lean core has no HTTP client, so requests
go through the `curl` executable (shipped with Windows 10+, macOS and most Linux distributions).
-/

namespace Clank
open Lean

/-- Version of the tactic/server protocol. Bump together with `docs/protocol.md`. -/
def protocolVersion : Nat := 3

/-- A local hypothesis of the goal. `name` is `none` for inaccessible (hygienic) hypotheses. -/
structure Hypothesis where
  name : Option String
  type : String
  value : Option String := none

/-- A library lemma that may help, e.g. one `apply?` would try. -/
structure Premise where
  name : String
  /-- The lemma's signature, e.g. `Nat.add_comm (n m : Nat) : n + m = m + n`. -/
  statement : String

/-- A project definition that the goal depends on. -/
structure Definition where
  name : String
  /-- The definition's source text, without its docstring. -/
  source : String

/-- Body of `POST /v1/prove`. -/
structure ProveRequest where
  /-- The goal as shown in the infoview. This is what the model sees. -/
  goal : String
  hypotheses : Array Hypothesis
  target : String
  universes : Array String
  imports : Array String
  /-- Arguments of the `open` commands in scope, e.g. `Real` or `Nat hiding add`. -/
  opens : Array String
  leanVersion : String
  samples : Nat
  /-- Seconds until the tactic gives up on the request (`clank.timeout`). -/
  timeout : Nat
  /-- Source text of the file before the `clank` call (trimmed), for the model to read. -/
  context : String := ""
  premises : Array Premise := #[]
  /-- Names of the tactics that parse in the file's environment. -/
  tactics : Array String := #[]
  /-- The file up to the current declaration with earlier proofs replaced by `sorry`, for the
  server to check candidates against. Empty if it could not be computed. -/
  filePrefix : String := ""
  /-- Project definitions the goal depends on, nearest first. -/
  definitions : Array Definition := #[]

structure Candidate where
  tactic : String
  /-- Whether the server managed to check the candidate with Lean. -/
  verified : Bool

structure ProveResponse where
  proofs : Array Candidate
  message : Option String := none
  /-- The failed candidate that got furthest, when the search found no proof. -/
  bestAttempt : Option String := none

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
    ("opens", Lean.toJson r.opens),
    ("lean_version", r.leanVersion),
    ("samples", r.samples),
    ("timeout", r.timeout),
    ("context", r.context),
    ("premises", Json.arr (r.premises.map fun p => Json.mkObj [("name", p.name), ("statement", p.statement)])),
    ("tactics", Lean.toJson r.tactics),
    ("prefix", r.filePrefix),
    ("definitions", Json.arr (r.definitions.map fun d => Json.mkObj [("name", d.name), ("source", d.source)]))]

def Candidate.fromJson? (j : Json) : Except String Candidate := do
  return { tactic := ← j.getObjValAs? String "tactic", verified := ← j.getObjValAs? Bool "verified" }

def ProveResponse.fromJson? (j : Json) : Except String ProveResponse := do
  let proofs ← (← j.getObjValAs? (Array Json) "proofs").mapM Candidate.fromJson?
  let message := (j.getObjValAs? String "message").toOption
  let bestAttempt := (j.getObjValAs? String "best_attempt").toOption
  return { proofs, message, bestAttempt }

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
    throwError "clank: request to {url} failed: {err.trimAscii.copy}{if out.isEmpty then "" else "\n" ++ out}"
  return out

/-- Ask the server at `endpoint` for candidate proofs. -/
def prove (endpoint : String) (req : ProveRequest) (timeout : Nat) : CoreM ProveResponse := do
  let url := (endpoint.dropEndWhile (· == '/')).copy ++ "/v1/prove"
  let out ← postJson url req.toJson timeout
  let json ← match Json.parse out with
    | .ok j => pure j
    | .error e => throwError "clank: server returned invalid JSON ({e}):\n{out}"
  match ProveResponse.fromJson? json with
  | .ok r => return r
  | .error e => throwError "clank: unexpected server response ({e}):\n{out}"

end Clank
