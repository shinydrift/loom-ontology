"""DuckDB adapter — the first `Engine`.

Reads Iceberg through pyiceberg rather than DuckDB's Iceberg extension: `compile()` emits real
DuckDB SQL against alias-named relations, and `execute()` binds each alias by handing the
plan's `ScanRequest` to the catalog and registering the resulting Arrow table. That keeps the
adapter honest about dialect lowering while needing no runtime extension install, and it means
the same `ScanRequest` pushdown works for any catalog implementation.

`compile()` is pure, so the generated SQL is asserted directly in tests.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..engine import Capabilities, CompiledQuery, EngineError, ScanRequest
from ..ir import (
    And,
    ColumnRef,
    Compare,
    Const,
    Contains,
    Eq,
    GetByKey,
    Not,
    Or,
    Predicate,
    Project,
    Search,
    TableRef,
    Traverse,
    predicate_columns,
    tables_of,
)

# DuckDB's LIKE metacharacters. A user searching for "50%" means the literal characters, so the
# value is escaped and the pattern declares an ESCAPE clause.
_LIKE_ESCAPE = "\\"


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _ref(alias: str, column: str) -> str:
    return f"{_quote(alias)}.{_quote(column)}"


def _escape_like(value: str) -> str:
    out = value.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
    for meta in ("%", "_"):
        out = out.replace(meta, _LIKE_ESCAPE + meta)
    return out


@dataclass
class DuckDBEngine:
    catalogs: Mapping[str, Any]
    options: Mapping[str, object] = field(default_factory=dict)
    _con: Any = field(default=None, repr=False)

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="duckdb",
            joins=True,
            offset=True,
            case_insensitive_like=True,
            native_merge=False,  # writes go through the Iceberg catalog, not DuckDB
        )

    # ---- compile ---------------------------------------------------------------

    def compile(self, plan: Project) -> CompiledQuery:
        if not isinstance(plan, Project):
            raise EngineError(f"a plan must be rooted in Project, got {type(plan).__name__}")
        if not plan.columns:
            raise EngineError("Project has no columns")

        select = ", ".join(f"{_ref(c.alias, c.column)} AS {_quote(c.output)}" for c in plan.columns)
        src = plan.source
        if isinstance(src, GetByKey):
            frm, where, params, scans = self._compile_get(src, plan)
            tail, tail_params = " LIMIT 2", ()
        elif isinstance(src, Search):
            frm, where, params, scans = self._compile_search(src, plan)
            tail, tail_params = self._order_and_page(src.table.alias, src.order_by, src.limit, src.offset)
        elif isinstance(src, Traverse):
            frm, where, params, scans = self._compile_traverse(src, plan)
            tail, tail_params = self._order_and_page(src.to_table.alias, src.order_by, src.limit, src.offset)
        else:
            raise EngineError(f"unsupported source node {type(src).__name__}")

        # Every table the plan reads, not the one it projects: a traverse's anchor end is governed
        # too, or the link is the way around a policy on the type you cannot search. `tables_of` is
        # what makes that structural rather than remembered — see `ir.TableRef`.
        governed, governed_params = self._governance(tables_of(src))
        clauses = [c for c in (where, governed) if c]

        sql = f"SELECT {select} FROM {frm}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += tail
        return CompiledQuery(
            sql=sql,
            params=tuple(params) + tuple(governed_params) + tuple(tail_params),
            scans=scans,
        )

    def _compile_get(self, src: GetByKey, plan: Project):
        """LIMIT 2, not 1 (applied by `compile`): the resolver needs to *see* a duplicate primary
        key in the data rather than silently returning whichever row happened to come first."""
        alias = src.table.alias
        columns = self._columns_for(plan, src, alias) | {src.key_column}
        scans = (
            ScanRequest(
                alias=alias,
                catalog=src.table.catalog,
                table=src.table.table,
                columns=tuple(sorted(columns)),
                predicates=((src.key_column, src.key_value),),
                limit=2,
            ),
        )
        return _quote(alias), f"{_ref(alias, src.key_column)} = ?", [src.key_value], scans

    def _compile_search(self, src: Search, plan: Project):
        alias = src.table.alias
        frm = _quote(alias)
        clauses: list[str] = []
        params: list[Any] = []
        pushdown: list[tuple[str, Any]] = []
        referenced = self._columns_for(plan, src, alias) | set(src.order_by)

        for f in src.filters:
            referenced.add(f.column)
            if isinstance(f, Eq):
                if f.value is None:
                    clauses.append(f"{_ref(f.alias, f.column)} IS NULL")
                else:
                    clauses.append(f"{_ref(f.alias, f.column)} = ?")
                    params.append(f.value)
                pushdown.append((f.column, f.value))
            elif isinstance(f, Contains):
                clauses.append(f"{_ref(f.alias, f.column)} ILIKE ? ESCAPE '{_LIKE_ESCAPE}'")
                params.append(f"%{_escape_like(f.value)}%")
            else:
                raise EngineError(f"unsupported filter {type(f).__name__}")

        scans = (
            ScanRequest(
                alias=alias,
                catalog=src.table.catalog,
                table=src.table.table,
                columns=tuple(sorted(referenced)),
                predicates=tuple(pushdown),
            ),
        )
        return frm, " AND ".join(clauses), params, scans

    def _compile_traverse(self, src: Traverse, plan: Project):
        to_alias, from_alias = src.to_table.alias, src.from_table.alias
        to_cols = self._columns_for(plan, src, to_alias) | {src.to_column} | set(src.order_by)
        from_cols = {src.from_column, src.anchor.column} | self._columns_for(plan, src, from_alias)

        if src.through is None:
            join = (
                f"{_quote(to_alias)} JOIN {_quote(from_alias)} "
                f"ON {_ref(to_alias, src.to_column)} = {_ref(from_alias, src.from_column)}"
            )
            extra_scans: tuple[ScanRequest, ...] = ()
        else:
            th = src.through
            join = (
                f"{_quote(to_alias)} "
                f"JOIN {_quote(th.table.alias)} ON {_ref(to_alias, src.to_column)} = {_ref(th.table.alias, th.to_column)} "
                f"JOIN {_quote(from_alias)} ON {_ref(th.table.alias, th.from_column)} = {_ref(from_alias, src.from_column)}"
            )
            extra_scans = (
                ScanRequest(
                    alias=th.table.alias,
                    catalog=th.table.catalog,
                    table=th.table.table,
                    columns=tuple(sorted({th.from_column, th.to_column})),
                ),
            )

        where = f"{_ref(from_alias, src.anchor.column)} = ?"
        scans = (
            ScanRequest(
                alias=to_alias,
                catalog=src.to_table.catalog,
                table=src.to_table.table,
                columns=tuple(sorted(to_cols)),
            ),
            ScanRequest(
                alias=from_alias,
                catalog=src.from_table.catalog,
                table=src.from_table.table,
                columns=tuple(sorted(from_cols)),
                predicates=((src.anchor.column, src.anchor.value),),
            ),
        ) + extra_scans
        return join, where, [src.anchor.value], scans

    @staticmethod
    def _columns_for(plan: Project, src, alias: str) -> set[str]:
        """The columns of one alias this query touches: what it projects, plus what a governance
        predicate reads.

        The two are not the same set, and deliberately: a policy may filter on a property it also
        masks, so a scan has to carry a column the projection never asks for."""
        projected = {c.column for c in plan.columns if c.alias == alias}
        governed = {
            column
            for table in tables_of(src)
            for a, column in predicate_columns(table.predicate)
            if a == alias
        }
        return projected | governed

    # ---- governance ------------------------------------------------------------

    def _governance(self, tables: Sequence[TableRef]) -> tuple[str, list[Any]]:
        """Every governed table's predicate, ANDed, in the order the plan names them.

        Never a `ScanRequest` predicate: that channel is a documented pushdown *hint* an adapter
        may ignore and the resolver re-applies, which is right for a caller's filter and wrong for
        a policy. A governance predicate lives in the `WHERE` clause and nowhere else, which is
        also what makes it filter *before* `LIMIT`/`OFFSET` — a page thinned after the fact would
        make `hasMore` and `offset` lie."""
        clauses: list[str] = []
        params: list[Any] = []
        for table in tables:
            if table.predicate is not None:
                clauses.append(self._predicate(table.predicate, params))
        return " AND ".join(clauses), params

    def _predicate(self, pred: Predicate, params: list[Any]) -> str:
        if isinstance(pred, And):
            return f"({self._predicate(pred.left, params)} AND {self._predicate(pred.right, params)})"
        if isinstance(pred, Or):
            return f"({self._predicate(pred.left, params)} OR {self._predicate(pred.right, params)})"
        if isinstance(pred, Not):
            return f"(NOT {self._predicate(pred.term, params)})"
        if isinstance(pred, Compare):
            return self._compare(pred, params)
        # `predicate.lower()` builds these and emits no others.
        raise EngineError(f"unsupported predicate node {type(pred).__name__}")  # pragma: no cover

    def _compare(self, cmp: Compare, params: list[Any]) -> str:
        """**`==` is not `=` here.** §5 says null is a value — `null == null` is true — so the
        equality a policy writes lowers to `IS NOT DISTINCT FROM`, which is the same statement in
        SQL's vocabulary. `=` would return unknown instead, and under a `NOT` that flips a row from
        excluded to admitted: the one place the two planes could disagree, closed at the one node
        where they disagree.

        Ordering is *not* lifted, and that is the other half of the same decision: SQL yields
        unknown for `NULL > 100` and §5 refuses to order a null, so `predicate.admits` calls it
        undecided and neither plane admits the row.

        A null literal takes the shorter spelling — `IS NULL` says exactly what `IS NOT DISTINCT
        FROM NULL` says, and needs no typed parameter for a value that has no type."""
        left, right = cmp.left, cmp.right
        if cmp.op in ("==", "!="):
            negated = "NOT " if cmp.op == "!=" else ""
            for a, b in ((left, right), (right, left)):
                if isinstance(a, Const) and a.value is None:
                    return f"{self._operand(b, params)} IS {negated}NULL"
            distinct = "IS NOT DISTINCT FROM" if cmp.op == "==" else "IS DISTINCT FROM"
            return f"{self._operand(left, params)} {distinct} {self._operand(right, params)}"
        return f"{self._operand(left, params)} {cmp.op} {self._operand(right, params)}"

    @staticmethod
    def _operand(operand: Any, params: list[Any]) -> str:
        if isinstance(operand, ColumnRef):
            return _ref(operand.alias, operand.column)
        if isinstance(operand, Const):
            params.append(operand.value)
            return "?"
        raise EngineError(f"unsupported operand {type(operand).__name__}")  # pragma: no cover

    @staticmethod
    def _order_and_page(alias: str, order_by: Sequence[str], limit: int | None, offset: int):
        sql = ""
        params: list[Any] = []
        if order_by:
            sql += " ORDER BY " + ", ".join(_ref(alias, c) for c in order_by)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        if offset:
            # DuckDB requires LIMIT before OFFSET; a page-2 request with no limit is still valid
            # SQL only if we supply one, so the resolver always sets a limit alongside an offset.
            if limit is None:
                raise EngineError("OFFSET requires a LIMIT")
            sql += " OFFSET ?"
            params.append(offset)
        return sql, params

    # ---- execute ---------------------------------------------------------------

    def execute(self, compiled: CompiledQuery) -> Sequence[dict]:
        con = self._connection()
        for scan in compiled.scans:
            catalog = self.catalogs.get(scan.catalog)
            if catalog is None:
                raise EngineError(f"catalog '{scan.catalog}' is not open")
            arrow = catalog.scan(
                scan.table,
                columns=list(scan.columns) or None,
                predicates=scan.predicates,
                limit=scan.limit,
            )
            # register() replaces any relation already bound to this alias, so every execute
            # sees a fresh snapshot of the table.
            con.register(scan.alias, arrow)
        try:
            cursor = con.execute(compiled.sql, list(compiled.params))
            names = [d[0] for d in cursor.description]
            # strict: a row that doesn't match the projection means the compiled SQL and the
            # cursor disagree, which should surface rather than silently drop a column.
            return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
        except Exception as e:
            raise EngineError(f"query failed: {e}\n  sql: {compiled.sql}") from e

    def _connection(self):
        if self._con is None:
            try:
                import duckdb
            except ImportError as e:  # pragma: no cover - packaging concern
                raise EngineError(
                    "the duckdb engine needs duckdb — install the extra: pip install 'loom-ontology[duckdb]'"
                ) from e
            self._con = duckdb.connect()
        return self._con

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None
