import Lean
import Clank.Options
import Clank.Client

/-!
# The `clank` tactic

`clank` sends the main goal to the clank server, re-checks every returned candidate proof in the
current context and, on success, closes the goal and suggests the verified proof via "Try this".

The server is never trusted: a candidate is accepted only if it elaborates here without errors,
closes the goal, contains no `sorry`, and uses no axioms beyond `propext`, `Classical.choice` and
`Quot.sound`.
-/

namespace Clank
open Lean Meta Elab Tactic
open Lean.Meta.Tactic.TryThis (addSuggestion getIndentAndColumn)

/-- Axioms a proof found by `clank` may depend on. -/
def allowedAxioms : List Name := [``propext, ``Classical.choice, ``Quot.sound]

/-- Remove trailing blank lines and the common leading indentation of `text`. -/
def dedent (text : String) : String :=
  let lines := (text.splitOn "\n").map (·.trimRight)
  let lines := lines.reverse.dropWhile (·.isEmpty) |>.reverse |>.dropWhile (·.isEmpty)
  let indents := lines.filter (!·.isEmpty) |>.map fun l => (l.takeWhile (· == ' ')).length
  let n := indents.foldl min (indents.headD 0)
  "\n".intercalate (lines.map (·.drop n))

/-- Indent every line but the first by `n` spaces. -/
def indentTail (n : Nat) (text : String) : String :=
  "\n".intercalate <| match text.splitOn "\n" with
    | [] => []
    | l :: ls => l :: ls.map fun l => if l.isEmpty then l else "".pushn ' ' n ++ l

/-- Replace all source positions in `stx` by `info`, so that messages produced while running a
candidate point at the `clank` call rather than at offsets into the candidate's own text. -/
partial def relocate (info : SourceInfo) : Syntax → Syntax
  | .node _ k args => .node info k (args.map (relocate info))
  | .atom _ v => .atom info v
  | .ident _ raw n pre => .ident info raw n pre
  | .missing => .missing

