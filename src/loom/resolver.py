"""The resolver — ontology operations to logical plans, and rows back to property names.

This is the layer the semantic guarantees live in. Every read anything above can perform is one
of the five methods here; none of them takes a predicate, a column, or a table from the caller,
only an object-type api name and property names that must exist in the model. That's what makes
"the LLM never receives raw SQL" structural rather than a convention — there is no code path from
a tool call to arbitrary SQL, because the resolver only ever emits `ir` nodes it built itself.

**The fifth is `match`, and it takes one thing the other four do not: a vector.** That is a *value*,
in the same category as a filter's literal, so the claim above is unchanged — a caller cannot name a
column with it any more than they can with a string. What it buys is that this layer still calls no
model: a ranked read would otherwise put a network round trip in the middle of building a plan. See
`embed.match`, which is where the caller's words become the vector this takes.

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

from collections.abc import Container, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from .auth import Principal
from .catalog.base import VECTOR_KEY_COLUMN, vector_table
from .embed.store import (
    DIMS_COLUMN,
    EMBEDDED_AT_COLUMN,
    MODEL_COLUMN,
    PROPERTY_COLUMN,
    VECTOR_COLUMN,
    oldest,
)
from .filters import FilterError
from .filters import lower as lower_filter
from .governance import PolicyProgram, PolicySet
from .model import LinkType, ObjectType, Ontology, Property, coerce_value
from .predicate import lower
from .query.engine import Engine
from .query.ir import (
    Column,
    Comparison,
    Eq,
    GetByKey,
    LinkFilter,
    Match,
    Project,
    Search,
    TableRef,
    ThroughRef,
    Traverse,
    VectorRef,
)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500

# Stable aliases: the projected table is always t0, so compiled SQL is deterministic and
# assertable in tests.
#
# **Three of them are constants and two are prefixes, and that is the milestone M6 twice predicted
# arriving by a route neither prediction named.** M6 said an attested principal would force these to
# become per-call, and it did not: a `PolicySet` is a value and a `Resolver` is shared. What forces
# it is `via` — a single plan may now name an unbounded number of tables, one per hop plus a mapping
# table each, so there is no constant to be had. The three that *can* stay constant do, because a
# plan holds exactly one of each and a stable name is what makes compiled SQL assertable.
_TARGET = "t0"
_SOURCE = "t1"
_THROUGH = "m0"
_VECTORS = "v0"
_LINK = "l"  # `l0`, `l1`, … — the far end of each `via` hop, in the order the hops were written
_LINK_THROUGH = "lm"  # `lm0`, … — that hop's mapping table, when the link declares one

# What a ranked read calls the two things a projection has no name for: the score, which is a column
# of no table, and the stamp on the vector that produced it.
#
# **Prefixed, and then made unique anyway.** Nothing stops a spec declaring a property called
# `score`, and the engine hands rows back as a dict keyed by output name — so a collision would not
# be an error, it would be two columns silently becoming one. `_unique` is what makes the two
# vocabularies un-mixable at the one layer where they briefly share a namespace; everywhere above
# this line they are already separated, Loom's word beside the object rather than inside it.
_SCORE_OUTPUT = "_loom_score"
_STAMP_OUTPUT = "_loom_embedded_at"


def _unique(base: str, taken: Container[str]) -> str:
    """`base`, lengthened until it is not one of `taken`."""
    name = base
    while name in taken:
        name += "_"
    return name


class ResolverError(RuntimeError):
    """A caller asked for something the ontology doesn't define, or the data contradicts it."""


@dataclass(frozen=True)
class Ranked:
    """One row of a ranked read: how near it came, and the object itself.

    **The score sits beside the object rather than inside it**, which is §7's namespace rule reaching
    the result. The object is the spec's vocabulary and `score` is Loom's, so putting one inside the
    other would let a declared property called `score` shadow the only thing this read adds — and,
    worse, would do it silently on exactly the ontologies most likely to have one."""

    score: float
    object: dict


@dataclass(frozen=True)
class MatchResult:
    """A page of a ranking, and the two facts that say what it is a ranking *by*.

    `model` because a similarity is only meaningful relative to the model that produced both sides
    of it, and because a deployment that has swapped models ranks nothing until `loom embed
    --remodel` runs — an empty page naming a model is diagnosable, an empty page is not.

    `embedded_as_of` is the oldest stamp **among these rows**: every object here was embedded at
    least this recently. See `embed.store.oldest` for why that set rather than the whole sidecar."""

    object_type: str
    property: str
    model: str
    matches: tuple[Ranked, ...] = ()
    embedded_as_of: datetime | None = None


