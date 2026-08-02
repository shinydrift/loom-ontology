"""The action runtime — the kinetic layer, one object at a time.

`run_upgradeTier(...)` binds its parameters, evaluates the validation rules the spec declares, and
mutates exactly one row. `runtime.py` is the loop, `evaluate.py` runs the expression language over
real values, `result.py` is what comes back.

Seven rules shape the package. Each had an obvious-looking alternative:

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

- **A read-then-write is not a transaction, and nothing here pretends otherwise.** The *write* is
  one Iceberg commit. The read and the write together are two, and between them the row can move.
  This slice records the snapshot every read saw (`Catalog.current_snapshot_id`, read *before* the
  rows so the record is at-or-before the data) and checks nothing. `ActionResult` says
  `concurrency: recorded, not enforced`, and `Failure.CONFLICT` is defined and raised by nobody, so
  the next slice is a check and one failure rather than a new result shape.

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
from .runtime import ActionError, ActionRuntime, build_runtime

__all__ = [
    "AMBIGUOUS_KEY",
    "APPLIED",
    "CONFLICT",
    "EXPRESSION_ERROR",
    "FAILED",
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
