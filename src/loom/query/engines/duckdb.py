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
    Comparison,
    Const,
    Contains,
    GetByKey,
    In,
    LinkFilter,
    Match,
    Not,
    Or,
    Predicate,
    Project,
    Search,
    TableRef,
    Traverse,
    predicate_columns,
    pushdown_hints,
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
            # `array_cosine_similarity` over `FLOAT[n]` is core DuckDB — no extension, so this is
            # a claim about the dependency already declared rather than about one an operator has
            # to install. The `vss` extension buys an HNSW index, which is an optimisation this
            # engine does not need to make the claim true.
            vector_search=True,
            native_merge=False,  # writes go through the Iceberg catalog, not DuckDB
        )

    # ---- compile ---------------------------------------------------------------

    def compile(self, plan: Project) -> CompiledQuery:
        if not isinstance(plan, Project):
            raise EngineError(f"a plan must be rooted in Project, got {type(plan).__name__}")
        if not plan.columns:
            raise EngineError("Project has no columns")

        select = ", ".join(f"{_ref(c.alias, c.column)} AS {_quote(c.output)}" for c in plan.columns)
        # A ranked read is the one plan whose SELECT list holds something no table has, so it is the
        # one that can contribute a parameter *before* the FROM clause. Empty for the other three.
        select_extra: str = ""
        select_params: Sequence[Any] = ()
        # Aliases whose governance predicate a source compiler has already rendered somewhere the
        # top-level `WHERE` cannot reach. Empty for three of the four nodes; see `_compile_match`.
        scoped: frozenset[str] = frozenset()
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
        elif isinstance(src, Match):
            select_extra, select_params, frm, where, params, scans, scoped = self._compile_match(
                src, plan
            )
            tail, tail_params = self._order_and_page(
                src.table.alias, src.order_by, src.limit, src.offset, rank=src.score_as
            )
        else:
            raise EngineError(f"unsupported source node {type(src).__name__}")

        # Every table the plan reads, not the one it projects: a traverse's anchor end is governed
        # too, or the link is the way around a policy on the type you cannot search. `tables_of` is
        # what makes that structural rather than remembered — see `ir.TableRef`.
        known = {t.alias for t in tables_of(src)}
        if not scoped <= known:
            # A backstop, and modest on purpose — the guarantee is not here. What would be dangerous
            # is an alias reported as governed whose predicate was never emitted, since its policy
            # would then be dropped by both clauses at once; that is prevented *structurally*, by
            # `_semi_join` reporting only from the branch that renders one, rather than checked
            # after the fact. This catches the remaining shape a report can take — a claim naming no
            # table of this plan, which can only be a claim about nothing. The opposite slip needs
            # no help at all: an unreported governed table falls through to `_governance`, where its
            # alias is out of scope and DuckDB refuses the query.
            raise EngineError(  # pragma: no cover - an adapter bug, not a caller's
                f"{type(src).__name__} reports governing {sorted(scoped - known)}, which this plan "
                "does not read"
            )
        governed, governed_params = self._governance(tables_of(src), scoped)
        clauses = [c for c in (where, governed) if c]

        sql = f"SELECT {select}{select_extra} FROM {frm}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += tail
        return CompiledQuery(
            sql=sql,
            params=tuple(select_params) + tuple(params) + tuple(governed_params) + tuple(tail_params),
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
        referenced = self._columns_for(plan, src, alias) | set(src.order_by)
        clauses, params = self._filter_clauses(src.filters, referenced)

        scans = (
            ScanRequest(
                alias=alias,
                catalog=src.table.catalog,
                table=src.table.table,
                columns=tuple(sorted(referenced)),
                predicates=tuple(pushdown_hints(src.filters)),
            ),
        )
        return _quote(alias), " AND ".join(clauses), params, scans

    def _filter_clauses(self, filters: Sequence[Comparison], referenced: set[str]):
        """A caller's conjunction as SQL clauses, recording in `referenced` every column it reads.

        Shared by `Search` and `Match`, because the conjunction means the same thing in both: a
        ranked read narrows with the identical grammar, and a second copy of this loop would be
        precisely where the two quietly stopped agreeing about what `contains` or a null `eq` does.
        """
        clauses: list[str] = []
        params: list[Any] = []
        for f in filters:
            if isinstance(f, Contains):
                referenced.add(f.column)
                clauses.append(f"{_ref(f.alias, f.column)} ILIKE ? ESCAPE '{_LIKE_ESCAPE}'")
                params.append(f"%{_escape_like(f.value)}%")
            elif isinstance(f, In):
                referenced.add(f.column)
                clauses.append(self._in(f, params))
            elif isinstance(f, Compare):
                # The same lowering a governance predicate gets, because it is the same node: one
                # meaning per operator in this dialect, so `eq` on a null column cannot answer one
                # thing for a caller and another for a policy.
                referenced |= {column for _, column in predicate_columns(f)}
                clauses.append(self._compare(f, params))
            else:
                raise EngineError(f"unsupported filter {type(f).__name__}")
        return clauses, params

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

    def _compile_match(self, src: Match, plan: Project):
        """A ranked read: join the sidecar, keep the vectors that are comparable, score the rest.

        **The comparability guard is in the `WHERE` *and* in the scan, for two different reasons.**
        In the scan it is a pushdown hint — `(model, dims, property)` are equality pairs, the one
        shape that channel carries — so a catalog can prune whole files of a superseded generation
        before DuckDB sees them. In the `WHERE` it is correctness, because a hint may be ignored:
        `array_cosine_similarity` over two different widths **raises** rather than answers, so this
        is what stands between a sidecar caught mid-`--remodel` and a tool call that fails instead
        of ranking the vectors that are current.

        **The object table's scan takes no `limit`.** `LIMIT k` bounds what comes back, never what
        has to be measured: every row the filters left is a candidate until its distance is computed.

        **Neither does the sidecar's, and that one is a cost rather than a choice.** The keys that
        survive the filters are known only after the object side is scanned, and `ScanRequest`
        carries a *conjunction* of equality pairs — a key set has no spelling in it, the way a range
        has none. So the vector column is materialized whole on every call: the distance arithmetic
        is linear in the filtered set and the I/O is linear in the sidecar. What would fix it is the
        same channel the range-pushdown backlog entry describes, or partitioning the sidecar; neither
        is this slice's, and both are optimisations rather than corrections.

        **A `via` hop is a semi-join and not a JOIN, and the reason is the projection.** Joining the
        far table would duplicate the near row once per far row that matches, which on a to-many link
        is silently a different answer: the same object several times, each with the same score, and
        the page it fills is smaller than it looks. `DISTINCT` repairs that only when the projection
        is unique, and a §6.1 mask can remove the primary key from it — so the repair is a function
        of the deployment's policy, which is exactly the kind of thing this codebase refuses to
        depend on. `IN (SELECT …)` says *some* once, in the grammar's own vocabulary, and cannot
        multiply a row whatever the projection holds.

        **Each hop's governance predicate is rendered inside its own subquery, and the roadmap said
        otherwise.** *A `via` inherits cross-object governance for free* was true of where the
        predicate comes from — `Resolver._table`, the one place a type becomes a table — and not of
        where it is *placed*: `compile` ANDs every `tables_of` predicate into the top-level `WHERE`,
        where a semi-joined alias is out of scope, and the parameters concatenate slot by slot so a
        clause in one slot cannot have its parameters in another. Both halves fail together, which is
        why this returns the aliases it has already governed and `_governance` covers the rest."""
        alias, v = src.table.alias, src.vectors
        valias = v.table.alias
        dims = len(src.query)

        join = (
            f"{_quote(alias)} JOIN {_quote(valias)} "
            f"ON {_ref(alias, src.key_column)} = {_ref(valias, v.key_column)}"
        )
        # A null vector is not a distant one — it is a row nothing was ever written for. Excluded
        # here rather than ranked last, so the score column can never be null and the ordering never
        # has to have an opinion about where a null sorts.
        guard = [
            f"{_ref(valias, v.vector_column)} IS NOT NULL",
            f"{_ref(valias, v.model_column)} = ?",
            f"{_ref(valias, v.dims_column)} = ?",
            f"{_ref(valias, v.property_column)} = ?",
        ]
        params: list[Any] = [v.model, dims, v.property]

        referenced = self._columns_for(plan, src, alias) | {src.key_column} | set(src.order_by)
        clauses, filter_params = self._filter_clauses(src.filters, referenced)
        params.extend(filter_params)

        link_scans: tuple[ScanRequest, ...] = ()
        scoped: set[str] = set()
        for link in src.links:
            referenced.add(link.near_column)
            clause, link_params, hop_scans, hop_scoped = self._semi_join(link, plan, src)
            clauses.append(clause)
            params.extend(link_params)
            link_scans += hop_scans
            # Taken from what `_semi_join` reports it *rendered*, never from the link it was handed.
            # A skip list assembled here in parallel would be a second statement about the same
            # thing, and the direction it could get wrong — claiming an alias whose predicate was
            # never emitted — is the one that silently drops a policy.
            scoped |= hop_scoped

        vector_columns = self._columns_for(plan, src, valias) | {
            v.key_column,
            v.vector_column,
            v.model_column,
            v.dims_column,
            v.property_column,
        }
        scans = (
            ScanRequest(
                alias=alias,
                catalog=src.table.catalog,
                table=src.table.table,
                columns=tuple(sorted(referenced)),
                predicates=tuple(pushdown_hints(src.filters)),
            ),
            ScanRequest(
                alias=valias,
                catalog=v.table.catalog,
                table=v.table.table,
                columns=tuple(sorted(vector_columns)),
                predicates=(
                    (v.model_column, v.model),
                    (v.dims_column, dims),
                    (v.property_column, v.property),
                ),
            ),
        ) + link_scans
        # `FLOAT[n]` on both sides — the fixed-width spelling `Capabilities.vector_search` claims,
        # and the one the distance function needs. The sidecar column arrives from Iceberg as a
        # variable-length `list<float>`, so the width the ranking happens at is *stated* in the SQL
        # rather than inferred from whichever row the engine reads first.
        stored = f"CAST({_ref(valias, v.vector_column)} AS FLOAT[{dims}])"
        score = (
            f", array_cosine_similarity({stored}, CAST(? AS FLOAT[{dims}])) AS {_quote(src.score_as)}"
        )
        return (
            score,
            (list(src.query),),
            join,
            " AND ".join(guard + clauses),
            params,
            scans,
            frozenset(scoped),
        )

    def _semi_join(self, link: LinkFilter, plan: Project, src: Match):
        """One hop as `near IN (SELECT far …)`, with that hop's own governance inside it.

        The far end's rows are the rows this deployment shows — the predicate arrived on
        `link.table` from `Resolver._table` like every other one — and it is ANDed *here*, after the
        hop's filters, because this subquery is the only scope its alias exists in.

        A mapping table joins in the middle when the link declares one, and it carries no predicate
        for `ThroughRef`'s reason: it stands for no object type, so no policy names it. What the
        subquery selects then changes ends — the mapping table's near-side column rather than the far
        table's join column — because that is the column the near row's value has to be found in."""
        alias = link.table.alias
        far_columns = self._columns_for(plan, src, alias) | {link.far_column}
        clauses, params = self._filter_clauses(link.filters, far_columns)

        if link.through is None:
            frm = _quote(alias)
            selected = _ref(alias, link.far_column)
            scans: tuple[ScanRequest, ...] = ()
        else:
            th = link.through
            frm = (
                f"{_quote(th.table.alias)} JOIN {_quote(alias)} "
                f"ON {_ref(alias, link.far_column)} = {_ref(th.table.alias, th.to_column)}"
            )
            selected = _ref(th.table.alias, th.from_column)
            scans = (
                ScanRequest(
                    alias=th.table.alias,
                    catalog=th.table.catalog,
                    table=th.table.table,
                    columns=tuple(sorted({th.from_column, th.to_column})),
                ),
            )

        scoped: frozenset[str] = frozenset()
        if link.table.predicate is not None:
            clauses.append(self._predicate(link.table.predicate, params))
            # Reported from the branch that emitted it, so *governed here* and *skipped there* are
            # one fact rather than two that could disagree.
            scoped = frozenset({alias})
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        scans += (
            ScanRequest(
                alias=alias,
                catalog=link.table.catalog,
                table=link.table.table,
                columns=tuple(sorted(far_columns)),
                predicates=tuple(pushdown_hints(link.filters)),
            ),
        )
        clause = f"{_ref(src.table.alias, link.near_column)} IN (SELECT {selected} FROM {frm}{where})"
        return clause, params, scans, scoped

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

    def _governance(
        self, tables: Sequence[TableRef], scoped: frozenset[str] = frozenset()
    ) -> tuple[str, list[Any]]:
        """Every governed table's predicate, ANDed, in the order the plan names them.

        Never a `ScanRequest` predicate: that channel is a documented pushdown *hint* an adapter
        may ignore and the resolver re-applies, which is right for a caller's filter and wrong for
        a policy. A governance predicate lives in the `WHERE` clause and nowhere else, which is
        also what makes it filter *before* `LIMIT`/`OFFSET` — a page thinned after the fact would
        make `hasMore` and `offset` lie.

        `scoped` names the aliases a source compiler has already governed somewhere this clause
        cannot see. It is a skip list rather than a second rule about which tables matter: every
        table is still governed, and this says only that one of them was governed *there*. Only
        `Match` populates it, and only for a `via` hop, whose alias exists inside a subquery and
        nowhere else."""
        clauses: list[str] = []
        params: list[Any] = []
        for table in tables:
            if table.predicate is not None and table.alias not in scoped:
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
        """One comparison — a policy's or a caller's, since M7 they are the same node.

        **`==` is not `=` here.** §5 says null is a value — `null == null` is true — so the
        equality a policy writes lowers to `IS NOT DISTINCT FROM`, which is the same statement in
        SQL's vocabulary. `=` would return unknown instead, and under a `NOT` that flips a row from
        excluded to admitted: the one place the two planes could disagree, closed at the one node
        where they disagree. A caller's `eq` gets the same spelling and the same rows — for a bound
        non-null parameter the two are identical, and for `{"eq": null}` this is what v0's
        `Eq(col, None)` already compiled to.

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
    def _in(node: In, params: list[Any]) -> str:
        """Null-safe membership: `IN` for the values that have a spelling there, `IS NULL` for the
        one that does not.

        SQL's `IN` is a disjunction over `=`, so it never matches a null in the list and answers
        unknown for a null column — neither is what `ir.In` means. Lifting the null out into its own
        disjunct is what makes `{"in": [null]}` select the rows `{"eq": null}` selects, which
        `_compare` lowers to `IS NULL` by the same argument: `IS NULL` says in SQL what
        `IS NOT DISTINCT FROM NULL` says, without a parameter for a value that has no type.

        The list is never empty — `filters._membership` refuses that before the node exists — so
        this never has to decide what `IN ()` means, which DuckDB rejects as a syntax error."""
        ref = _ref(node.alias, node.column)
        values = [v for v in node.values if v is not None]
        terms = []
        if values:
            params.extend(values)
            terms.append(f"{ref} IN ({', '.join(['?'] * len(values))})")
        if len(values) != len(node.values):
            terms.append(f"{ref} IS NULL")
        return terms[0] if len(terms) == 1 else "(" + " OR ".join(terms) + ")"

    @staticmethod
    def _operand(operand: Any, params: list[Any]) -> str:
        if isinstance(operand, ColumnRef):
            return _ref(operand.alias, operand.column)
        if isinstance(operand, Const):
            params.append(operand.value)
            return "?"
        raise EngineError(f"unsupported operand {type(operand).__name__}")  # pragma: no cover

    @staticmethod
    def _order_and_page(
        alias: str, order_by: Sequence[str], limit: int | None, offset: int, rank: str | None = None
    ):
        """The tail. `rank` is the score's output name, which sorts descending *before* `order_by`.

        By the alias rather than by repeating the expression: DuckDB resolves an output name in
        `ORDER BY`, and spelling the distance twice would be two places for the width to be wrong.
        The columns after it are the tie-break rather than decoration — see `ir.Match`."""
        sql = ""
        params: list[Any] = []
        terms = ([f"{_quote(rank)} DESC"] if rank else []) + [_ref(alias, c) for c in order_by]
        if terms:
            sql += " ORDER BY " + ", ".join(terms)
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
