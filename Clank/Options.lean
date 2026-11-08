import Lean

/-! User-facing options for the `clank` tactic. -/

register_option clank.endpoint : String := {
  defValue := "http://127.0.0.1:8765"
  descr := "Base URL of the clank prover server"
}

register_option clank.samples : Nat := {
  defValue := 8
  descr := "Number of candidate proofs the clank server should sample"
}

register_option clank.timeout : Nat := {
  defValue := 120
  descr := "Timeout in seconds for a single clank server request"
}

register_option clank.fastPath : Bool := {
  defValue := true
  descr := "Try cheap tactics (rfl, decide, omega, simp, norm_num, ...) before asking the server"
}

register_option clank.premises : Nat := {
  defValue := 8
  descr := "Number of library lemmas matching the goal (as `apply?` finds them) sent as hints; 0 disables"
}

register_option clank.context : Nat := {
  defValue := 4000
  descr := "Characters of the file before the `clank` call sent as context; 0 disables"
}
