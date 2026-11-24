/-!
# Click-label gate (mirrors `keyless.gaze.learner.inlier_weight`)

A click becomes a training label with weight
`w = π·L / (π·L + (1-π)·U)`, the posterior probability that the user was
looking at the click target (inlier likelihood `L`, uniform outlier density
`U`). In fixed point (all quantities scaled non-negative integers) we prove
the weight is a valid probability and is monotone in `L`: a click closer to
the predicted gaze never receives less weight than one farther away.
-/

namespace Keyless.ClickGate

/-- Numerator and denominator of the gate weight. -/
def num (p L : Nat) : Nat := p * L
def den (p q L U : Nat) : Nat := p * L + q * U

/-- **Valid probability.** `0 ≤ num ≤ den`. -/
theorem weight_le_one (p q L U : Nat) : num p L ≤ den p q L U := by
  unfold num den; omega

/-- **Monotone in the inlier likelihood.** If `L₁ ≤ L₂` then
`num(L₁)/den(L₁) ≤ num(L₂)/den(L₂)`, stated by cross-multiplication. -/
theorem weight_mono (p q U L₁ L₂ : Nat) (h : L₁ ≤ L₂) :
    num p L₁ * den p q L₂ U ≤ num p L₂ * den p q L₁ U := by
  unfold num den
  -- (pL₁)(pL₂ + qU) ≤ (pL₂)(pL₁ + qU)  ⇔  pL₁·qU ≤ pL₂·qU
  have h1 : p * L₁ * (q * U) ≤ p * L₂ * (q * U) :=
    Nat.mul_le_mul_right _ (Nat.mul_le_mul_left _ h)
  rw [Nat.mul_add, Nat.mul_add, Nat.mul_comm (p * L₂) (p * L₁)]
  omega

end Keyless.ClickGate
