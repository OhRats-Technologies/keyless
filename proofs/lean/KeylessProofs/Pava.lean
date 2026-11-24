/-!
# Pool-adjacent-violators feasibility (mirrors `layout_solver.pava`)

A block stores an integer weighted sum `s` and a positive integer weight `w`;
its mean is `s / w`. Means are compared by cross-multiplication, which is
exact for positive weights. The stack is a list with the most recent block at
the head. We prove that every push keeps block means non-decreasing from
bottom to top, so the pooled output is monotone. Optimality (the KKT
argument) is checked numerically in `tests/unit/test_layout_solver.py`.
-/

namespace Keyless.Pava

structure Block where
  s : Int
  w : Int

/-- `meanLe a b` ⇔ `a.s / a.w ≤ b.s / b.w` for positive weights. -/
def meanLe (a b : Block) : Prop := a.s * b.w ≤ b.s * a.w

instance (a b : Block) : Decidable (meanLe a b) := by unfold meanLe; infer_instance

def merge (a b : Block) : Block := ⟨a.s + b.s, a.w + b.w⟩

/-- Push `b` on top of stack `st` (head = top), pooling while the top two violate. -/
def push : List Block → Block → List Block
  | [], b => [b]
  | a :: rest, b => if meanLe a b then b :: a :: rest else push rest (merge a b)

/-- Stack invariant: means do not decrease from bottom to top. -/
inductive Sorted : List Block → Prop
  | nil : Sorted []
  | single (a : Block) : Sorted [a]
  | cons (top below : Block) (rest : List Block) :
      meanLe below top → Sorted (below :: rest) → Sorted (top :: below :: rest)

theorem Sorted.tail {a : Block} {rest : List Block} (h : Sorted (a :: rest)) : Sorted rest := by
  cases h with
  | single => exact Sorted.nil
  | cons _ _ _ _ h2 => exact h2

/-- **PAVA invariant.** Pushing any block onto a sorted stack keeps it sorted. -/
theorem push_sorted : ∀ (st : List Block) (b : Block), Sorted st → Sorted (push st b)
  | [], b, _ => by simp [push]; exact Sorted.single b
  | a :: rest, b, h => by
    simp only [push]
    split
    · next hle => exact Sorted.cons b a rest hle h
    · exact push_sorted rest (merge a b) h.tail

/-- Folding pushes over any input list yields a sorted stack. -/
theorem fold_sorted (xs : List Block) :
    ∀ st, Sorted st → Sorted (xs.foldl push st) := by
  induction xs with
  | nil => intro st h; simpa using h
  | cons x xs ih => intro st h; exact ih (push st x) (push_sorted st x h)

/-- **PAVA output is monotone**: running PAVA from an empty stack always gives
non-decreasing block means. -/
theorem pava_sorted (xs : List Block) : Sorted (xs.foldl push []) :=
  fold_sorted xs [] Sorted.nil

/-- Pooling conserves total weight (no evidence is dropped). -/
def totalW : List Block → Int
  | [] => 0
  | a :: rest => a.w + totalW rest

theorem push_totalW : ∀ (st : List Block) (b : Block), totalW (push st b) = totalW st + b.w
  | [], b => by simp [push, totalW]
  | a :: rest, b => by
    simp only [push]
    split
    · simp [totalW]; omega
    · rw [push_totalW rest (merge a b)]; simp [totalW, merge]; omega

end Keyless.Pava
