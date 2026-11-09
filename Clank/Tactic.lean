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
  let lines := (text.splitOn "\n").map (·.trimAsciiEnd.copy)
  let lines := lines.reverse.dropWhile (·.isEmpty) |>.reverse |>.dropWhile (·.isEmpty)
  let indents := lines.filter (!·.isEmpty) |>.map fun l => (l.toList.takeWhile (· == ' ')).length
  let n := indents.foldl min (indents.headD 0)
  "\n".intercalate (lines.map (·.drop n |>.copy))

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
    if !stx.isOfKind ``Parser.Term.byTactic then return .error "candidate is not a tactic block"
    -- Running a candidate that calls `clank` would recurse into the server.
    if (stx.find? fun s => s.isOfKind `Clank.clank || s.isOfKind `Clank.clankDump).isSome then
      return .error "candidate calls clank"
    return .ok stx[1]

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

/-- Keywords of the tactics that parse in the current environment, which covers exactly the Lean
core and the file's transitive imports. Symbolic and `?`/`!` variants are left out. -/
def availableTactics : CoreM (Array String) := do
  let some cat := Parser.getParserCategory? (← getEnv) `tactic | return #[]
  let hidden := ["clank", "clank_dump", "sorry", "admit", "native_decide"]
  let isWord (s : String) :=
    s.front.isAlpha && s.all (fun c => c.isAlphanum || c == '_' || c == '\'') && !hidden.contains s
  let names := cat.tables.leadingTable.foldl
    (fun acc tk _ => let s := tk.toString (escape := false); if isWord s then acc.push s else acc) #[]
  return names.qsort (· < ·)

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
      timeout
      tactics := ← availableTactics }

/-- Run `x` with `n` thousand heartbeats. Errors, including runtime ones, yield `fallback`. -/
def withBudget (n : Nat) (fallback : α) (x : TacticM α) : TacticM α :=
  tryCatchRuntimeEx
    (withCurrHeartbeats <| withTheReader Core.Context ({ · with maxHeartbeats := n * 1000 }) x)
    fun e => do if e.isInterrupt then throw e else return fallback

open LibrarySearch in
/-- Up to `n` library lemmas whose conclusion matches the goal, in the order `apply?` would try
them. They are only hints for the model, so a failing search just returns fewer. -/
def findPremises (goal : MVarId) (n : Nat) : TacticM (Array Premise) := do
  if n == 0 then return #[]
  withBudget 20000 #[] <| goal.withContext <| withoutModifyingState do
    let mut out : Array Premise := #[]
    for (_, (name, _)) in ← librarySearchSymm libSearchFindDecls goal do
      if out.size ≥ n then break
      if out.any (·.name == name.toString) then continue
      let sig ← PrettyPrinter.ppSignature name
      out := out.push { name := name.toString, statement := toString sig.fmt }
    return out

/-- The last `n` characters of the file before `ref`, starting at a line boundary. -/
def fileContext (ref : Syntax) (n : Nat) : TacticM String := do
  let some pos := ref.getPos? | return ""
  if n == 0 then return ""
  let before := String.Pos.Raw.extract (← getFileMap).source 0 pos
  if before.length ≤ n then return before
  let tail := before.drop (before.length - n)
  return ((tail.dropWhile (· != '\n')).drop 1).copy

/-- The proof of a `theorem`, `lemma` or `example` node with children `args`. -/
def declProof? (args : Array Syntax) : Option Syntax :=
  if args.size == 4 && (args[0]!.isToken "theorem" || args[0]!.isToken "lemma") then some args[3]!
  else if args.size == 3 && args[0]!.isToken "example" then some args[2]!
  else none

/-- Text replacements, in source order, that remove the proofs from a command. -/
partial def stubEdits (stx : Syntax) : Array (String.Pos.Raw × String.Pos.Raw × String) :=
  let replace (part : Syntax) (text : String) := match part.getRange? with
    | some r => #[(r.start, r.stop, text)]
    | none => #[]
  match stx with
  | .node _ kind args =>
    -- Replaying a `clank` call would make the server's own check call the server.
    if kind == `Clank.clank || kind == `Clank.clankDump then replace stx "sorry"
    else match declProof? args with
      -- `where` clauses declare lemmas that later proofs may use, so those proofs stay.
      | some proof =>
        if proof.isOfKind ``Parser.Command.declValSimple && proof[3].isNone then
          replace proof ":= sorry"
        else args.flatMap stubEdits
      | none => args.flatMap stubEdits
  | _ => #[]

/-- The text of `source` between `start` and `stop` with `edits` applied. -/
def applyEdits (source : String) (start stop : String.Pos.Raw)
    (edits : Array (String.Pos.Raw × String.Pos.Raw × String)) : String := Id.run do
  let mut out := ""
  let mut pos := start
  for (first, last, text) in edits do
    out := out ++ String.Pos.Raw.extract source pos first ++ text
    pos := last
  return out ++ String.Pos.Raw.extract source pos stop

/-- Whether `cmd` only reports something, like `#eval` or `#check`. -/
def isDiagnosticCommand (cmd : Syntax) : Bool :=
  match cmd.find? (·.isAtom) with
  | some (.atom _ val) => val.startsWith "#"
  | _ => false