_REVERSED_CARDINALITY = {"many_to_one": "one_to_many", "one_to_many": "many_to_one"}
"""The two cardinalities that are not symmetric, and therefore the whole of what a reverse hop
changes about one. See `LinkDirection.cardinality`."""


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

    @property
    def cardinality(self) -> str:
        """The cardinality **of this direction**, which is not always the link's own.

        `target_object_type` and `source_object_type` already flip with `forward`, and this is the
        third fact about a hop that has to: a `many_to_one` link traversed from its `to` end is a
        `one_to_many` hop, and reporting the declared word there tells a caller *at most one* about
        a route that returns as many rows as exist. The self-contradiction was visible in the
        envelope that carried it — `cardinality: many_to_one` beside a `count` of 5 and `hasMore`.

        `one_to_one` and `many_to_many` are symmetric, so they read the same from either end."""
        if self.forward:
            return self.link.cardinality
        return _REVERSED_CARDINALITY.get(self.link.cardinality, self.link.cardinality)


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

        Two spellings, ANDed together and with each other: a bare value — a case-insensitive
        substring for a `searchable` string property, exact for everything else, which is what it
        has always meant — or an object of comparison operators, `{"salesDate": {"gte": ...,
        "lt": ...}}`. `filters.py` is the whole of what one may say, including why a bare `null` is
        refused there rather than answered here."""
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

    def match(
        self,
        object_type: str,
        vector: Sequence[float],
        model: str,
        filters: Mapping[str, Any] | None = None,
        via: Mapping[str, Any] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> MatchResult:
        """Rank this type's rows by how near their stored vector is to `vector`.

        **It takes a vector and never a string, which is what keeps the model out of this layer.**
        Every other read here is a pure function of the ontology and the caller's arguments; a
        ranked one would otherwise have to reach for a network or a 150MB runtime in the middle of
        building a plan. A vector is a *value*, like a filter's literal, so the resolver's standing
        claim is untouched: nothing a caller sends becomes a predicate, a column or a table.

        `model` arrives with it because the two travel together — a vector means nothing except
        relative to the model that produced it — and it is what the comparability guard compares the
        sidecar's own column against. See `ir.VectorRef`.

        **The filters are the same grammar `search` takes, answered by the same code**, and they
        narrow *before* the ranking. That is a rule rather than a per-query choice; `ir.Match` says
        why. Governance needs no line here at all: the predicate rides on the table this builds
        through `_table`, so a withheld row is not ranked low, it is not there.

        **`via` is that same sentence one hop out**, and it is what makes the ranking answer the
        question anyone actually has. Ranking orders by meaning and cannot say *belonging to a
        gold-tier customer*; a hop is keyed by link name and carries the identical filter grammar,
        read against the type at the far end and governed by the same `_table`. It narrows with the
        filters, before the ranking, for the same reason they do."""
        obj = self._object(object_type)
        prop = obj.semantic_property
        if prop is None:
            declared = ", ".join(
                sorted(o.api_name for o in self.ontology.object_types.values() if o.semantic)
            )
            raise ResolverError(
                f"'{object_type}' declares no 'semantic:' property, so there is nothing to rank it "
                f"by (types that do: {declared or 'none'})"
            )
        if not vector:
            # Not an empty ranking: a zero-width query has no distance to anything, so every row
            # would come back in primary-key order wearing a score. `{"in": []}`'s argument — an
            # answer a caller cannot tell from a real one is worse than a refusal.
            raise ResolverError(f"the query vector for '{object_type}' is empty, so it ranks nothing")

        columns = self._projection(obj, _TARGET)
        taken = {c.output for c in columns}
        score_as = _unique(_SCORE_OUTPUT, taken)
        stamp_as = _unique(_STAMP_OUTPUT, taken | {score_as})
        plan = Project(
            source=Match(
                table=self._table(obj, _TARGET),
                vectors=self._vectors(obj, model, prop),
                key_column=obj.pk_property.column,
                query=tuple(float(x) for x in vector),
                score_as=score_as,
                filters=self._filters(obj, filters or {}),
                links=self._via(obj, via or {}),
                # The tie-break, not the order. Two rows at the same distance would otherwise swap
                # between calls and make page 2 unrelated to page 1 — `search`'s reason, and the
                # reason `offset` means something here at all.
                order_by=(obj.pk_property.column,),
                limit=self._page_size(limit),
                offset=self._offset(offset),
            ),
            columns=(*columns, Column(alias=_VECTORS, column=EMBEDDED_AT_COLUMN, output=stamp_as)),
        )

        matches: list[Ranked] = []
        stamps: list[Any] = []
        for row in self._run(plan):
            data = dict(row)
            score = data.pop(score_as)
            stamps.append(data.pop(stamp_as, None))
            matches.append(Ranked(score=float(score), object=data))
        return MatchResult(
            object_type=obj.api_name,
            property=prop.name,
            model=model,
            matches=tuple(matches),
            embedded_as_of=oldest(stamps),
        )

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
        """Resolve a link name as seen from `object_type`, in either direction.

        The object type is resolved first, because `links_of` answers an empty list for a name no
        ontology declares and the sentence below then reads as though it did: `traverse` on a typo'd
        `objectType` came back `'Widget' has no link 'orders' (available: none)`, which tells an
        agent that Widget is a type it may start from and that this deployment declares no links
        from it. Both halves are false, and the second sends it looking for the link rather than for
        the typo. `_object` already has the sentence that is true."""
        self._object(object_type)
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

    def _vectors(self, obj: ObjectType, model: str, prop: Property) -> VectorRef:
        """An object type as its sidecar — `_table`'s sibling, and beside it for that reason.

        The one place a type becomes a table is the one place it becomes a *sidecar*, so the name
        comes from `vector_table` here and from nowhere a caller can reach. It carries no predicate
        and cannot be given one: `ir.VectorRef` has no field for it, because a sidecar stands for no
        object type and a ranked read is governed on the end that does.

        In the object's own catalog, which is `EmbedRuntime._store`'s choice seen from the read
        side: a vector is derived from the rows of one table and is written beside them."""
        return VectorRef(
            table=TableRef(
                catalog=obj.backing_catalog, table=vector_table(obj.api_name), alias=_VECTORS
            ),
            key_column=VECTOR_KEY_COLUMN,
            vector_column=VECTOR_COLUMN,
            model_column=MODEL_COLUMN,
            dims_column=DIMS_COLUMN,
            property_column=PROPERTY_COLUMN,
            model=model,
            property=prop.name,
        )

    def _via(self, obj: ObjectType, via: Mapping[str, Any]) -> tuple[LinkFilter, ...]:
        """A caller's `via` argument as existential hops — the same grammar, one join out.

        **Keyed by link name, and that is why it is a separate argument rather than a dotted key
        inside `filter`.** `filter` is shared with `search_` through `_filterable`, and §7's namespace
        rule is that each level of the argument tree belongs entirely to one vocabulary: property
        names there, link names here. A `placedBy.tier` key inside `filter` would put a link name and
        a property name in one namespace, so an ontology with a link and a property of the same name
        would have a surface that could not say which one a caller meant.

        Everything below the link name is `_filters` unchanged, against the type at the *far* end and
        with that hop's alias — which is what makes a mask one hop out refuse a filter exactly as a
        mask here does, without a second rule. The far table comes from `_table`, so its rows are the
        rows this deployment shows; `ir.LinkFilter` covers what is existential about it and
        `DuckDBEngine._compile_match` covers where a hop's predicate has to be *placed*."""
        if not isinstance(via, Mapping):
            got = "null" if via is None else type(via).__name__
            available = ", ".join(sorted(d.name for d in self.links_of(obj.api_name))) or "none"
            raise ResolverError(
                f"'via' takes an object keyed by link name, got {got} — write "
                f'{{"via": {{"<link>": {{"<property>": ...}}}}}} (links from '
                f"'{obj.api_name}': {available})"
            )
        out: list[LinkFilter] = []
        for index, (name, body) in enumerate(via.items()):
            direction = self.link_direction(obj.api_name, name)
            far = self._object(direction.target_object_type)
            near_end = direction.link.frm if direction.forward else direction.link.to
            far_end = direction.link.to if direction.forward else direction.link.frm
            near_join = self._property(obj, near_end.property, f"link '{name}'")
            far_join = self._property(far, far_end.property, f"link '{name}'")
            if not isinstance(body, Mapping):
                got = "null" if body is None else type(body).__name__
                raise ResolverError(
                    f"'via.{name}' takes an object of {far.api_name} filters, got {got} — write "
                    f'{{"via": {{"{name}": {{"<property>": ...}}}}}}, or {{}} to keep only rows that '
                    f"have a {far.api_name} at all"
                )
            alias = f"{_LINK}{index}"
            through = None
            if direction.link.through is not None:
                # `through.fromColumn`/`toColumn` are written against the link's declared from/to
                # ends, so a reverse hop swaps which one the near side joins — `traverse`'s line,
                # read from the end that is filtering rather than the end that is landing.
                th = direction.link.through
                through = ThroughRef(
                    table=TableRef(
                        catalog=th.catalog, table=th.table, alias=f"{_LINK_THROUGH}{index}"
                    ),
                    from_column=th.from_column if direction.forward else th.to_column,
                    to_column=th.to_column if direction.forward else th.from_column,
                )
            out.append(
                LinkFilter(
                    table=self._table(far, alias),
                    near_column=near_join.column,
                    far_column=far_join.column,
                    filters=self._filters(far, body, alias),
                    through=through,
                )
            )
        return tuple(out)

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

    def _filters(
        self, obj: ObjectType, filters: Mapping[str, Any], alias: str = _TARGET
    ) -> tuple[Comparison, ...]:
        """A caller's `filter` argument as comparison nodes — the spec, then governance, then grammar.

        The split is the layering: *which properties may be filtered at all* is answered twice, by
        the spec's `searchable` list and then by this deployment's policies, while *what may be said
        about one* is the grammar's question and lives in `filters.py`, where the surface reads the
        same answer to build its schema. A governed property is refused before its value is even
        parsed, so no refusal can be a function of what a caller guessed the value might be.

        **`searchable` is checked here and not only at the surface.** It used to be enforced by
        the surface alone — `_filterable` builds both read tools' `filter` schemas from the list, so
        the ranked read inherited the same hole — and a caller who ignored the advertised schema
        could filter on any declared property, leaving `filter`'s `additionalProperties: false` an
        announcement nothing made true. §2 rule 6 is the
        narrower claim: *a property still has to be listed here to be filterable at all*. The one
        thing the list already gated at this level was `contains` (`filters.operators`), which is
        why the refusal below borrows that one's words.

        It is checked *before* the mask, so a deployment's policy names are never mentioned for a
        property that was never filterable in the first place — a mask on a searchable property
        still gets the oracle refusal, which is the case that argument was written for.

        **`alias` is what makes `via` cost this method nothing.** A hop filters the same grammar
        against a different type at a different alias, so every refusal above — not declared, not
        searchable, withheld by a policy — is inherited one join out rather than restated there. In
        particular the oracle refusal: filtering a masked property of the *far* type would binary-
        search it exactly as filtering a masked property here would, and it is refused by this code
        because it is this code."""
        if not isinstance(filters, Mapping):
            # The shape, before the contents. `Resolver.search` is reached from a tool call whose
            # arguments are whatever JSON arrived, and a `filter` that is not an object used to
            # reach `.items()` and come back as a raw AttributeError — the same defect the surface
            # fixed one level up when a missing argument surfaced as `KeyError: 'key'`.
            got = "null" if filters is None else type(filters).__name__
            raise ResolverError(
                f"'filter' takes an object of property filters, got {got} — write "
                f'{{"filter": {{"{next(iter(obj.searchable), "property")}": ...}}}}'
            )
        out: list[Comparison] = []
        for name, value in filters.items():
            prop = obj.properties.get(name)
            if prop is None:
                known = ", ".join(obj.properties) or "none"
                raise ResolverError(f"'{obj.api_name}' has no property '{name}' (known: {known})")
            if name not in obj.searchable:
                available = ", ".join(obj.searchable)
                raise ResolverError(
                    f"'{obj.api_name}.{name}' is not declared searchable, so it cannot be filtered "
                    "on — 'searchable' is what decides a property is filterable at all. Add "
                    f"'{name}' to this objectType's 'searchable' list, or filter on: "
                    + (available or "nothing, since this objectType declares none")
                )
            policy = self.policies.masked_by(obj.api_name, name)
            if policy is not None:
                # A refusal rather than an empty result, and the difference is the whole point: a
                # filter on a withheld property is an oracle for its value — a substring filter
                # binary-searches it in a handful of calls, and an exact one confirms a guess. A
                # range narrows it faster than either, which is the same refusal earning its keep
                # against a grammar it was written before. The refusal gives away only what the
                # mask already announced.
                raise ResolverError(
                    f"'{obj.api_name}.{name}' is withheld by governance policy '{policy}', so it "
                    "cannot be filtered on either — a filter on a property you cannot read answers "
                    "the question the mask refused"
                )
            try:
                out.extend(
                    lower_filter(obj, prop, value, alias, self.ontology.object_types)
                )
            except FilterError as e:
                raise ResolverError(str(e)) from e
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
        into an agent's context, so there is no way to ask for one.

        **Refused rather than clamped, and both bounds the same way.** This used to answer a limit
        above the cap with `min(limit, MAX_PAGE_SIZE)`, which reads like generosity and is a silent
        narrowing: the page an agent got back was 500 rows while the envelope echoed the number it
        asked for, so a client paging with `offset += limit` stepped past everything it had not
        been given. The lower bound was already a refusal, and enforcing one bound while quietly
        rewriting the other is what made the two disagree. Now the number a caller sends is either
        the page size or an error, and the envelope cannot report a size that was not used."""
        if limit is None:
            return DEFAULT_PAGE_SIZE
        if limit < 1:
            raise ResolverError(f"limit must be >= 1, got {limit}")
        if limit > MAX_PAGE_SIZE:
            raise ResolverError(
                f"limit must be <= {MAX_PAGE_SIZE}, got {limit} — {MAX_PAGE_SIZE} is the largest "
                "page this surface serves. Ask for that many and page with 'offset'"
            )
        return limit

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
