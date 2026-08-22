"""What a caller's `filter` argument may say — one grammar, read by the surface and the resolver.

`predicate.py` is this module's twin: that one is what a *deployment* may say about rows, this one is
what a *caller* may. They overlap on the six comparisons and differ in both directions, which is the
whole of why `ir` has the node set it has — see the comment blocks there.

**Why one module rather than two halves.** The operators a property admits are needed in two places:
`mcp/registry` generates a JSON Schema from them (announcement) and `Resolver._filters` accepts or
refuses against them (enforcement). Those two answering differently is the failure mode this codebase
keeps closing — a surface advertising an argument that fails on every call, or accepting one it never
advertised — so the operator set is a function here and `test_filters.py` asserts the schema and the
lowering are generated from the same one.

**The two spellings, and why the older one survives.**

    {"tier": "gold"}                         the v0 spelling — a bare value
    {"salesDate": {"gte": "2026-01-01",      operators, ANDed
                   "lt":  "2026-02-01"}}
    {"tier": {"in": ["gold", "platinum"]}}   membership — a disjunction of values

A bare value is **type-directed sugar**: `contains` on a searchable string property, `eq` on
everything else. That is exactly what v0 meant by it, and preserving it is not politeness — making
the bare spelling uniformly mean `eq` would silently *narrow* every filter already written against a
searchable string, returning fewer rows with no error. Loom refuses rather than degrades, and a
silent narrowing is the degradation `negotiate.py` argues is worse than failing.

**Composition is AND, at both levels** — several operators on one property, several properties in one
filter — because `ir.Search.filters` is a flat ANDed tuple and AND therefore costs no new node.

**`in` is the one disjunction here, and admitting it corrects a claim this module used to make.**
The sentence that stood here said `in` was "sugar over an OR that does not exist yet" and therefore
had to wait for one. It does not. An `or` composes *predicates* and needs the tuple to become a tree;
`in` disjoins *values*, against one column, all of them constants — one node, `ir.In`, sitting in the
flat tuple exactly where `Contains` sits. What it does inherit from the `eq` it abbreviates is
**null-safety**: an abbreviation that selected different rows than the thing it abbreviates would be
a trap, and an invisible one, since the two spellings agree on every table with no nulls in the
filtered column. `or` and `not` stay deferred — and `not` is the one that costs, because it reopens
the no-negation argument below rather than sidestepping it.

**Null, and the two refusals this grammar makes.** A bare `{"ltv": null}` is refused, permanently.
JSON cannot tell a key a caller left blank from one they meant as null — and an agent emitting null
for *a value it did not have* is the likeliest way this argument is ever malformed. v0 answered it as
`ltv IS NULL`, which returns a plausible, wrong, non-empty result set: the same failure mode
`negotiate.py` refuses when it declines to compile `Contains` down to `Eq`. So null is legal only
where the caller also wrote the operator — `{"ltv": {"eq": null}}` — which cannot be a blank field.
§5's *null is a value you can test with `==`/`!=`, not one you can order* then becomes visible in the
generated schema itself: the two equality operators accept null and the four ordering ones do not.
`in` accepts one for the same reason `eq` does, and is the third place that fact is written down.

The second refusal is **`{"in": []}`**, and it is the same argument reached from the other end. An
empty list has an honest answer — no rows — and returning it is what makes it a refusal: a caller
cannot tell "your list was empty" from "nothing matched", so an agent whose candidate set collapsed
to nothing gets told, in the vocabulary of a result, that its question was answered. `minItems: 1`
in the generated schema is that refusal announced rather than only enforced.

Everything else about null follows `predicate.py` without needing a second rule: an ordering
comparison against a null column is **undecided**, and an undecided row is not returned. For a
policy that is *admitted only on true*; for a filter it is the flatter statement that a filter
selects rows and an undecided row is not selected. The two agree here for a reason worth writing
down — **this grammar has no negation**, so the disagreement M5 had to settle (`NOT undecided`
failing open) cannot arise, and SQL's three-valued answer and Loom's are the same rows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ._shape import suggest
from .model import ObjectType, Property, coerce_value
from .query.ir import ColumnRef, Compare, Comparison, Const, Contains, In

FILTER_OPS: Mapping[str, str] = {
    "eq": "==",
    "ne": "!=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}
"""Every comparison operator, in the one place that maps a filter's spelling to §5's.

