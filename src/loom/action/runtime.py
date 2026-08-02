"""The action runtime — one declared action, run against one row.

The whole of an action is four steps, in this order and no other:

1. **Bind.** Caller-supplied parameters are coerced to their declared types by the same function
   the read path coerces with (`model.coerce_value`), defaults are applied, and a missing required
   parameter is a failure rather than a `None` that surfaces three steps later.
2. **Read.** For `modify`/`delete`, the target row — *all* of it, physically. See `_read`.
3. **Evaluate.** Every validation rule, against the bound parameters and the row just read. All of
   them, so a caller sees every precondition it failed rather than one per attempt.
4. **Write.** One call to one `RowWriter` verb, which is one Iceberg transaction.

Everything that can refuse happens in 1-3, so **a run that refuses changes nothing** — the same
promise `loom apply` makes, and for the same reason: a half-done write leaves a row that neither
the caller nor the spec describes.

Two boundaries this file is careful about:

- **It does not go through the resolver.** The resolver projects a row down to declared properties,
  which is exactly the set a modify must *not* be limited to — see `_read`. The resolver stays the
  semantic read (projected, paged, and where governance will live); this is a physical read of one
  row by key.
- **It holds a `RowWriter`, never a `CatalogWriter`.** It cannot alter a schema, because the port
  it asks for has no verb for it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..catalog.base import Catalog, CatalogError, RowWriter, row_writer_for
from ..model import Action, ObjectType, Ontology, coerce_value
from .evaluate import EvalError, Scope, evaluate
from .result import (
    AMBIGUOUS_KEY,
    APPLIED,
    EXPRESSION_ERROR,
    FAILED,
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


class ActionError(RuntimeError):
    """The caller asked for something the ontology doesn't define — an action that isn't there, a
    catalog that isn't bound. Distinct from a `Failure`, which is an action that ran and refused."""


@dataclass
class ActionRuntime:
    """Runs the actions of one ontology against one set of catalogs.

    The entry point for both `loom run` and (in M4) `run_<action>`. There is deliberately only one:
    a dev command that could do something the generated tool cannot would be a back door into the
    write path, which is the argument that put `loom query` under the same rule on the read side."""

    ontology: Ontology
    catalogs: Mapping[str, Catalog]

    def run(
        self, action_name: str, parameters: Mapping[str, Any], *, dry_run: bool = False
    ) -> ActionResult:
        action = self.ontology.actions.get(action_name)
        if action is None:
            known = ", ".join(sorted(self.ontology.actions)) or "none"
            raise ActionError(f"unknown action '{action_name}' (known: {known})")
        target = self.ontology.object_types[action.target_object_type]
        return _Run(self, action, target, dry_run).execute(parameters)

    def preview(self, action_name: str, parameters: Mapping[str, Any]) -> ActionResult:
        """Everything but the write. The write path's `loom plan`, and nearly free, because a
        refusal already had to change nothing."""
        return self.run(action_name, parameters, dry_run=True)

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
    failures: list[Failure] = field(default_factory=list)

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

        # The snapshot *before* the rows, not after: it makes the recorded id at-or-before the data
        # the rules were evaluated against, so the concurrency slice's check can report a conflict
        # that wasn't one but can never miss one that was. The other order silently blesses a lost
        # update.
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
            self._write(catalog, table, pk.column, key, row, values)
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
    ) -> None:
        """One verb, one transaction. The writer is asked for here rather than held, so nothing in
        this process keeps a row-writable handle between actions."""
        writer: RowWriter = row_writer_for(catalog)
        op = self.action.effect.op
        if op == "deleteObject":
            writer.delete_row(table, key_column, key)
            return
        if op == "createObject":
            writer.insert_row(table, self._columns({}, values))
            return
        assert row is not None  # OBJECT_NOT_FOUND refused the run otherwise
        # `row` first, then the effect's columns over the top: every column the ontology does not
        # map survives the rewrite exactly as it was read.
        writer.replace_row(table, key_column, key, self._columns(row, values))

    def _columns(self, row: Mapping[str, Any], values: Mapping[str, Any]) -> dict[str, Any]:
        """Property-named values back onto physical columns, over the row that was read.

        For `create` the row is empty, so the result names only the columns the effect set; the
        storage layer fills the rest from the table's own schema. For `modify` the row is the whole
        physical row, unmapped columns and all, and the values land on top of it."""
        out = dict(row)
        for name, value in values.items():
            out[self.target.properties[name].column] = value
        return out

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
