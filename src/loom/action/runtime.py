"""The action runtime — one declared action, run against one row, and a record that it happened.

The whole of an action is five steps, in this order and no other:

1. **Bind.** Caller-supplied parameters are coerced to their declared types by the same function
   the read path coerces with (`model.coerce_value`), defaults are applied, and a missing required
   parameter is a failure rather than a `None` that surfaces three steps later.
2. **Read.** For `modify`/`delete`, the target row — *all* of it, physically. See `_read`.
3. **Evaluate.** Every validation rule, against the bound parameters and the row just read. All of
   them, so a caller sees every precondition it failed rather than one per attempt.
4. **Write.** One call to one `RowWriter` verb, which is one Iceberg transaction — carrying the
   snapshot step 2 read, so that the read and the write take effect as one decision or not at all.

5. **Record.** One row in `_loom_meta.edits`, once per run and after the last attempt, through a
   fourth port that can name no table. The row write has already stamped the same `edit_id` into its
   own Iceberg commit, which is the only attribution that is atomic with the edit; this is a second
   commit, and the ordering it chose is written up on `ActionRuntime._record`.

Everything that can refuse happens in 1-3, so **a run that refuses changes nothing it was asked to
change** — the same promise `loom apply` makes, and for the same reason: a half-done write leaves a
row that neither the caller nor the spec describes. A run that conflicts refuses on that same
definition: the write was declined before it committed, not undone afterwards.

That sentence used to end at "changes nothing", and step 5 is why it does not. **A refused run is
recorded.** It writes no data — the promise the words were protecting is intact — but it appends a
row to Loom's own log, because an audit trail of successes cannot answer *who tried to delete this
customer*, and a conflict is now a refusal too, so a contended row would otherwise leave no trace of
the attempts that lost on it. `ActionRuntime._record` carries the boundary (a run is recorded once it
has named a row) and `action.log` carries the rest.

Steps 1-4 run up to `MAX_ATTEMPTS` times; step 5 runs once. `ActionRuntime.run` carries the argument
for why a conflict is retried here rather than handed straight back; `_write` carries the argument
for why all three operations are checked, and `_conflict` for what the failure tells a caller.

Three boundaries this file is careful about:

- **It does not go through the resolver.** The resolver projects a row down to declared properties,
  which is exactly the set a modify must *not* be limited to — see `_read`. The resolver stays the
  semantic read (projected, paged, and where governance will live); this is a physical read of one
  row by key.
- **It holds a `RowWriter`, never a `CatalogWriter`.** It cannot alter a schema, because the port
  it asks for has no verb for it. The `EditLogWriter` it also holds does not weaken that: it takes
  no table name, so the only thing it can reach is `_loom_meta.edits`. This is the boundary that
  survives being served: a process running `run_<action>` tools can change the rows the spec's
  actions declare and no schema at all — see `mcp.server.build_server`, where the sentence about
  handles is restated for a process that outlives a run.
- **It never invents an actor.** `default_actor()` is not called from here — it is honest for
  `loom apply` and for `loom run`, and a lie for `run_<action>` over MCP, where it would name whoever
  started `loom serve`. The actor is an argument, and when nobody supplies one the log says so. The
  MCP caller passes `mcp.actor`: a string an operator declared about a deployment, which is a
  different kind of thing from one Loom inferred about a process, and `None` when they declared
  none.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from ..catalog.base import (
    Catalog,
    CatalogError,
    ConcurrencyError,
    RowWriter,
    edit_log_writer_for,
    row_writer_for,
)
from ..model import Action, ObjectType, Ontology, coerce_value
from .evaluate import EvalError, Scope, evaluate
from .log import (
    UNKNOWN_ACTOR,
    EditLog,
    EditRecord,
    commit_properties,
    new_edit_id,
    now,
    render_key,
)
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
    TYPE_ERROR,
    UNKNOWN_PARAMETER,
    VALIDATION_FAILED,
    WRITE_FAILED,
    ActionResult,
    Failure,
)

MAX_ATTEMPTS = 3
"""How many times one `run` will read, evaluate and try to write before handing the conflict back.