Two spellings for one meaning is a cost, paid deliberately: `gte` is what an agent writes without
being told, and `>=` is not a JSON key anybody reaches for. The IR keeps §5's spelling, so nothing
below this module has two names for one operator."""

CONTAINS = "contains"
"""Case-insensitive substring — what declaring a string property `searchable` has always meant.

Named `contains` in the filter namespace while §5 reserves the same word for membership in a list,
and that collision is deliberate rather than missed. They are different vocabularies (§7: operator
keys are Loom's words, one level below the spec's), an agent writes `contains` for substring without
being taught, and the ontology-facing meaning of §5's `contains` is unreachable until `array`
properties land. When they do, an array property's membership operator needs a name of its own; this
word is spent."""

MEMBERSHIP = "in"
"""Equality against any of a list of values — the equality family's third spelling.

Offered wherever `eq` is, and gated by nothing, because it *is* `eq`: whatever an engine can compare
for equality it can compare against two values. That is also why it demands no new negotiated
capability — `negotiate.py`'s rule is that a requirement is something a spec can demand and an engine
can fail, and no dialect that can say `WHERE c = ?` cannot say `WHERE c IN (?, ?)`."""

NULLABLE_OPS = frozenset({"eq", "ne", MEMBERSHIP})
"""The operators §5 answers for a null, and therefore the ones whose schema admits one.

`in` belongs here as a container: the *list* is never null (that is `{"in": null}`, refused), but an
element may be, and it means there what it means to `eq`."""

ORDERINGS = tuple(op for op in FILTER_OPS if op not in NULLABLE_OPS)

ORDERABLE_KINDS = frozenset(
    {"string", "int", "long", "double", "decimal", "date", "timestamp"}
)
"""Kinds a caller may order.

`enum` is not one: its `values` are a declared set, and their order in the YAML is a list rather
than an ordering — `tier > 'bronze'` would answer with whatever the engine's collation says about
the strings, which is not what the spec declared. `boolean` and `objectRef` are not orderable for
the same reason read from the other end: an objectRef travels as a key, and keys are equal or not.

`string` *is* here, and it is the weakest entry: the answer is the engine's collation rather than
Loom's. It stays because §5 already orders strings and `predicate.py` already lowers
`object.name > 'm'` for a policy — refusing it here would mean the deployment's grammar and the
caller's disagree about an operator both can spell, which is a worse thing to explain than a
collation."""


class FilterError(ValueError):
    """A caller's filter is not something this grammar can say."""


def operators(prop: Property, searchable: bool) -> tuple[str, ...]:
    """Every operator this property admits, in the order a schema and a plan should list them.

    `searchable` gates `contains` and nothing else. That is not the surface's gate — which
    properties appear in the `filter` schema at all is `_search_tool`'s business — but a narrower
    fact that has to be true here too: `negotiate.py` demands `case_insensitive_like` of an engine
    for exactly the *searchable string* properties, so emitting a `Contains` for any other property
    would ask an engine for something no requirement checked it could do."""
    # `in` sits with the equality family and ahead of the orderings, because it *is* equality —
    # and this order is the one a schema lists operators in and the one `lower()` emits nodes in.
    ops = ["eq", "ne", MEMBERSHIP]
    if prop.type.kind in ORDERABLE_KINDS:
        ops.extend(ORDERINGS)
    if prop.type.kind == "string" and searchable:
        ops.append(CONTAINS)
    return tuple(ops)


