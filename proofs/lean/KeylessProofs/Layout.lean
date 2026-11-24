/-!
# Non-overlapping display layout (mirrors `keyless.gaze.layout_solver`)

Positions are integers (fixed-point angular units, e.g. microradians), which
is how the guarantees are checked without Mathlib. Index form: `x i` is the
centre of the i-th display in the chosen left-to-right order, `h i ≥ 0` its
half-width, `gap ≥ 0` the minimum bezel gap.
-/

namespace Keyless.Layout

/-- Required centre spacing between display `i` and `i+1`. -/
def g (h : Nat → Int) (gap : Int) (i : Nat) : Int := h i + h (i + 1) + gap

/-- Prefix sums `G 0 = 0`, `G (i+1) = G i + gaps i`. -/
def G (gaps : Nat → Int) : Nat → Int
  | 0 => 0
  | i + 1 => G gaps i + gaps i

/-- Spacing constraints of the chain. -/
def Spaced (gaps : Nat → Int) (x : Nat → Int) : Prop :=
  ∀ i, x (i + 1) - x i ≥ gaps i

/-- Change of variables `y i = x i - G i` used to reduce to isotonic regression. -/
def toIso (gaps : Nat → Int) (x : Nat → Int) (i : Nat) : Int := x i - G gaps i

/-- **Change of variables.** `x` is spaced iff `x - G` is stepwise non-decreasing. -/
theorem spaced_iff_iso_step (gaps x : Nat → Int) :
    Spaced gaps x ↔ ∀ i, toIso gaps x i ≤ toIso gaps x (i + 1) := by
  unfold Spaced toIso
  constructor
  · intro hs i; have := hs i; simp only [G]; omega
  · intro hm i; have := hm i; simp only [G] at this; omega

/-- Stepwise monotone implies monotone. -/
theorem mono_of_step (y : Nat → Int) (hstep : ∀ i, y i ≤ y (i + 1)) :
    ∀ i k, y i ≤ y (i + k) := by
  intro i k
  induction k with
  | zero => simp
  | succ k ih => have := hstep (i + k); rw [← Nat.add_assoc]; omega

/-- **No overlap.** With non-negative half-widths and gap, any spaced layout
keeps every pair of displays disjoint (right edge of `i` ≤ left edge of `j`). -/
theorem no_overlap (h x : Nat → Int) (gap : Int)
    (hh : ∀ i, 0 ≤ h i) (hgap : 0 ≤ gap) (hs : Spaced (g h gap) x) :
    ∀ i k, x i + h i + gap ≤ x (i + k + 1) - h (i + k + 1) := by
  intro i k
  induction k with
  | zero => have := hs i; unfold g at this; simp only [Nat.add_zero]; omega
  | succ k ih =>
    have step := hs (i + k + 1)
    unfold g at step
    have := hh (i + k + 1)
    rw [show i + (k + 1) + 1 = i + k + 1 + 1 by omega]
    omega

/-- Forward sweep `y 0 = x 0`, `y (i+1) = max (x (i+1)) (y i + gaps i)`:
the floating-point guard applied after PAVA in Python. -/
def sweep (gaps x : Nat → Int) : Nat → Int
  | 0 => x 0
  | i + 1 => max (x (i + 1)) (sweep gaps x i + gaps i)

/-- **Sweep soundness.** The sweep always yields a spaced layout. -/
theorem sweep_spaced (gaps x : Nat → Int) : Spaced gaps (sweep gaps x) := by
  intro i; simp only [sweep]; omega

/-- **Sweep is the identity on valid layouts.** It never perturbs a layout the
solver already made valid. -/
theorem sweep_id_of_spaced (gaps x : Nat → Int) (hs : Spaced gaps x) :
    ∀ i, sweep gaps x i = x i := by
  intro i
  induction i with
  | zero => rfl
  | succ i ih => simp only [sweep, ih]; have := hs i; omega

/-- **Sweep only moves displays right (never left).** -/
theorem sweep_ge (gaps x : Nat → Int) : ∀ i, x i ≤ sweep gaps x i := by
  intro i
  cases i with
  | zero => simp [sweep]
  | succ i => simp only [sweep]; omega

end Keyless.Layout