/-- The file between its imports and the command containing `ref`, with earlier proofs replaced by
`sorry`. Candidates only need the statements of earlier theorems, and rechecking their proofs would
be slow. Namespaces, `variable`s and attributes stay, so the server's environment matches the file.
Empty if the file does not parse. -/
def stubbedPrefix (ref : Syntax) : TacticM String := do
  let some pos := ref.getPos? | return ""
  let source := (← getFileMap).source
  let input := Parser.mkInputContext source "<clank>" (normalizeLineEndings := false)
  let (_, start, headerMsgs) ← Parser.parseHeader input
  if headerMsgs.hasErrors then return ""
  let context : Parser.ParserModuleContext := { env := ← getEnv, options := ← getOptions }
  let mut state := start
  let mut out := ""
  repeat
    let (cmd, next, msgs) := Parser.parseCommand input context state {}
    if msgs.hasErrors || Parser.isTerminalCommand cmd then return ""
    if pos.byteIdx < next.pos.byteIdx then return out
    unless isDiagnosticCommand cmd do
      out := out ++ applyEdits source state.pos next.pos (stubEdits cmd)
    state := next
  return ""

/-- Libraries the model knows already or that say nothing about the project. -/
def isLibraryModule (m : Name) : Bool :=
  [`Init, `Std, `Lean, `Mathlib, `Batteries, `Aesop, `Clank].any (·.isPrefixOf m)

/-- Whether `n` comes from the project, which includes the current file. -/
def isProjectConstant (env : Environment) (n : Name) : Bool :=
  match env.getModuleIdxFor? n with
  | some i => match env.header.moduleNames[i.toNat]? with
    | some m => !isLibraryModule m
    | none => false
  | none => true

/-- The constants a declaration's statement, body or constructors mention. -/
def referencedConstants (ci : ConstantInfo) : Array Name :=
  let type := ci.type.getUsedConstants
  match ci with
  | .defnInfo v => type ++ v.value.getUsedConstants
  | .opaqueInfo v => type ++ v.value.getUsedConstants
  | .inductInfo v => type ++ v.ctors.toArray
  | _ => type

/-- Whether `n` is compiler-generated or part of a declaration shown elsewhere. -/
def isGeneratedConstant (env : Environment) (n : Name) (ci : ConstantInfo) : Bool :=
  ci.isCtor || (env.getProjectionFnInfo? n).isSome || n.isInternal || isMatcherCore env n ||
    isAuxRecursor env n || isNoConfusion env n || (match ci with | .recInfo _ => true | _ => false)

/-- Add the project definitions reachable from `names` to `found`, looking through generated
constants. Theorems are skipped, since a definition's meaning never depends on them. -/
partial def collectDefinitions (env : Environment) (seen : IO.Ref NameSet) (found : IO.Ref (Array Name))
    (names : Array Name) : IO Unit := do
  for n in names do
    unless isProjectConstant env n do continue
    if (← seen.get).contains n then continue
    seen.modify (·.insert n)
    let some ci := env.find? n | continue
    if ci matches .thmInfo _ then continue
    if isGeneratedConstant env n ci then collectDefinitions env seen found (referencedConstants ci)
    else found.modify (·.push n)

/-- The source of module `m`, found next to its build output. -/
def moduleSource (m : Name) : IO (Option String) := do
  let olean := (← findOLean m).toString.replace "\\" "/"
  let some root := (olean.splitOn ".lake/build/lib/lean/").head? | return none
  let path : System.FilePath := root ++ "/".intercalate (m.components.map toString) ++ ".lean"
  unless ← path.pathExists do return none
  return some ((← IO.FS.readFile path).replace "\r\n" "\n")

/-- Remove a leading docstring from a declaration's source. -/
def stripDocstring (text : String) : String :=
  let text := text.trimAscii.copy
  if text.startsWith "/--" then "-/".intercalate ((text.splitOn "-/").drop 1) else text

/-- The source text of `n`, or its signature if the source cannot be found. -/
def declarationSource (n : Name) : TacticM String := do
  let signature : TacticM String := return toString (← PrettyPrinter.ppSignature n).fmt
  let some ranges ← findDeclarationRanges? n | signature
  let env ← getEnv
  let text? ← match env.getModuleIdxFor? n with
    | none => pure (some (← getFileMap).source)
    | some i => match env.header.moduleNames[i.toNat]? with
      | some m => try moduleSource m catch _ => pure none
      | none => pure none
  let some text := text? | signature
  let first := ranges.range.pos.line - 1
  let count := ranges.range.endPos.line - ranges.range.pos.line + 1
  return dedent (stripDocstring ("\n".intercalate (((text.splitOn "\n").drop first).take count)))

/-- How many steps from the goal project definitions are collected. -/
def definitionDepth : Nat := 3

/-- Project definitions the goal depends on, nearest first, totalling at most `limit` characters.
They are only context for the model, so a failing search just returns fewer. -/
def findDefinitions (goal : MVarId) (limit : Nat) : TacticM (Array Definition) := do
  if limit == 0 then return #[]
  withBudget 20000 #[] <| goal.withContext do
    let env ← getEnv
    let mut frontier := (← instantiateMVars (← goal.getType)).getUsedConstants
    for decl in ← getLCtx do
      if decl.isImplementationDetail then continue
      frontier := frontier ++ (← instantiateMVars decl.type).getUsedConstants
      if let some v := decl.value? then
        frontier := frontier ++ (← instantiateMVars v).getUsedConstants
    let seen ← IO.mkRef ({} : NameSet)
    let mut out : Array Definition := #[]
    let mut used := 0
    for _ in [0:definitionDepth] do
      let found ← IO.mkRef #[]
      collectDefinitions env seen found frontier
      frontier := #[]
      for n in ← found.get do
        if let some ci := env.find? n then frontier := frontier ++ referencedConstants ci
        let source ← declarationSource n
        if used + source.length > limit then continue
        used := used + source.length
        out := out.push { name := n.toString, source }
    return out

/-- The request for the main goal, with the file context and premise hints. -/
def fullRequest (ref : Syntax) : TacticM ProveRequest := do
  let goal ← getMainGoal
  let opts ← getOptions
  let req ← mkRequest goal (clank.samples.get opts) (clank.timeout.get opts)
  return { req with
    context := ← fileContext ref (clank.context.get opts)
    premises := ← findPremises goal (clank.premises.get opts)
    filePrefix := ← stubbedPrefix ref
    definitions := ← findDefinitions goal (clank.definitions.get opts) }

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
on failure the state is restored and the error is returned. With `maxHeartbeats?`, the candidate
gets that many heartbeats (in thousands, like the `maxHeartbeats` option) instead of the usual
limit.
-/
def runCandidate (goal : MVarId) (tac : Syntax) (maxHeartbeats? : Option Nat := none) :
    TacticM (Option MessageData) := do
  let saved ← saveState
  let msgs ← Core.getMessageLog
  Core.resetMessageLog
  let limit (x : TacticM Unit) : TacticM Unit := match maxHeartbeats? with
    | some n => withCurrHeartbeats <| withTheReader Core.Context ({ · with maxHeartbeats := n * 1000 }) x
    | none => x
  -- Runtime exceptions (heartbeats, recursion depth) mean this candidate failed, not `clank`.
  tryCatchRuntimeEx
    (do
      limit <| Term.withoutErrToSorry <| withoutRecover <| evalTactic tac
      let unsolved ← getUnsolvedGoals
      unless unsolved.isEmpty do throwError "unsolved goals{indentD (goalsToMessageData unsolved)}"
      checkProofTerm goal
      let newMsgs ← Core.getMessageLog
      if newMsgs.hasErrors then throwError "candidate produced errors"
      Core.setMessageLog (msgs ++ newMsgs)
      return none)
    fun e => do
      if e.isInterrupt then throw e
      saved.restore (restoreInfo := true)
      Core.setMessageLog msgs
      return some e.toMessageData

/-- Cheap tactics `clank` tries before asking the server. Tactics that are unavailable (e.g.
`norm_num` without Mathlib) fail to parse and are skipped. -/
def fastPathTactics : List String :=
  ["rfl", "decide", "omega", "simp", "norm_num", "linarith", "positivity"]

/-- Heartbeats (in thousands) for each fast-path tactic. -/
def fastPathHeartbeats : Nat := 20000

/-- Try `fastPathTactics` on `goal` and return the first that closes it. -/
def fastPath (goal : MVarId) (ref : Syntax) : TacticM (Option String) := do
  for text in fastPathTactics do
    if let .ok tac ← parseProof text then
      if (← runCandidate goal (relocate (SourceInfo.fromRef ref) tac) fastPathHeartbeats).isNone then
        return some text
  return none

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
  if clank.fastPath.get opts then
    if let some tac ← fastPath goal stx then
      addSuggestion stx { suggestion := .string tac }
      return
  let resp ← prove (clank.endpoint.get opts) (← fullRequest stx) (clank.timeout.get opts)
  let mut failures : Array MessageData := #[]
  let mut parsedAttempt : Option String := none
  for cand in resp.proofs do
    let text := dedent cand.tactic
    match ← parseProof text with
    | .error e => failures := failures.push m!"{text}\n{e}"
    | .ok tac =>
      match ← runCandidate goal (relocate (SourceInfo.fromRef stx) tac) with
      | some err =>
        failures := failures.push m!"{text}\n{err}"
        parsedAttempt := parsedAttempt <|> some text
      | none =>
        addSuggestion stx { suggestion := .string (← suggestionText stx text) }
        return
  let note := resp.message.map (m!"\n{·}") |>.getD m!""
  let details := failures.toList.take 3 |>.map (m!"\n\n" ++ ·)
  let attempt? := (resp.bestAttempt.map dedent) <|> parsedAttempt <|>
    (resp.proofs[0]?.map (dedent ·.tactic))
  let attemptNote := attempt?.map (m!"\n\nMost promising attempt:\n{·}") |>.getD m!""
  let msg := m!"clank: no proof found ({resp.proofs.size} candidates returned){note}{attemptNote}" ++
    MessageData.joinSep details m!""
  unless (← read).recover do throwError msg
  -- Throwing would backtrack the state and lose the suggestion, so report the error and admit.
  if let some text := attempt? then
    addSuggestion stx { suggestion := .string (← suggestionText stx text) }
      (header := "Most promising attempt (does not close the goal):")
      (codeActionPrefix? := some "Insert failed attempt: ")
  logErrorAt stx msg
  admitGoal goal
  replaceMainGoal []

/-- `clank_dump` logs the JSON request `clank` would send for the main goal and closes it with
`sorry`. Used to build benchmark inputs and to debug the protocol. -/
syntax (name := clankDump) "clank_dump" : tactic

@[tactic clankDump] def evalClankDump : Tactic := fun stx => withMainContext do
  logInfo m!"{(← fullRequest stx).toJson.compress}"
  (← getMainGoal).admit

end Clank
