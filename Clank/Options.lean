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