/-- Parse a candidate as the body of a `by` block, returning its tactic sequence. -/
def parseProof (text : String) : CoreM (Except String Syntax) := do
  let src := "by\n  " ++ indentTail 2 text
  match Parser.runParserCategory (← getEnv) `term src "<clank>" with
  | .error e => return .error s!"parse error: {e}"
  | .ok stx =>
    if stx.isOfKind ``Parser.Term.byTactic then return .ok stx[1]
    else return .error "candidate is not a tactic block"

/-- The `open` commands in scope, as arguments to `open` (e.g. `Real`, `Nat hiding add`). The server
replays them so that the pretty-printed goal, which may use scoped notation, parses again. -/
def openArgs : CoreM (Array String) := do
  let mut args := #[]
  for decl in (← getOpenDecls).reverse do
    if let .simple ns except := decl then
      if ns.isAnonymous then continue
      let suffix := if except.isEmpty then "" else " hiding " ++ " ".intercalate (except.map toString)
      args := args.push (toString ns ++ suffix)
  return args

/-- Build the server request for `goal`. -/
def mkRequest (goal : MVarId) (samples timeout : Nat) : TacticM ProveRequest := goal.withContext do
  let goalText := toString (← ppGoal goal)
  -- The statement must parse again on the server: without binder types, `∃ f, f 1 = 2` does not.
  withOptions (·.setBool `pp.funBinderTypes true) do
    let mut hypotheses := #[]
    for decl in ← getLCtx do
      if decl.isImplementationDetail then continue
      let name := if decl.userName.hasMacroScopes then none else some decl.userName.toString
      let value ← decl.value?.mapM fun v => return toString (← ppExpr v)
      hypotheses := hypotheses.push { name, type := toString (← ppExpr decl.type), value }
    -- Proofs never need the tactic itself.
    let imports := (← getEnv).imports.map (·.module) |>.filter (!(`Clank).isPrefixOf ·)
    return {
      goal := goalText
      hypotheses
      target := toString (← ppExpr (← goal.getType))
      universes := (← Term.getLevelNames).reverse.toArray.map (·.toString)
      imports := imports.map (·.toString)
      opens := ← openArgs
      leanVersion := Lean.versionString
      samples
      timeout }

/-- Check that the (now assigned) `goal` has an acceptable proof term. -/
def checkProofTerm (goal : MVarId) : TacticM Unit := do
  let pf ← instantiateMVars (mkMVar goal)
  if pf.hasSorry then throwError "proof contains `sorry`"
  if pf.hasExprMVar then throwError "proof contains metavariables"
  let mut bad : Array Name := #[]
  for c in pf.getUsedConstants do
    for ax in ← collectAxioms c do
      unless allowedAxioms.contains ax || bad.contains ax do bad := bad.push ax
  unless bad.isEmpty do throwError "proof uses disallowed axioms {bad}"

/--
Run candidate `tac` on the main goal. On success the goal is closed and `none` is returned;
on failure the state is restored and the error is returned.
-/
def runCandidate (goal : MVarId) (tac : Syntax) : TacticM (Option MessageData) := do
  let saved ← saveState
  let msgs ← Core.getMessageLog
  Core.resetMessageLog
  try
    Term.withoutErrToSorry <| withoutRecover <| evalTactic tac
    let unsolved ← getUnsolvedGoals
    unless unsolved.isEmpty do throwError "unsolved goals{indentD (goalsToMessageData unsolved)}"
    checkProofTerm goal
    let newMsgs ← Core.getMessageLog
    if newMsgs.hasErrors then throwError "candidate produced errors"
    Core.setMessageLog (msgs ++ newMsgs)
    return none
  catch e =>
    if e.isInterrupt || e.isRuntime then throw e
    saved.restore (restoreInfo := true)
    Core.setMessageLog msgs
    return some e.toMessageData

/--
Format `tac` for a "Try this" replacing `ref`. If `ref` starts its line, continuation lines are
aligned with it. Otherwise (as in `:= by clank`) a multi-line proof starts on a new line, indented
two spaces past the current line.
-/
def suggestionText (ref : Syntax) (tac : String) : TacticM String := do
  let fileMap ← getFileMap
  let (indent, column) := match ref.getRange? with
    | some range => getIndentAndColumn fileMap range
    | none => (0, 0)
  if indent == column || !tac.contains '\n' then
    return indentTail column tac
  let n := indent + 2
  return "\n".pushn ' ' n ++ indentTail n tac

syntax (name := clank) "clank" : tactic

@[tactic clank] def evalClank : Tactic := fun stx => withMainContext do
  let goal ← getMainGoal
  let opts ← getOptions
  let timeout := clank.timeout.get opts
  let req ← mkRequest goal (clank.samples.get opts) timeout
  let resp ← prove (clank.endpoint.get opts) req timeout
  let mut failures : Array MessageData := #[]
  for cand in resp.proofs do
    let text := dedent cand.tactic
    match ← parseProof text with
    | .error e => failures := failures.push m!"{text}\n{e}"
    | .ok tac =>
      match ← runCandidate goal (relocate (SourceInfo.fromRef stx) tac) with
      | some err => failures := failures.push m!"{text}\n{err}"
      | none =>
        addSuggestion stx { suggestion := .string (← suggestionText stx text) }
        return
  let note := resp.message.map (m!"\n{·}") |>.getD m!""
  let details := failures.toList.take 3 |>.map (m!"\n\n" ++ ·)
  throwError m!"clank: no proof found ({resp.proofs.size} candidates returned){note}" ++
    MessageData.joinSep details m!""

/-- `clank_dump` logs the JSON request `clank` would send for the main goal and closes it with
`sorry`. Used to build benchmark inputs and to debug the protocol. -/
syntax (name := clankDump) "clank_dump" : tactic

@[tactic clankDump] def evalClankDump : Tactic := fun _ => withMainContext do
  let opts ← getOptions
  let req ← mkRequest (← getMainGoal) (clank.samples.get opts) (clank.timeout.get opts)
  logInfo m!"{req.toJson.compress}"
  (← getMainGoal).admit

end Clank