Three because a conflict is another commit landing inside a window measured in milliseconds, so
losing it twice running means the table is genuinely hot rather than that we were unlucky — and at
that point spinning is a livelock dressed up as a slow success. The bound is about liveness, not
correctness: every attempt is as safe as the first, and the caller still gets a retryable failure
and can decide with fresher information than the runtime has."""


class ActionError(RuntimeError):
    """The caller asked for something the ontology doesn't define — an action that isn't there, a
    catalog that isn't bound. Distinct from a `Failure`, which is an action that ran and refused."""


@dataclass
class ActionRuntime:
    """Runs the actions of one ontology against one set of catalogs.

    The entry point for both `loom run` and `run_<action>`. There is deliberately only one: a dev
    command that could do something the generated tool cannot would be a back door into the write
    path, which is the argument that put `loom query` under the same rule on the read side. It is
    also why `status` gates neither caller — a runtime that refused a deprecated action would make
    the two disagree in the other direction, so the tool surface labels rather than hides."""

    ontology: Ontology
    catalogs: Mapping[str, Catalog]

    def run(
        self,
        action_name: str,
        parameters: Mapping[str, Any],
        *,
        actor: str | None = None,
        dry_run: bool = False,
    ) -> ActionResult:
        """One action, up to `MAX_ATTEMPTS` times, recorded once, and the last word either way.

        `actor` is per call rather than per runtime because `loom serve` is long-lived and a caller
        is not. It is not defaulted here — see the module docstring and `log.UNKNOWN_ACTOR`.

        A conflict is retried **here** rather than handed straight back, because the check it comes
        from is deliberately coarse: it asserts the whole table's snapshot, so every unrelated
        append to a busy table refuses a run that would have been perfectly correct. Those two are
        one decision, not two — a table-level check is only defensible if something absorbs the
        conflicts it invents, and pushing that onto every caller means every caller writing the same
        retry loop, badly, including the ones that are language models.

        Each attempt is a **fresh** `_Run`: the row is read again, every rule is evaluated again
        against the row that is actually about to be written over, and every effect expression is
        evaluated again. Nothing is replayed. Replaying the first attempt's decision would write
        values computed against a row that no longer exists and freeze a `now()` at the moment of a
        read that lost — the same reason `loom run` re-runs its preview instead of recording it.

        The consequence worth stating plainly: a retry can succeed against a row the caller never
        saw. What makes that sound rather than sly is that the spec's `validation` rules *are* the
        caller's statement of which states it is willing to act on, and they are re-checked against
        the newer row — a stricter test than the caller's own stale read could apply. Where the
        competing write genuinely invalidates the action, the retry does not paper over it: it comes
        back as `validation_failed` or `object_not_found`, the real reason, rather than as a
        conflict the caller is invited to retry forever. And the result says how many attempts it
        took, because "applied" after three internal re-reads is a different fact from "applied"."""
        action = self.ontology.actions.get(action_name)
        if action is None:
            known = ", ".join(sorted(self.ontology.actions)) or "none"
            raise ActionError(f"unknown action '{action_name}' (known: {known})")
        target = self.ontology.object_types[action.target_object_type]

        # Minted before the first attempt, not after the last: the write has to be able to stamp it
        # into its own commit, and every attempt of one run carries the same one because they are one
        # edit that took several tries rather than several edits.
        edit_id = new_edit_id()
        who = actor or UNKNOWN_ACTOR

        result = None
        run = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            run = _Run(self, action, target, dry_run, attempt=attempt, edit_id=edit_id, actor=who)
            result = run.execute(parameters)
            if not run.conflicted:
                break
        assert result is not None and run is not None  # the loop runs at least once
        if dry_run:
            # A preview writes nothing and holds nothing, so there is no edit to record and no id to
            # cite. `loom run` previews before every real run; logging them would double the table.
            return result
        return self._record(run, result, target, edit_id, who)

    def preview(self, action_name: str, parameters: Mapping[str, Any]) -> ActionResult:
        """Everything but the write. The write path's `loom plan`, and nearly free, because a
        refusal already had to change nothing."""
        return self.run(action_name, parameters, dry_run=True)

    def _record(
        self, run: _Run, result: ActionResult, target: ObjectType, edit_id: str, actor: str
    ) -> ActionResult:
        """One row in `_loom_meta.edits`, after the fact.

        **Once per run, not once per attempt.** The losing attempts wrote nothing, so they are not
        edits; they are one edit that took three tries, and `attempts` on the row says so. The states
        they lost to are not this run's to describe either — if the competing writer came through
        Loom, its own record is already in this table, and if it did not, Loom could not describe it
        honestly anyway. The alternative leaves a log in which most rows describe things that did not
        happen.

        **Only once the run named a row.** `run.addressed` is the gate: a `missing_parameter` or an
        unparseable key never reached an object, so its record would carry no key and answer no audit
        question. That is a *request* log — it belongs at the serve boundary, not in a table called
        `edits`.

        **After the write, never before.** Both orderings lose something, and this one loses the
        thing that can be recovered. Log-then-write records intentions that may not have happened,
        which makes every row in the table suspect; write-then-log loses the records of writes that
        succeeded, which would be worse if the record were the only copy — and it is not. The row
        write stamped `loom.edit_id` into its own Iceberg commit, so a crash in this gap leaves a
        stamped snapshot with no matching row: a gap a reader can *find*, rather than silence. That
        is also what makes `failed` answerable for the first time. The guarantee is asymmetric and
        worth saying so: a lost record of a *refusal* is not detectable, because a refusal leaves
        nothing in the lake to stamp. That is inherent to refusing, not a defect in the log.

        **A failed append does not fail the action.** By the time this runs the row has committed;
        returning `failed` would tell a caller to retry a delete that already happened. It comes back
        as a non-retryable `log_failed` beside an otherwise unchanged status — and the same for a
        catalog that implements no `EditLogWriter`, because making the log a precondition of the
        write is a policy ("no log, no write") that belongs in M5 with the other policies, not wired
        in here where nobody can turn it off."""
        if not run.addressed:
            return result
        entry = EditRecord(
            edit_id=edit_id,
            recorded_at=now(),
            actor=actor,
            action=result.action,
            object_type=result.object_type,
            operation=result.operation,
            catalog=target.backing_catalog,
            table_name=target.backing_table,
            object_key=render_key(result.key),
            status=result.status,
            attempts=result.attempts,
            read_snapshot_id=result.read_snapshot_id,
            parameters=run.bound,
            before=result.before,
            after=result.after,
            failures=[f.as_json() for f in result.failures],
        )
        result = replace(result, edit_id=edit_id)
        try:
            catalog = self.catalog_for(target)
            EditLog(catalog=catalog, writer=edit_log_writer_for(catalog)).record(entry)
        except CatalogError as e:
            return replace(
                result,
                failures=(
                    *result.failures,
                    Failure(
                        code=LOG_FAILED,
                        message=f"the run was not recorded in the edit log: {e}",
                        detail={"editId": edit_id, "status": result.status},
                    ),
                ),
            )
        return result

    def catalog_for(self, obj: ObjectType) -> Catalog:
        catalog = self.catalogs.get(obj.backing_catalog)
        if catalog is None:
            raise ActionError(
                f"objectType '{obj.api_name}' is backed by catalog '{obj.backing_catalog}', "
                f"which is not declared in loom.yaml"
            )
        return catalog


