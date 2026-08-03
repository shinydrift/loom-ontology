"""The action runtime — the kinetic layer, one object at a time.

`run_upgradeTier(...)` binds its parameters, evaluates the validation rules the spec declares,
mutates exactly one row, and records that it did. `runtime.py` is the loop, `result.py` is what comes
back, `log.py` is what stays behind. The expression language it evaluates is `loom.evaluate`, which
lived here until M5 gave it a second plane — a governance predicate is evaluated over a row by the
same rules and is not an action.

Ten rules shape the package. Each had an obvious-looking alternative:

- **Rows go through a third port, and Loom's own record through a fourth.** `Catalog` reads,
  `CatalogWriter` changes a table's shape, `RowWriter` changes its rows, `EditLogWriter` appends to
  `_loom_meta.edits` — and none of them extends another. `loom apply` cannot delete a row and an
  action cannot alter a schema, not by policy but because the port each holds has no verb for it.
  The runtime asks for its writers per run, for the one catalog the target object binds, so it holds
  no row-writable *typed* reference between calls and `row_writer_for()` stays the one place the
  write plane is named at a call site.

  That sentence used to end "so nothing in a serving process holds a row-writable handle between
  calls", and `run_<action>` is why it does not. A serving process holds `Catalog`s, and a catalog
  that implements every port — every real one does — is one function call from being a row writer
  whatever the runtime does with it, so the handle rule stops being load-bearing the moment a
  long-lived process can write at all. What survives it is stronger, and a fake can prove it: the
  runtime holds a `RowWriter` and an `EditLogWriter` and never a `CatalogWriter`, so **a serving
  process can change the rows the spec's actions declare and no schema at all.**

  The fourth port was the edit log's first question and the count is the honest thing to change:
  writing the log through `insert_row` would have needed a snapshot expectation the append does not
  hold and would let a busy log table refuse the very write it describes, and holding a
  `CatalogWriter` for it would have handed the runtime `alter_table` to buy one append. So the port
  takes **no table name** — there is nothing to point at the wrong table with.

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

- **A refusal changes nothing it was asked to change, and says everything.** Binding, the read, the
  uniqueness check and every validation rule all run before the single write call, so a refused run
  writes no data, exactly as a refused `loom apply` does. And every rule is evaluated, not just up to
  the first failure: an agent fixing one precondition per call is as miserable as an author fixing
  one typo per run. Nothing a caller, an author or the data can cause is an exception; it is a typed
  `Failure`.

  The qualifier is the edit log's doing and is stated rather than slipped in. A refusal *is* recorded
  — an audit trail of successes cannot answer "who tried to delete this customer", and a conflict is
  a refusal too, so a contended row would otherwise leave no trace of the attempts it swallowed.
  `loom apply` still refuses before it holds a writer and records nothing at all, which is a stronger
  instance of the same rule rather than an exception to it.

- **The record is written after the write, and the part that must be atomic travels inside it.**
  Iceberg has no transaction spanning two tables, so a row write and a log append are two commits and
  one of them can be lost. The one that can't be is the row write's own snapshot summary, which
  carries `loom.edit_id` — so a crash in the gap leaves a stamped snapshot with no matching record,
  which a reader can *find*, rather than the silence that makes a log evidence of nothing. That also
  makes `failed` answerable for the first time. A failed append never fails the action: the row has
  already committed, and reporting otherwise would tell a caller to retry a delete that happened.

  What the record holds is the ontology's view — declared properties, the same projection `before`
  and `after` use — extended to a new reader rather than excepted for one. The physical row was the
  alternative, and it would have made this table an unabridged copy of the data that *outlives* the
  row it copies, which is a worse leak than the one the never-report rule exists to prevent.

- **The runtime never invents an actor.** `default_actor()` is honest for `loom apply` and for
  `loom run`, which a person runs, and a lie for `run_<action>` over MCP, where it names whoever
  started `loom serve` and stamps every caller with the same string. So the actor is an argument, the
  CLI passes it in at the one call site where it is true, and when nobody supplies one the log
  records `unknown` — which is worth more than a confident wrong answer.

  The MCP caller now exists and supplies `mcp.actor` — a value an *operator declared*, never one
  Loom inferred, which is the whole distinction the rule was about. Unset, a served run records
  `unknown`, and that is the honest answer rather than a gap: stdio has no authentication, no
  principal and no identity in the protocol, so the edit log over stdio answers what was done, to
  which row, when, with which parameters and whether it refused — and does not answer *who*, because
  the transport has nobody to name. A client-supplied actor was the other option and is worse than
  `unknown`: an audit record whose subject fills in its own name is self-attestation.

- **`operation: delete` does not contradict "Loom never drops".** Never-drop is about *inference* —
  Loom refusing to conclude a destruction from **silence** in a spec, because a column no property
  mentions is someone else's data rather than a deleted property. `operation: delete` is the
  opposite of silence: a person wrote the word, named the object type, and the key arrives as a
  declared parameter. The scopes differ too. Never-drop is about **schema** — a column, a table —
  and Loom still never drops one, in any slice. This deletes **one row**, addressed by primary key,
  because an action said to.
"""

from __future__ import annotations

from .log import EDIT_COLUMNS, UNKNOWN_ACTOR, EditLog, EditRecord, require_edit_log
from .result import (
    AMBIGUOUS_KEY,
    APPLIED,
    CONFLICT,
    EXPRESSION_ERROR,
    FAILED,
    LOG_FAILED,
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
from .runtime import MAX_ATTEMPTS, ActionError, ActionRuntime, WriteBinding, bind_writes, build_runtime

__all__ = [
    "AMBIGUOUS_KEY",
    "APPLIED",
    "CONFLICT",
    "EDIT_COLUMNS",
    "EXPRESSION_ERROR",
    "FAILED",
    "LOG_FAILED",
    "MAX_ATTEMPTS",
    "MISSING_PARAMETER",
    "OBJECT_EXISTS",
    "OBJECT_NOT_FOUND",
    "PREVIEWED",
    "REFUSED",
    "RETRYABLE",
    "TYPE_ERROR",
    "UNKNOWN_ACTOR",
    "UNKNOWN_PARAMETER",
    "VALIDATION_FAILED",
    "WRITE_FAILED",
    "ActionError",
    "ActionResult",
    "ActionRuntime",
    "EditLog",
    "EditRecord",
    "Failure",
    "build_runtime",
    "bind_writes",
    "WriteBinding",
    "require_edit_log",
]