def property_schema(prop: Property, searchable: bool) -> dict:
    """The JSON Schema for one property's filter value: the bare spelling, or an operator object.

    An `anyOf` of two branches, which is the price of keeping v0's payloads valid, and it buys
    something back: the operator branch says *in the schema* which operators accept a null, so
    §5's "testable, not orderable" is a fact a client can read rather than one it discovers."""
    value = prop.type.json_schema()
    nullable = {"anyOf": [value, {"type": "null"}]}
    admits = operators(prop, searchable)
    props: dict[str, dict] = {
        op: (dict(nullable) if op in NULLABLE_OPS else dict(value))
        for op in admits
        if op not in (CONTAINS, MEMBERSHIP)
    }
    if MEMBERSHIP in admits:
        props[MEMBERSHIP] = {
            "type": "array",
            "items": dict(nullable),
            "minItems": 1,
            "description": f"{prop.name} equals any of these",
        }
    if CONTAINS in admits:
        props[CONTAINS] = {"type": "string", "description": "case-insensitive substring"}
    return {
        "anyOf": [
            {**value, "description": f"{_bare_meaning(prop, searchable)} on {prop.name}"},
            {
                "type": "object",
                "properties": props,
                "additionalProperties": False,
                "minProperties": 1,
                "description": f"comparisons on {prop.name}, ANDed",
            },
        ]
    }


def bare_operator(prop: Property, searchable: bool) -> str:
    """What a bare value means for this property — the v0 spelling, stated as sugar.

    Type-directed, because that is what v0 did: `search_customer({"name": "ac"})` has always been a
    substring match and `{"tier": "gold"}` has always been exact, and both descriptions are already
    in the generated schema. Rewriting either would change a shipped answer without erroring."""
    return CONTAINS if prop.type.kind == "string" and searchable else "eq"


def _bare_meaning(prop: Property, searchable: bool) -> str:
    return (
        "case-insensitive substring match"
        if bare_operator(prop, searchable) == CONTAINS
        else "exact match"
    )


def lower(
    obj: ObjectType,
    prop: Property,
    value: Any,
    alias: str,
    objects: Mapping[str, ObjectType],
) -> tuple[Comparison, ...]:
    """One property's filter value as `ir` comparison nodes, or a `FilterError` naming why not.

    Several nodes for one property, because several operators on one property is how a range is
    written and `Search.filters` is already a conjunction. They come back in `operators()` order
    rather than the caller's key order, so the SQL a filter compiles to does not depend on how a
    JSON object happened to be serialized."""
    searchable = prop.name in obj.searchable
    where = f"'{obj.api_name}.{prop.name}'"

    if value is None:
        raise FilterError(
            f"{where}: a bare null is not a filter value — JSON cannot tell a field left blank "
            f"from one meant as null, so answering the second reads exactly like answering the "
            f"first. Write {{\"{prop.name}\": {{\"eq\": null}}}} to select rows where "
            f"{prop.name} is null"
        )

    if not isinstance(value, dict):
        return (_comparison(obj, prop, bare_operator(prop, searchable), value, alias, objects),)

    allowed = operators(prop, searchable)
    if not value:
        raise FilterError(
            f"{where}: {{}} names no comparison — a filter with no operator says nothing about a "
            f"row. Drop it, or name one of: {', '.join(allowed)}"
        )

    # Every key checked before any value is read, so an unknown operator is reported as one rather
    # than as whatever its value failed to coerce to.
    for op in value:
        if op not in allowed:
            raise FilterError(_no_such_operator(obj, prop, op, allowed))
    # In `allowed` order rather than the caller's, so the SQL does not depend on key order.
    return tuple(
        _comparison(obj, prop, op, value[op], alias, objects) for op in allowed if op in value
    )


