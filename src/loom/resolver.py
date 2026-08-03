"""The resolver — ontology operations to logical plans, and rows back to property names.

This is the layer the semantic guarantees live in. Every read anything above can perform is one
of the four methods here; none of them takes a predicate, a column, or a table from the caller,
only an object-type api name and property names that must exist in the model. That's what makes
"the LLM never receives raw SQL" structural rather than a convention — there is no code path from
a tool call to arbitrary SQL, because the resolver only ever emits `ir` nodes it built itself.

It's also where governance enforces (M5): column masks and row predicates belong *here*, below the
MCP layer, so a direct API caller and an agent get filtered identically. A mask is applied to the
**projection** and a predicate to the **table**, which are the two strongest places available: a
withheld property is never selected and a withheld row is not in the table the query reads, so
neither is in a result set for anything above to forget to drop. Nothing above this line has to
know a policy exists — and for rows, nothing above it is *told*: a mask announces itself because
the schema is public, and a row predicate does not because the rows are the data. See
`governance.py` for what a policy may say, and `predicate.py` for what a `rows:` expression means on
the two planes that have to agree about it.

**A policy may name a caller and this layer still receives no identity** (M6). What a resolver holds
is a `PolicySet` that is already *decided* — every guard answered, every `principal.` reference
folded to a literal — chosen one rung above by `PolicyProgram.select`, because a principal is
constant for the duration of a call. So none of the enforcement below changed when policies learned
to vary: they read a decided set exactly as they always did, and `governed_by` is how a caller's set
gets in.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from .auth import Principal
from .governance import PolicyProgram, PolicySet
from .model import LinkType, ObjectType, Ontology, Property, coerce_value
from .predicate import lower
from .query.engine import Engine
from .query.ir import Column, Comparison, Contains, Eq, GetByKey, Project, Search, TableRef, ThroughRef, Traverse

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500

# Stable aliases: the projected table is always t0, so compiled SQL is deterministic and
# assertable in tests.
_TARGET = "t0"
_SOURCE = "t1"
_THROUGH = "m0"


class ResolverError(RuntimeError):
    """A caller asked for something the ontology doesn't define, or the data contradicts it."""


@dataclass(frozen=True)
class LinkDirection:
    """A named way out of an object type: a link plus which end you're standing on.

    `name` is what a caller passes — the link's apiName when traversing from its `from` end, or
    its reverseName when traversing from the `to` end."""

    name: str
    link: LinkType
    forward: bool

    @property
    def target_object_type(self) -> str:
        return self.link.to.object_type if self.forward else self.link.frm.object_type

    @property
    def source_object_type(self) -> str:
        return self.link.frm.object_type if self.forward else self.link.to.object_type


