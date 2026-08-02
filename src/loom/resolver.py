"""The resolver — ontology operations to logical plans, and rows back to property names.

This is the layer the semantic guarantees live in. Every read anything above can perform is one
of the four methods here; none of them takes a predicate, a column, or a table from the caller,
only an object-type api name and property names that must exist in the model. That's what makes
"the LLM never receives raw SQL" structural rather than a convention — there is no code path from
a tool call to arbitrary SQL, because the resolver only ever emits `ir` nodes it built itself.

It's also where governance enforces (M5): column masks — and row predicates after them — belong
*here*, below the MCP layer, so a direct API caller and an agent get filtered identically. A mask
is applied to the **projection**, which is the strongest place available: a withheld property is
never selected, so it is never in a result set for anything above to forget to drop. See
`governance.py` for what a policy may say and why none of them names a caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .governance import PolicySet
from .model import LinkType, ObjectType, Ontology, Property, coerce_value
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

    # ---- reads -----------------------------------------------------------------

    def get(self, object_type: str, key: Any) -> dict | None:
        """Fetch one object by primary key. None when it doesn't exist."""
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

    @staticmethod
    def _table(obj: ObjectType, alias: str) -> TableRef:
        return TableRef(catalog=obj.backing_catalog, table=obj.backing_table, alias=alias)

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
    withholds of it, and `loom validate` does not require a `loom.yaml` to exist at all."""
    from .catalog import open_catalogs
    from .governance import bind_policies
    from .negotiate import check_capabilities
    from .query.engines import open_engine

    open_cats = catalogs if catalogs is not None else open_catalogs(config)
    engine = open_engine(config.engine, open_cats)
    check_capabilities(ontology, engine.capabilities())
    return Resolver(
        ontology=ontology, engine=engine, policies=bind_policies(ontology, config.policies)
    )