@dataclass
class _Run:
    """One execution. Holds the failures as they accumulate, so every step can add to the list and
    exactly one place decides whether the write happens."""

    rt: ActionRuntime
    action: Action
    target: ObjectType
    dry_run: bool
    attempt: int = 1
    edit_id: str = ""
    actor: str = UNKNOWN_ACTOR
    failures: list[Failure] = field(default_factory=list)
    conflicted: bool = False
    bound: dict[str, Any] | None = None
    """The parameters as coerced, for the log. `None` until `_bind` has run and produced a key."""
    addressed: bool = False
    """Whether this run got as far as naming a row. The edit log's gate — see `_record`.

    Distinct from `key is not None`, which cannot be the test: `None` is a key a nullable primary-key
    column can genuinely hold, and `_result` has already flattened `_ABSENT` into it by then."""

    # ---- the four steps --------------------------------------------------------

    def execute(self, supplied: Mapping[str, Any]) -> ActionResult:
        catalog = self.rt.catalog_for(self.target)
        table, pk = self.target.backing_table, self.target.pk_property
        creating = self.action.operation == "create"

        # Each step short-circuits on failure rather than pushing on, because a later step's
        # complaint about the wreckage of an earlier one is noise: an unbound parameter makes every
        # expression below it meaningless, and "there is no current object" is not a useful second
        # sentence after "no Customer with customerId 'c9'".
        params = self._bind(supplied)
        if self.failures:
            return self._result(REFUSED)

        key = self._key(params)
        if key is _ABSENT:
            return self._result(REFUSED)
        # From here the run has named an object, which is what makes it an attempted *edit* rather
        # than a malformed call — and therefore what makes it worth a row in the log.
        self.addressed = True
        self.bound = params

        # The snapshot *before* the rows, not after: it makes the recorded id at-or-before the data
        # the rules were evaluated against, so the check can report a conflict that wasn't one but
        # can never miss one that was. The other order silently blesses a lost update. Those false
        # conflicts are the price of that order, paid deliberately and absorbed by the retry in
        # `ActionRuntime.run` — they are a consequence of the decision, not a defect in it.
        snapshot = catalog.current_snapshot_id(table) if catalog.table_exists(table) else None
        row = self._read(catalog, table, pk.column, key)
        before = self._project(row)
        if self.failures:  # an ambiguous key — the read itself contradicted the spec
            return self._result(REFUSED, key=key, snapshot=snapshot)
        if creating and row is not None:
            self._fail(OBJECT_EXISTS, f"a {self.target.api_name} with {pk.name} {key!r} already exists",
                       {"key": key})
            return self._result(REFUSED, key=key, before=before, snapshot=snapshot)
        if not creating and row is None:
            self._fail(OBJECT_NOT_FOUND, f"no {self.target.api_name} with {pk.name} {key!r}", {"key": key})
            return self._result(REFUSED, key=key, snapshot=snapshot)

        scope = Scope(parameters=params, object_row=None if creating else before)
        self._validate(scope)
        values = self._effect_values(scope)
        if self.failures:
            return self._result(REFUSED, key=key, before=before, snapshot=snapshot)

        after = self._after(before, values, key)
        if self.dry_run:
            return self._result(PREVIEWED, key=key, before=before, after=after, snapshot=snapshot)

        try:
            self._write(catalog, table, pk.column, key, row, values, snapshot)
        except ConcurrencyError as e:
            # `REFUSED`, not `FAILED`. The write was declined before it committed, so this is a run
            # that changed nothing — the same promise every other refusal makes, and the reason a
            # caller can retry it without first working out what landed.
            self._conflict(catalog, table, pk.column, key, before, e)
            return self._result(REFUSED, key=key, before=before, snapshot=snapshot)
        except CatalogError as e:
            self._fail(WRITE_FAILED, str(e))
            return self._result(FAILED, key=key, before=before, snapshot=snapshot)
        return self._result(APPLIED, key=key, before=before, after=after, snapshot=snapshot)

    # ---- 1. bind ---------------------------------------------------------------

    def _bind(self, supplied: Mapping[str, Any]) -> dict[str, Any]:
        """Caller values to declared types. The same coercion the read path uses, because "an LLM
        sent `'42'` for a long" is the same problem in both directions and two implementations
        would be two answers to it."""
        declared = self.action.parameters
        for name in supplied:
            if name not in declared:
                known = ", ".join(declared) or "none"
                self._fail(UNKNOWN_PARAMETER, f"action '{self.action.api_name}' has no parameter "
                                              f"'{name}' (declared: {known})", {"parameter": name})
        bound: dict[str, Any] = {}
        for name, param in declared.items():
            raw = supplied.get(name, param.default)
            if raw is None:
                if param.required:
                    self._fail(MISSING_PARAMETER, f"parameter '{name}' is required", {"parameter": name})
                bound[name] = None
                continue
            try:
                bound[name] = coerce_value(
                    param.type, raw, self.rt.ontology.object_types, f"parameter '{name}'"
                )
            except ValueError as e:
                self._fail(TYPE_ERROR, str(e), {"parameter": name})
                bound[name] = None
        return bound

    def _key(self, params: Mapping[str, Any]) -> Any:
        """The primary key this run addresses: the effect's `key` expression for modify/delete, and
        the value `set` gives the PK property for create. `_ABSENT` when it can't be worked out,
        which is a failure already recorded."""
        pk = self.target.pk_property
        expr = self.action.effect.key
        if expr is None:
            expr = self.action.effect.set_values.get(pk.name)
            if expr is None:  # pragma: no cover - the validator requires create to set the PK
                return _ABSENT
        try:
            return coerce_value(
                pk.type, evaluate(expr, Scope(parameters=params)), self.rt.ontology.object_types,
                f"key of {self.target.api_name}",
            )
        except EvalError as e:
            self._fail(EXPRESSION_ERROR, f"could not evaluate the key '{expr.raw}': {e}", {"expression": expr.raw})
        except ValueError as e:
            self._fail(TYPE_ERROR, str(e))
        return _ABSENT

    # ---- 2. read ---------------------------------------------------------------

    def _read(self, catalog: Catalog, table: str, key_column: str, key: Any) -> dict[str, Any] | None:
        """The target row, **whole and physical** — every column, including the ones no property
        maps.

        That is the point of it, and the reason it can't go through the resolver. A modify is an
        equality-delete plus an append, which rewrites the row entirely, so a column the ontology
        never declared has to be carried across or it is silently nulled. Those are the same
        columns `loom plan` reports as unmanaged and politely leaves alone; this is that rule one
        level down, where the data lives rather than the schema.

        It doubles as the read the rules need — `newTier != object.tier` needs the row — so there is
        exactly one read, not two.
        """
        if not catalog.table_exists(table):
            return None
        rows = catalog.scan(table, predicates=[(key_column, key)]).to_pylist()
        # `predicates` is documented as a pushdown *hint*, so the filter is applied again here
        # rather than trusted. On the read path a missed filter returns extra rows; here it would
        # delete one.
        rows = [r for r in rows if r.get(key_column) == key]
        if len(rows) > 1:
            # The read path already refuses this (`Resolver.get`). Refusing it on the write path
            # matters more: an equality-delete on a key matching two rows removes both and appends
            # one. Loom cannot repair the table — it can only decline to make it worse.
            self._fail(
                AMBIGUOUS_KEY,
                f"{self.target.primary_key} {key!r} matches {len(rows)} rows in '{table}' — the "
                f"backing table violates the uniqueness the spec declares",
                {"key": key, "matched": len(rows)},
            )
            return None
        return rows[0] if rows else None

    def _project(self, row: Mapping[str, Any] | None) -> dict[str, Any] | None:
        """The physical row as the ontology sees it: declared properties, by property name.

        The unmapped columns stay behind. They are carried across the write (that is what `row`
        itself is for) but they are not the ontology's to show, and reporting them would leak
        somebody else's data past a governance layer that does not exist yet."""
        if row is None:
            return None
        return {name: row.get(prop.column) for name, prop in self.target.properties.items()}

    # ---- 3. evaluate -----------------------------------------------------------

    def _validate(self, scope: Scope) -> None:
        """Every rule, not the first failing one — the same accumulate-everything bargain the spec
        validator makes with an author. A rule that cannot be evaluated at all is its own failure
        and does not hide the honest ones beside it."""
        for rule in self.action.validation:
            try:
                outcome = evaluate(rule.expr, scope)
            except EvalError as e:
                self._fail(EXPRESSION_ERROR, f"rule '{rule.raw}' could not be evaluated: {e}",
                           {"rule": rule.raw})
                continue
            if not isinstance(outcome, bool):  # pragma: no cover - the validator types rules offline
                self._fail(EXPRESSION_ERROR, f"rule '{rule.raw}' did not evaluate to a boolean",
                           {"rule": rule.raw})
            elif not outcome:
                # The spec author's own sentence, verbatim. The runtime has nothing better to say
                # about a domain rule it knows nothing about.
                self._fail(VALIDATION_FAILED, rule.message, {"rule": rule.raw})

    def _effect_values(self, scope: Scope) -> dict[str, Any]:
        """Each `set` expression evaluated and coerced to its property's declared type.

        Coerced, not just evaluated: `now()` gives a `datetime` that a `date` property stores as a
        date, `'1299.99'` becomes a `Decimal` checked against the declared precision, and a
        fractional number destined for a `long` is refused rather than truncated."""
        values: dict[str, Any] = {}
        for name, expr in self.action.effect.set_values.items():
            prop = self.target.properties[name]
            try:
                value = coerce_value(
                    prop.type, evaluate(expr, scope), self.rt.ontology.object_types,
                    f"property '{name}'",
                )
            except EvalError as e:
                self._fail(EXPRESSION_ERROR, f"could not evaluate '{expr.raw}' for property "
                                             f"'{name}': {e}", {"property": name, "expression": expr.raw})
                continue
            except ValueError as e:
                self._fail(TYPE_ERROR, str(e), {"property": name})
                continue
            if value is None and not prop.nullable:
                # Caught here rather than left to the storage layer, whose complaint would name a
                # column and a required-ness rather than the expression that produced the null.
                self._fail(TYPE_ERROR, f"property '{name}' is not nullable, but '{expr.raw}' "
                                       f"evaluated to null", {"property": name})
                continue
            values[name] = value
        return values

    def _after(self, before: Mapping[str, Any] | None, values: Mapping[str, Any], key: Any) -> dict | None:
        if self.action.effect.op == "deleteObject":
            return None
        base = dict(before) if before is not None else {n: None for n in self.target.properties}
        base.update(values)
        base[self.target.primary_key] = key
        return base

    # ---- 4. write --------------------------------------------------------------

    def _write(
        self,
        catalog: Catalog,
        table: str,
        key_column: str,
        key: Any,
        row: Mapping[str, Any] | None,
        values: Mapping[str, Any],
        snapshot: int | None,
    ) -> None:
        """One verb, one transaction, and the snapshot the read saw carried into it.

        The writer is asked for here rather than held, so this runtime keeps no row-writable typed
        reference between actions and the plane it is asking for is visible at the call site. What
        that does *not* claim, now that a serving process can reach this method: that the process
        holds nothing capable of row writes. It holds catalogs, and a real catalog implements every
        port. The claim that survives is the one above the class — no `CatalogWriter`, ever.

        **All three verbs are checked, and each earns it separately.**

        `modify` is the obvious one, and its reason is the carry-across two methods up: the write
        puts back every column the ontology never mapped, using values read before the competing
        commit. Committing anyway would not merely lose a race — it would take somebody else's newer
        value and quietly restore the old one, using a column Loom deliberately refuses to look at.
        Which settles the question that rule raises: a change to an unmapped column *is* a conflict,
        and the reason sits beside the never-inspect rule rather than qualifying it. Loom does not
        read that column and does not overwrite it blind; the snapshot check is how it manages the
        second without doing the first, since it compares no columns at all.

        `create` earns it because it has a read too — the primary-key existence check — and two
        concurrent creates on one key both pass it. Both would then append, manufacturing exactly
        the duplicate row `_read` refuses as `ambiguous_key` every time it meets one afterwards, and
        which Loom can never repair. Checked, they cannot: both read the same snapshot, so only one
        commit can land on it. That check is what finally makes the existence check mean something
        — for writers coming through Loom. It still guarantees nothing about a writer that isn't,
        which is why `ambiguous_key` stays.

        `delete` is where the argument is supposed to cut the other way — the row is gone either
        way, so refusing because it changed first refuses something that has already effectively
        happened. That holds only if the competing write was also a delete. If it was a `modify`,
        the row is *not* gone: it changed, and deleting it destroys a state the caller never saw, in
        the one operation nothing can put back. A conflicting modify can be re-applied and a
        conflicting create refuses cleanly; a delete that lost a race is simply gone. So it is
        checked on the strongest reason of the three — and the objection gets the outcome it wanted
        anyway, because when the competing write really was a delete, the retry re-reads, finds
        nothing, and returns `object_not_found`. That is "it has already happened", said accurately,
        rather than a delete reporting that it did work it did not do.

        **And every verb carries this run's identity into the commit it produces.** The edit log is a
        second commit and Iceberg has no transaction spanning two tables, so the stamp is the only
        record of this write that cannot be separated from it. It is what turns a lost log row from
        silence into a detectable gap, and it is why the log can be written afterwards at all."""
        writer: RowWriter = row_writer_for(catalog)
        stamp = commit_properties(self.edit_id, self.action.api_name, self.actor)
        op = self.action.effect.op
        if op == "deleteObject":
            writer.delete_row(
                table, key_column, key, expect_snapshot_id=snapshot, commit_properties=stamp
            )
            return
        if op == "createObject":
            writer.insert_row(
                table,
                self._columns({}, values),
                expect_snapshot_id=snapshot,
                commit_properties=stamp,
            )
            return
        assert row is not None  # OBJECT_NOT_FOUND refused the run otherwise
        # `row` first, then the effect's columns over the top: every column the ontology does not
        # map survives the rewrite exactly as it was read.
        writer.replace_row(
            table,
            key_column,
            key,
            self._columns(row, values),
            expect_snapshot_id=snapshot,
            commit_properties=stamp,
        )

    def _columns(self, row: Mapping[str, Any], values: Mapping[str, Any]) -> dict[str, Any]:
        """Property-named values back onto physical columns, over the row that was read.

        For `create` the row is empty, so the result names only the columns the effect set; the
        storage layer fills the rest from the table's own schema. For `modify` the row is the whole
        physical row, unmapped columns and all, and the values land on top of it."""
        out = dict(row)
        for name, value in values.items():
            out[self.target.properties[name].column] = value
        return out

    # ---- conflict --------------------------------------------------------------

    def _conflict(
        self,
        catalog: Catalog,
        table: str,
        key_column: str,
        key: Any,
        before: Mapping[str, Any] | None,
        exc: ConcurrencyError,
    ) -> None:
        """The one retryable failure, carrying enough to decide whether retrying is the point.

        "Conflict, retry" on its own is the failure mode: an agent told only that will retry until
        it runs out of patience against a table that is merely busy, and will give up just as
        readily on the one case where its intent has genuinely been overtaken. So the detail answers
        the question behind the retry — *did the thing I was about change?* — rather than only
        reporting that something did:

        - `expectedSnapshotId` / `foundSnapshotId` — the table version the rules were evaluated
          against, and the one it is at now. `found` is advisory: it is read after the refusal, so
          on a hot table it may already be past the commit that actually won.
        - `attempts` — how many times this run read and tried before giving the conflict back.
        - `changed` — the **declared properties** whose value moved under this run, `null` when the
          row could not be re-read to tell (or when there was no prior row at all, as for a
          `create`). Diffed through the same projection `before` and `after` use, so the columns no
          property maps are not compared any more than they are reported. An empty list is a real
          answer, and the most common one: the table moved and this row did not.
        - `contended` — whether any of `changed` is a property this action reads in a rule or writes
          in an effect. That is the difference between a race worth re-examining and a queue.

        It is also the shape the edit log will want — expected, found, and what moved — which is why
        it is settled here rather than migrated into later."""
        changed = self._changed(catalog, table, key_column, key, before)
        in_play = self._properties_in_play()
        contested = sorted(set(changed) & in_play) if changed else []
        self._fail(
            CONFLICT,
            self._conflict_message(key, changed, contested),
            {
                "table": table,
                "expectedSnapshotId": exc.expected,
                "foundSnapshotId": exc.found,
                "attempts": self.attempt,
                "changed": changed,
                "contended": bool(contested),
            },
        )
        self.conflicted = True

    def _conflict_message(self, key: Any, changed: list[str] | None, contested: list[str]) -> str:
        head = (
            f"{self.target.api_name} {key!r} could not be written: the table moved between the read "
            f"and the write, after {self.attempt} attempt{'s' if self.attempt != 1 else ''}"
        )
        if contested:
            return f"{head} — {', '.join(contested)} changed under it"
        if changed:
            return (
                f"{head} — {', '.join(changed)} changed, but nothing this action reads or writes did"
            )
        if changed == []:
            return f"{head} — no declared property of this row changed; the table is simply busy"
        return f"{head} — the row could not be re-read to say what changed"

    def _changed(
        self,
        catalog: Catalog,
        table: str,
        key_column: str,
        key: Any,
        before: Mapping[str, Any] | None,
    ) -> list[str] | None:
        """Declared properties that differ between what this attempt read and what is there now.

        Deliberately not a row comparison: it runs *after* the refusal, as a diagnosis, and never as
        the check. A comparison could not be the check — Iceberg's commit protocol can assert a
        ref's snapshot and nothing finer, so a row-level test is unavoidably a compare-then-write
        with a window between the two, which is the guarantee this slice exists to close. What is
        too weak to decide with is still useful to explain with."""
        if before is None:
            return None
        try:
            rows = catalog.scan(table, predicates=[(key_column, key)]).to_pylist()
        except CatalogError:  # pragma: no cover - the table was readable moments ago
            return None
        rows = [r for r in rows if r.get(key_column) == key]
        if len(rows) != 1:
            return None  # gone, or doubled — neither is a property-level answer
        now = self._project(rows[0]) or {}
        return sorted(name for name, was in before.items() if was != now.get(name))

    def _properties_in_play(self) -> set[str]:
        """The declared properties this action reads in a rule or writes in an effect.

        `object.<prop>` in a validation rule or in an effect value is the action saying that
        property is part of its reasoning; a `set` key is it saying the property is part of its
        outcome. Anything else on the row is a neighbour."""
        names = set(self.action.effect.set_values)
        exprs = [rule.expr for rule in self.action.validation]
        exprs += list(self.action.effect.set_values.values())
        if self.action.effect.key is not None:
            exprs.append(self.action.effect.key)
        for expr in exprs:
            names.update(
                ref.path[1] for ref in expr.refs() if len(ref.path) == 2 and ref.path[0] == "object"
            )
        return names & set(self.target.properties)

    # ---- result ----------------------------------------------------------------

    def _fail(self, code: str, message: str, detail: Mapping[str, Any] | None = None) -> None:
        self.failures.append(Failure(code=code, message=message, detail=dict(detail or {})))

    def _result(
        self,
        status: str,
        key: Any = None,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        snapshot: int | None = None,
    ) -> ActionResult:
        return ActionResult(
            action=self.action.api_name,
            object_type=self.target.api_name,
            operation=self.action.operation,
            status=status,
            key=None if key is _ABSENT else key,
            before=before,
            after=after,
            read_snapshot_id=snapshot,
            attempts=self.attempt,
            failures=tuple(self.failures),
        )


class _Absent:
    """A key that could not be computed. Distinct from `None`, which is a key a nullable column
    could genuinely hold."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<absent>"


_ABSENT = _Absent()


def build_runtime(ontology: Ontology, config, catalogs: Mapping[str, Any] | None = None) -> ActionRuntime:
    """Wire an ontology to the catalogs named in a project config. Mirrors `build_resolver`, and
    notably takes no engine: writes bypass the compute engine entirely, which is what keeps the
    write path identical across DuckDB, Trino and Spark."""
    from ..catalog import open_catalogs

    return ActionRuntime(
        ontology=ontology, catalogs=catalogs if catalogs is not None else open_catalogs(config)
    )