@dataclass
class Resolver:
    ontology: Ontology
    engine: Engine
    policies: PolicySet = field(default_factory=PolicySet)
    """What this deployment withholds. Empty by default — a resolver built without one governs
    nothing, which is what every construction of one that predates M5 meant and still means."""

    def governed_by(self, policies: PolicySet) -> Resolver:
        """This resolver, reading for a caller these policies were decided for.

        **Per-call scope, not per-call construction**, and the difference is the whole of why the
        milestone that attests a principal does not have to fix the `t0`/`t1`/`m0` aliases. Nothing
        here opens a catalog or an engine: the ontology and the engine are *shared*, and what is new
        per call is a three-field value holding an already-decided set. A `Resolver` per call would
        multiply the racers rather than remove them.

        Returns `self` when the set is the one already held — which is not an optimisation but the
        assertion that a deployment with no conditional policy is provably unchanged: `select`
        returns the same object for every caller, so this returns the same resolver for every
        caller, so there is nothing per-call about a program that names nobody."""
        if policies is self.policies:
            return self
        return replace(self, policies=policies)

    # ---- reads -----------------------------------------------------------------

    def get(self, object_type: str, key: Any) -> dict | None:
        """Fetch one object by primary key. None when it doesn't exist — or when a policy does not
        show it, which is the same answer on purpose: a caller that could tell the two apart would
        have an existence oracle over the rows a `rows:` predicate withholds.

        One honest consequence of that, since the duplicate-key check below reads the governed set:
        under a predicate this can no longer *see* a duplicate primary key whose second row is
        withheld. The write path still refuses it — `_Run._read` reads physically, and `ambiguous_key`
        is the one refusal that has to, because an equality-delete on a doubled key removes both."""
        obj = self._object(object_type)
        plan = Project(
            source=GetByKey(
                table=self._table(obj, _TARGET),
                key_column=obj.pk_property.column,
                key_value=self._coerce(obj.pk_property, key, f"{object_type} key"),
            ),
            columns=self._projection(obj, _TARGET),
        )
        rows = self._run(plan)
        if len(rows) > 1:
            raise ResolverError(
                f"primary key '{obj.primary_key}' = {key!r} matches {len(rows)} rows in "
                f"'{obj.backing_table}' — the backing table violates the uniqueness the spec declares"
            )
        return rows[0] if rows else None

    def list(self, object_type: str, limit: int | None = None, offset: int = 0) -> list[dict]:
        """A page of objects, ordered by primary key."""
        return self.search(object_type, {}, limit=limit, offset=offset)

    def search(
        self,
        object_type: str,
        filters: Mapping[str, Any] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        """Filter objects by property values.

        A `searchable` string property matches on case-insensitive substring — that's what makes
        it worth declaring searchable. Every other property, including a searchable enum (whose
        values are a closed set, so substring matching would only introduce ambiguity), matches
        exactly.
        """
        obj = self._object(object_type)
        table = self._table(obj, _TARGET)
        plan = Project(
            source=Search(
                table=table,
                filters=self._filters(obj, filters or {}),
                order_by=(obj.pk_property.column,),
                limit=self._page_size(limit),
                offset=self._offset(offset),
            ),
            columns=self._projection(obj, _TARGET),
        )
        return self._run(plan)

    def traverse(
        self,
        object_type: str,
        key: Any,
        link: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict]:
        """Walk one hop. Returns objects of the link's other end, projected as that type."""
        source = self._object(object_type)
        direction = self.link_direction(object_type, link)
        target = self._object(direction.target_object_type)

        source_end = direction.link.frm if direction.forward else direction.link.to
        target_end = direction.link.to if direction.forward else direction.link.frm
        source_join = self._property(source, source_end.property, f"link '{direction.name}'")
        target_join = self._property(target, target_end.property, f"link '{direction.name}'")

        through = None
        if direction.link.through is not None:
            th = direction.link.through
            # `through.fromColumn`/`toColumn` are written against the link's declared from/to
            # ends, so traversing in reverse swaps which one anchors.
            through = ThroughRef(
                table=TableRef(catalog=th.catalog, table=th.table, alias=_THROUGH),
                from_column=th.from_column if direction.forward else th.to_column,
                to_column=th.to_column if direction.forward else th.from_column,
            )

        plan = Project(
            source=Traverse(
                from_table=self._table(source, _SOURCE),
                to_table=self._table(target, _TARGET),
                from_column=source_join.column,
                to_column=target_join.column,
                anchor=Eq(
                    alias=_SOURCE,
                    column=source.pk_property.column,
                    value=self._coerce(source.pk_property, key, f"{object_type} key"),
                ),
                through=through,
                order_by=(target.pk_property.column,),
                limit=self._page_size(limit),
                offset=self._offset(offset),
            ),
            columns=self._projection(target, _TARGET),
        )
        return self._run(plan)

    # ---- model lookups ---------------------------------------------------------

    def links_of(self, object_type: str) -> list[LinkDirection]:
        """Every named hop out of an object type, both declared and reverse directions."""
        out: list[LinkDirection] = []
        for link in self.ontology.link_types.values():
            if link.frm.object_type == object_type:
                out.append(LinkDirection(name=link.api_name, link=link, forward=True))
            if link.reverse_name and link.to.object_type == object_type:
                out.append(LinkDirection(name=link.reverse_name, link=link, forward=False))
        return out

    def link_direction(self, object_type: str, name: str) -> LinkDirection:
        """Resolve a link name as seen from `object_type`, in either direction."""
        for candidate in self.links_of(object_type):
            if candidate.name == name:
                return candidate
        known = ", ".join(sorted(c.name for c in self.links_of(object_type))) or "none"
        raise ResolverError(f"'{object_type}' has no link '{name}' (available: {known})")

    def _object(self, api_name: str) -> ObjectType:
        obj = self.ontology.object_types.get(api_name)
        if obj is None:
            known = ", ".join(sorted(self.ontology.object_types)) or "none"
            raise ResolverError(f"unknown object type '{api_name}' (known: {known})")
        return obj

    @staticmethod
    def _property(obj: ObjectType, name: str, ctx: str):
        prop = obj.properties.get(name)
        if prop is None:  # pragma: no cover - the referential validator rejects this at build time
            raise ResolverError(f"{ctx}: '{obj.api_name}' has no property '{name}'")
        return prop

    def _table(self, obj: ObjectType, alias: str) -> TableRef:
        """An object type as a table — and as the rows of it this deployment shows.

        The one place a type becomes a table, which is why the row predicate is attached here: both
        ends of a traverse are governed by this line rather than by two rules somebody has to
        remember, and `get`, `search` and `list` are governed by it without asking. See
        `ir.TableRef` for why the predicate rides on the table reference rather than on the three
        source nodes.

        **The one assertion a per-caller `PolicySet` needs.** An undecided set is the announcement
        `PolicyProgram.announcements()` builds for the tool descriptions and the startup banner; its
        predicates may still name a principal, so reading with it would be a read nobody selected —
        and the failure would be *silent*, one conditional policy short. Every read goes through this
        method, which is what makes the check total rather than a habit."""
        if not self.policies.decided:
            raise ResolverError(
                "this resolver holds policies that were never decided for a caller — a read must "
                "use the set selected for the principal of the call in flight (PolicyProgram.select)"
            )
        expr = self.policies.predicate_for(obj.api_name)
        return TableRef(
            catalog=obj.backing_catalog,
            table=obj.backing_table,
            alias=alias,
            predicate=None if expr is None else lower(expr, obj, alias),
        )

    def masked(self, object_type: str) -> tuple[str, ...]:
        """The properties a policy withholds from every read of this type.

        Public because the surface has to be able to *say* it — see `governance.py` on why a mask
        announces itself and a row predicate will not — and because saying it is the only part of a
        mask that belongs above this layer. The withholding itself is already done by the time
        anything can ask."""
        return self.policies.masked(object_type)

    def _projection(self, obj: ObjectType, alias: str) -> tuple[Column, ...]:
        """The columns a read selects: declared properties, minus what a policy withholds.

        Withheld by never being *asked for*, rather than by being dropped from the rows on the way
        back. It costs nothing and it is a stronger claim: a masked value is not in the result set,
        so there is no layer above this one that could return it by forgetting to filter, and none
        of them has to know a policy exists. `bind_policies` refuses a mask on a primary key, which
        is what guarantees this tuple is never empty."""
        masked = self.policies.masked(obj.api_name)
        return tuple(
            Column(alias=alias, column=p.column, output=p.name)
            for p in obj.properties.values()
            if p.name not in masked
        )

    def _filters(self, obj: ObjectType, filters: Mapping[str, Any]) -> tuple[Comparison, ...]:
        out: list[Comparison] = []
        for name, value in filters.items():
            prop = obj.properties.get(name)
            if prop is None:
                known = ", ".join(obj.properties) or "none"
                raise ResolverError(f"'{obj.api_name}' has no property '{name}' (known: {known})")
            policy = self.policies.masked_by(obj.api_name, name)
            if policy is not None:
                # A refusal rather than an empty result, and the difference is the whole point: a
                # filter on a withheld property is an oracle for its value — a substring filter
                # binary-searches it in a handful of calls, and an exact one confirms a guess. The
                # refusal gives away only what the mask already announced.
                raise ResolverError(
                    f"'{obj.api_name}.{name}' is withheld by governance policy '{policy}', so it "
                    "cannot be filtered on either — a filter on a property you cannot read answers "
                    "the question the mask refused"
                )
            if prop.type.kind == "string" and name in obj.searchable and value is not None:
                out.append(Contains(alias=_TARGET, column=prop.column, value=str(value)))
            else:
                out.append(
                    Eq(
                        alias=_TARGET,
                        column=prop.column,
                        value=self._coerce(prop, value, f"filter '{name}'"),
                    )
                )
        return tuple(out)

    def _coerce(self, prop: Property, value: Any, ctx: str) -> Any:
        """Bring a caller-supplied value to the property's declared Python type.

        Delegates to `model.coerce_value`, which the action runtime uses on the way *out* for
        exactly the same reason this uses it on the way in — see its docstring. Only the failure
        type is this layer's business."""
        try:
            return coerce_value(prop.type, value, self.ontology.object_types, ctx)
        except ValueError as e:
            raise ResolverError(str(e)) from e

    # ---- paging ----------------------------------------------------------------

    @staticmethod
    def _page_size(limit: int | None) -> int:
        """Always bounded. An unbounded read is a way for one tool call to pull a whole table
        into an agent's context, so there is no way to ask for one."""
        if limit is None:
            return DEFAULT_PAGE_SIZE
        if limit < 1:
            raise ResolverError(f"limit must be >= 1, got {limit}")
        return min(limit, MAX_PAGE_SIZE)

    @staticmethod
    def _offset(offset: int) -> int:
        if offset < 0:
            raise ResolverError(f"offset must be >= 0, got {offset}")
        return offset

    def _run(self, plan: Project) -> list[dict]:
        return list(self.engine.execute(self.engine.compile(plan)))


def build_resolver(ontology: Ontology, config, catalogs: Mapping[str, Any] | None = None) -> Resolver:
    """Wire an ontology to the engine and catalogs named in a project config.

    **This is where an engine is negotiated with, and "at serve" was the wrong place for it.** M4's
    box says capability negotiation happens at serve, which is where it is *observed* rather than
    where it belongs: this function is the one place a spec and an engine are paired, so checking
    here means `loom query` refuses exactly what `loom serve` refuses. Putting it in `cmd_serve`
    would leave a dev command reading successfully out of an engine the served surface will not
    stand on — the same shape as the back door `loom query` was deliberately built not to be, and
    the same principle M5 states for governance: enforce below MCP so a direct call and an agent
    call get the same answer.

    It is not an invariant of `Resolver` itself, which stays constructible from any engine. That is
    what lets a test drive the resolver with a fake, and an adapter be exercised before anybody has
    decided which ontology it will serve; the pairing is what has to be checked, not the pair.

    **And this is where a governance policy is bound**, for the reason above rather than a second
    one — M4's capability slice said it was borrowing M5's principle a milestone early, and this is
    the milestone paying it back through the same function. `loom query` and `loom serve` refuse the
    same policies and withhold the same properties, because there is one place that turns a config
    and a spec into something that can read. Binding here rather than in `loom validate` is the same
    boundary seen from the other side: a spec that is valid stays valid whatever a deployment
    withholds of it, and `loom validate` does not require a `loom.yaml` to exist at all.

    **One sentence above needed correcting when policies learned to name a caller, and it is
    narrower rather than wrong.** *`loom query` refuses exactly what `loom serve` refuses* is a claim
    about the **pairing** — a spec against a deployment — and every refusal in `bind_reads` is still
    surface-blind and fires identically for both. What differs is not a check but an *ability*: this
    function additionally asks for a policy set while naming nobody, which is a true statement about
    the surface calling it and not a property of the pairing. A conditional program refuses here for
    that reason (`PolicyProgram.select`), and it is one function rather than a second check beside
    the first — nothing was reopened with a `surface=` argument, which would also have got this case
    wrong: `config.mcp.attests` is true for an attesting config that `loom query` still cannot attest
    anybody with."""
    return bind_reads(ontology, config, catalogs).for_(None)


@dataclass(frozen=True)
class ReadBinding:
    """A spec paired with a deployment on the read plane, before any caller is known.

    What `build_resolver` was, split at the one seam a per-caller policy needs: everything expensive
    and everything static happens once, here, and `for_` is a value-level step that can happen per
    call. `build_server` holds one of these for the process; `loom query` calls `for_(None)` and
    never keeps it."""

    ontology: Ontology
    engine: Engine
    program: PolicyProgram

    def for_(self, principal: Principal | None) -> Resolver:
        """A resolver reading as this caller, or the refusal for a surface that has none."""
        return Resolver(
            ontology=self.ontology, engine=self.engine, policies=self.program.select(principal)
        )

    def announcing(self) -> Resolver:
        """A resolver for building tool schemas and a banner, which cannot read a row.

        The tool set is a function of the spec and the deployment's *masks*, and masks are the same
        for every caller by construction — so it is assembled once, from here, rather than per
        caller. What this resolver cannot do is answer a read: see `Resolver._table`."""
        return Resolver(
            ontology=self.ontology, engine=self.engine, policies=self.program.announcements()
        )


def bind_reads(
    ontology: Ontology, config, catalogs: Mapping[str, Any] | None = None
) -> ReadBinding:
    """Pair this spec with this deployment on the read plane. Every static refusal lives here."""
    from .catalog import open_catalogs
    from .governance import bind_policies
    from .negotiate import check_capabilities
    from .query.engines import open_engine

    open_cats = catalogs if catalogs is not None else open_catalogs(config)
    engine = open_engine(config.engine, open_cats)
    check_capabilities(ontology, engine.capabilities())
    auth = config.mcp.auth
    return ReadBinding(
        ontology=ontology,
        engine=engine,
        program=bind_policies(ontology, config.policies, auth.claims if auth else {}),
    )