def _no_such_operator(obj: ObjectType, prop: Property, op: str, allowed: Sequence[str]) -> str:
    kind = prop.type.kind
    if op == CONTAINS and kind == "string":
        # The one refusal that is about the *spec* rather than the operator: substring is what
        # declaring a property searchable means, and `negotiate.py` checks the engine can do it
        # only for the properties that declared it.
        return (
            f"'{obj.api_name}.{prop.name}' is not declared searchable, so it matches exactly — "
            "'contains' is a substring match, which is what 'searchable' asks an engine for. Add "
            f"'{prop.name}' to this objectType's 'searchable' list, or filter with 'eq'"
        )
    if op == CONTAINS or op in FILTER_OPS:
        return (
            f"'{op}' does not apply to '{obj.api_name}.{prop.name}' ({kind}) — "
            f"available: {', '.join(allowed)}"
        )
    hint = suggest(op, {name: None for name in allowed}) or f"available: {', '.join(allowed)}"
    return f"'{op}' is not a filter operator — {hint}"


def _comparison(
    obj: ObjectType,
    prop: Property,
    op: str,
    value: Any,
    alias: str,
    objects: Mapping[str, ObjectType],
) -> Comparison:
    if op == MEMBERSHIP:
        return _membership(obj, prop, value, alias, objects)
    column = ColumnRef(alias=alias, column=prop.column)
    if op == CONTAINS:
        # Coerced like every other value rather than type-checked here: `contains` is only offered
        # on a string property, where `coerce_value` is `str(value)` — which is what v0's bare
        # spelling did, so an agent sending `42` for a substring search keeps getting `'42'`.
        text = _coerced(prop, value, objects, f"filter '{prop.name}.{CONTAINS}'")
        return Contains(alias=alias, column=prop.column, value=str(text))
    if value is None and op not in NULLABLE_OPS:
        # §5 refuses to order a null and `predicate._compare` refuses the same expression at load
        # time; a filter is written per call, so this is that refusal at the only time it has.
        raise FilterError(
            f"'{obj.api_name}.{prop.name}' {op} null is undecided for every row — null is a value "
            "you can test with 'eq' or 'ne', not one you can order"
        )
    return Compare(
        op=FILTER_OPS[op],
        left=column,
        right=Const(_coerced(prop, value, objects, f"filter '{prop.name}.{op}'")),
    )


def _membership(
    obj: ObjectType,
    prop: Property,
    value: Any,
    alias: str,
    objects: Mapping[str, ObjectType],
) -> In:
    """`{"in": [...]}` as one `ir.In`, or the two refusals that keep it meaning what it says.

    Every element is coerced exactly as the `eq` it stands for would be, including a null, which
    `coerce_value` passes through — so a list of one is the same node's worth of meaning as the
    `eq` spelling, which is the property the adapter's lowering then has to preserve in SQL."""
    where = f"'{obj.api_name}.{prop.name}'"
    if not isinstance(value, list):
        got = "null" if value is None else type(value).__name__
        raise FilterError(
            f"{where}: 'in' takes a list of values, got {got} — write "
            f'{{"{prop.name}": {{"in": ["a", "b"]}}}}, or use "eq" for a single value'
        )
    if not value:
        # The refusal's whole content is that the honest answer is indistinguishable from a
        # different question's honest answer — see the module docstring.
        raise FilterError(
            f"{where}: 'in' [] matches no row, and no caller can tell that from a search that "
            "found nothing. Name the values you meant, or drop the filter to match every row"
        )
    return In(
        alias=alias,
        column=prop.column,
        values=tuple(
            _coerced(prop, item, objects, f"filter '{prop.name}.{MEMBERSHIP}[{i}]'")
            for i, item in enumerate(value)
        ),
    )


def _coerced(prop: Property, value: Any, objects: Mapping[str, ObjectType], ctx: str) -> Any:
    try:
        return coerce_value(prop.type, value, objects, ctx)
    except ValueError as e:
        raise FilterError(str(e)) from e
