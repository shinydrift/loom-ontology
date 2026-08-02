"""The action runtime — the kinetic layer, one object at a time.

`run_upgradeTier(...)` binds its parameters, evaluates the validation rules the spec declares, and
mutates exactly one row. `runtime.py` is the loop, `evaluate.py` runs the expression language over
real values, `result.py` is what comes back.

Eight rules shape the package. Each had an obvious-looking alternative:

- **Rows go through a third port.** `Catalog` reads, `CatalogWriter` changes a table's shape,
  `RowWriter` changes its rows — and none of them extends another. `loom apply` cannot delete a row
  and an action cannot alter a schema, not by policy but because the port each holds has no verb
  for it. The runtime asks for its writer per run, for the one catalog the target object binds, so
  nothing in a serving process holds a row-writable handle between calls.

- **The read before the write is a full physical row.** A modify is an equality-delete plus an
  append, so it rewrites the row entirely and every column no property maps has to be carried
  across or it is silently nulled. Those are the same columns `loom plan` reports as unmanaged and
  leaves alone — this is that rule one level down, where the data is rather than the schema. It is
  why the runtime reads through the `Catalog` port instead of the resolver, which projects a row
  down to exactly the columns a modify must *not* be limited to. A column whose type the ontology
  has no name for (`array`, `struct`, `map` — §1's deferred set) is carried untouched and
  unexamined: the runtime never builds a `PropType` for it, and the conversion is driven by the
  table's own schema, not by anything Loom knows.

- **A read-then-write is one decision, and the commit is what makes it one.** The snapshot the read
  saw (`Catalog.current_snapshot_id`, read *before* the rows, so it is at-or-before the data) is
  carried into the write as `RowWriter`'s required `expect_snapshot_id`, and the implementation
  lowers it into the commit itself — for Iceberg, a requirement the catalog validates against live
  metadata as the metadata pointer swaps. Not a re-read and a comparison: that would leave a window
  between deciding and committing, which narrows the race instead of closing it, and "optimistic
  concurrency" is a phrase that promises closed. A run that conflicts refuses, and changes nothing.

  The check is the **table's** snapshot, so an unrelated append conflicts with a run it had nothing
  to do with. That coarseness is chosen, not tolerated: Iceberg's commit protocol can assert a ref's
  snapshot and nothing finer, so the only narrower test is a row comparison, and a row comparison
  cannot be carried into a commit. Coarse-and-closed beats narrow-and-open. What makes it liveable
  is `MAX_ATTEMPTS`, which is the same decision seen from the other side — a check that invents
  false conflicts is only defensible if the runtime absorbs them rather than every caller.

  It also answers a question the carry-across rule above leaves open, without qualifying it. A
  competing write to a column no property maps *is* a conflict — not because Loom inspected the
  column, but because a modify puts that column back from a read that predates the competing write,
  so committing anyway would restore a stale value over somebody else's newer one. Loom will not
  read that column and will not overwrite it blind, and the snapshot check is how it manages the
  second without doing the first: it compares no columns at all.

- **A conflict is retried here, bounded, and the count is reported.** Three attempts, each a fresh
  read and a fresh evaluation of every rule and every effect expression — never a replay, which
  would write values computed against a row that no longer exists. It is more useful than returning
  the first conflict and it is also the more dangerous choice, because a retry can succeed against a
  row the caller never saw. What makes it sound is that a spec's `validation` rules *are* the
  caller's statement of which states it will act on, and they are re-checked against the newer row —
  a stricter test than the caller's own stale read. Where the competing write genuinely invalidates
  the action, the retry does not paper over it: the run comes back `validation_failed` or
  `object_not_found`, the real reason, instead of a conflict inviting an agent to retry forever. So
  the retry turns most races into nothing and the rest into a decision. `ActionResult.attempts` says
  how many it took, because "applied" after three internal re-reads is a different fact from
  "applied", and `before` is then the row actually written over rather than the one first read.

- **`{{ customer }}` and `newTier != object.tier` are one language.** `expr.parse()` strips the
  braces at load; nothing here ever sees one. An effect value may hold any expression, not only a
  parameter reference — which is why `placedAt: "now()"` works. There is no interpolation: a
  template like `"tier-{{ x }}"` is a parse error, and string building is the language's own `+`.

- **Null is a value you can test, not one you can order.** `null != 'gold'` is true. Not SQL
  three-valued logic — see `evaluate.py` for why an "unknown" precondition would be worse than a
  decided one.

- **A refusal changes nothing, and says everything.** Binding, the read, the uniqueness check and
  every validation rule all run before the single write call, so a refused run is a no-op exactly
  as a refused `loom apply` is. And every rule is evaluated, not just up to the first failure: an
  agent fixing one precondition per call is as miserable as an author fixing one typo per run.
  Nothing a caller, an author or the data can cause is an exception; it is a typed `Failure`.

- **`operation: delete` does not contradict "Loom never drops".** Never-drop is about *inference* —
  Loom refusing to conclude a destruction from **silence** in a spec, because a column no property
  mentions is someone else's data rather than a deleted property. `operation: delete` is the
  opposite of silence: a person wrote the word, named the object type, and the key arrives as a
  declared parameter. The scopes differ too. Never-drop is about **schema** — a column, a table —
  and Loom still never drops one, in any slice. This deletes **one row**, addressed by primary key,
  because an action said to.
"""

from __future__ import annotations

from .evaluate import EvalError, Scope, evaluate
from .result import (
    AMBIGUOUS_KEY,
    APPLIED,
    CONFLICT,
    EXPRESSION_ERROR,
    FAILED,
    MISSING_PARAMETER,
    OBJECT_EXISTS,
    OBJECT_NOT_FOUND,
    PREVIEWED,
    REFUSED,
    RETRYABLE,
    TYPE_ERROR,
    UNKNOWN_PARAMETER,
    VALIDATION_FAILED,
    WRITE_FAILED,
    ActionResult,
    Failure,
)
from .runtime import MAX_ATTEMPTS, ActionError, ActionRuntime, build_runtime

__all__ = [
    "AMBIGUOUS_KEY",
    "APPLIED",
    "CONFLICT",
    "EXPRESSION_ERROR",
    "FAILED",
    "MAX_ATTEMPTS",
    "MISSING_PARAMETER",
    "OBJECT_EXISTS",
    "OBJECT_NOT_FOUND",
    "PREVIEWED",
    "REFUSED",
    "RETRYABLE",
    "TYPE_ERROR",
    "UNKNOWN_PARAMETER",
    "VALIDATION_FAILED",
    "WRITE_FAILED",
    "ActionError",
    "ActionResult",
    "ActionRuntime",
    "EvalError",
    "Failure",
    "Scope",
    "build_runtime",
    "evaluate",
]
