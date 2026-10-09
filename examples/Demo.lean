import Clank

/-! Needs a running clank server (see README.md). Not part of the default build target. -/

example (a b : Nat) : a + b = b + a := by rw [Nat.add_comm]

theorem zero_add' : ∀ n : Nat, 0 + n = n := by
  intro n
  <;> simp
  <;> rfl

-- Ask for more samples and point at a non-default server.
set_option clank.samples 16 in
set_option clank.endpoint "http://127.0.0.1:8765" in
example (xs ys : List Nat) : (xs ++ ys).length = ys.length + xs.length := by
  induction xs <;> simp_all [Nat.add_comm, Nat.add_assoc]
  <;> rfl
