"""Ontology Model -> MCP tool set.

Nothing here is hand-authored per ontology: the tool names come from api names, the input schemas
from `PropType.json_schema()`, and the descriptions from the spec's own `description` fields. That
is the whole point of the spec being the single source of truth — the agent-facing contract is
*derived*, so it cannot drift from the model the resolver enforces.

The generated surface is fixed (spec §7):

    get_<object>      one object by primary key
    search_<object>   filter by declared `searchable` properties
    list_<object>     a page of objects
    match_<object>    rank by meaning against the declared `semantic:` property
    traverse          one hop along a declared link
    run_<action>      one declared action, against one row

There is deliberately no tool that accepts a predicate, a column, a table, or a query string.

**`match_` is a tool rather than an operator in the filter grammar**, and the rule that decides it is
the one `traverse` states below, read from the other side: a generic tool is right when the varying
element does not change the schema, and a *filter operator* is right when it decides rows. A
similarity clause decides nothing — it ranks — so putting it under `filter` would introduce `k` and
an ordering into a grammar that has neither, and would make `{similar} AND {tier: gold}` a question
with two answers. `search` is also a word already spent on rows, the same discipline `filters.py`
records about `contains`.

**Two argument namespaces, and they never mix.** Names that come from the spec's vocabulary live
inside a nested object; names Loom chose live at the top level. `search_<object>` was already built
this way — declared property filters under `filter`, `limit`/`offset` beside it — and
`run_<action>` follows it: declared parameters under `parameters`, `dryRun` beside it. Stating the
rule rather than repeating the shape is what makes the collision impossible: an action may declare
a parameter called `dryRun`, or `limit`, or `filter`, and none of them can shadow an argument Loom
means something by. `get_<object>` and `traverse` are the same rule seen from the other side — their
top-level names (`key`, `objectType`, `link`) are Loom's words, and only the *types* behind them come
from the spec.

Typed filters put Loom's words *below* a spec name for the first time — `filter: {salesDate: {gte:
…}}` — and the rule holds because it was never "Loom's vocabulary appears once". It is that **each
level of the argument tree belongs entirely to one vocabulary, and they alternate**: top level
Loom's, `filter` the spec's, per-property Loom's again. A property name never appears where an
operator does, so nothing can shadow anything, and a spec may declare a property called `gte`.

`match_`'s `via` is where that rule earns its keep a second time, and it sharpens it. The spec has
*two* vocabularies, not one — link names and property names — and they must not share a level
either: a `placedBy.tier` key inside `filter` would make an ontology with a link and a property of
the same name unable to say which one it meant. So `via` is a top-level argument of its own, and
what alternates is not Loom/spec but **one namespace per level**: `via` Loom's, the link name the
spec's links, the property name the spec's properties, the operator Loom's again.

**Where the read tools and the write tools differ, and where they don't.** A `run_` tool takes a
runtime instead of a resolver, because a modify must see the whole physical row and the resolver
projects one down to declared properties. Everything else is the same bargain: the name comes from
the api name, the schema from the declared parameter types, the description from the spec's own
`description`, and the result is `ActionResult.as_json()` serialized — a shape the runtime defined,
not one this layer composes.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .. import filters
from .._shape import suggest
from ..auth import current_principal
from ..governance import PolicyProgram
from ..model import Action, ObjectType
from ..resolver import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Resolver

if TYPE_CHECKING:
    from ..action import ActionRuntime
    from ..embed.match import Matcher

TRAVERSE_TOOL = "traverse"

TEXT_ARG = "text"
"""`match_`'s one required argument, and it is Loom's word at the top level like every other one.

What a caller passes is *not* a value of the semantic property — it is the question, in the caller's
own words, which is the entire reason this plane exists. Naming it after the property would say the
opposite."""

VIA_ARG = "via"
"""`match_`'s cross-object narrowing, keyed by **link name** — and that is why it is a top-level
argument rather than a dotted key inside `filter`.

`filter` is the spec's vocabulary one level down, and it is shared with `search_` through
`_filterable`: its keys are property names. A `placedBy.tier` key inside it would put link names and
property names in one namespace, which §7's rule exists to prevent — the alternation is *top level
Loom's, `filter` the spec's, per-property Loom's again*, and a link name is a third thing. So it gets
its own level, and the tree alternates the same way underneath it: `via` Loom's, link name the
spec's, then a filter object that is the spec's again keyed by property, then operators."""

PARAMETERS_ARG = "parameters"
DRY_RUN_ARG = "dryRun"
RESERVED_RUN_ARGS = (PARAMETERS_ARG, DRY_RUN_ARG)
"""The complete top level of a `run_` tool. Nothing derived from a spec ever appears here — see the
module docstring — so this tuple is also the assertion that a declared parameter cannot widen the
surface with a name Loom reserves."""

_PAGE_SCHEMA = {
    "limit": {
        "type": "integer",
        "minimum": 1,
        "maximum": MAX_PAGE_SIZE,
        # `maximum` is enforced by `Resolver._page_size` and not only advertised here — a larger
        # number is refused rather than quietly clamped, so the `limit` in a page envelope below is
        # always the page size the caller actually got.
        "description": f"max rows to return (default {DEFAULT_PAGE_SIZE}, maximum {MAX_PAGE_SIZE})",
    },
    "offset": {"type": "integer", "minimum": 0, "description": "rows to skip, for paging"},
}


@dataclass(frozen=True)
class ToolSpec:
    """One generated tool. `handler` takes the decoded arguments and returns a JSON-safe result."""

    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict], Any]

    def argument_refusal(self, arguments: Mapping[str, Any]) -> str | None:
        """Why this argument list is not one this tool accepts, or None.

        **The top level, and deliberately only the top level**, because that is the one level of the
        argument tree nothing else owns. Below it the vocabularies alternate (see the module
        docstring) and each nested level already has an enforcer with a better sentence than a
        generic one: a `filter` key no property answers to is refused by `Resolver._filters` naming
        the object type and its properties, and a `parameters` key no parameter answers to is refused
        by the action runtime as `unknown_parameter`, inside a typed `ActionResult`. Recursing here
        would replace both with something vaguer, so it stops where the ownership starts.

        What it buys is the two failures the top level had instead. An **unknown** argument was
        silently dropped — `search_daily_sales_performance(salesDate={...})`, the shape you get by
        forgetting the `filter` nesting, ran as an unfiltered search and answered `isError: false`
        with the whole table. A surface that narrows on request must not widen on a typo, and that is
        the widest a typo can get. A **missing required** argument reached the handler and came back
        as `KeyError: 'key'` — a Python exception where every other refusal in this system is a
        written sentence naming what to do instead.

        Checked against `input_schema` rather than against a second list, so the enforcement cannot
        drift from what `on_list_tools` advertises: `additionalProperties: False` is what every
        generated schema already claims, and this is that claim becoming true."""
        if self.input_schema.get("additionalProperties") is not False:  # pragma: no cover - all are
            return None
        accepted = self.input_schema.get("properties") or {}
        unknown = [k for k in arguments if k not in accepted]
        if unknown:
            hint = suggest(unknown[0], accepted) if len(unknown) == 1 else None
            named = ", ".join(f"'{k}'" for k in unknown)
            what = "is not an argument" if len(unknown) == 1 else "are not arguments"
            return (
                f"{named} {what} of '{self.name}' — {hint or 'accepted: ' + (', '.join(accepted) or 'none')}"
            )
        missing = [k for k in self.input_schema.get("required") or () if k not in arguments]
        if missing:
            return f"'{self.name}' requires {', '.join(repr(k) for k in missing)}"
        return None


def snake_case(api_name: str) -> str:
    """`Customer` -> `customer`, `PurchaseOrder` -> `purchase_order`.

    MCP tool names are identifiers agents type, so they get the conventional spelling rather than
    the api name verbatim."""
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", api_name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def json_safe(value: Any) -> Any:
    """Coerce engine-native values to something JSON can carry.

    Decimal becomes a string rather than a float — the whole reason a spec declares
    `decimal(12,2)` is that the value must not go through binary floating point."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def _reading(resolver: Resolver, program: PolicyProgram | None) -> Resolver:
    """The resolver for the call in flight — this one, or this one governed for its caller.

    **This is the only line in the tool layer that knows a policy can name a caller**, and what it
    passes down is a *decided* `PolicySet`, never an identity: `select` reads the principal, answers
    every guard and folds every `principal.` reference into a literal, and what comes back names
    nobody. `program is None` is a resolver that was already decided — which is every direct
    construction of one, and every test that builds a tool set from a resolver alone.

    For a program with nothing conditional this returns the *same object* it was given, so a
    deployment that predates this milestone is provably unchanged rather than argued to be."""
    if program is None:
        return resolver
    return resolver.governed_by(program.select(current_principal()))


def build_tools(
    resolver: Resolver,
    runtime: ActionRuntime | None = None,
    actor: str | None = None,
    program: PolicyProgram | None = None,
    matcher: Matcher | None = None,
) -> list[ToolSpec]:
    """Introspect the ontology into the tool set this deployment exposes.

    The read tools always. The `run_` tools only when a runtime is supplied, which is `loom serve`'s
    way of saying `mcp.writes` is on — the surface is what the deployment permits, not what the spec
    declares, and the banner counts what was built rather than what could have been.

    **A `match_` tool needs both halves, and the asymmetry with a mask is the point.** The spec
    declares `semantic:` and the deployment configures `mcp.embedding`; absent the second there is no
    tool, exactly as `mcp.writes: false` exposes no action. That is *not* the same thing a policy
    does: a mask over the semantic property is refused before this deployment starts, because §7 says
    no deployment gets to be the one that makes a tool disappear. A deployment configuring no
    provider is not withholding a tool it could serve — it has no model, so there is no ranking to
    withhold. `matcher` is `None` in that case and in the case of a spec that declares nothing, and
    building it costs no model load and no catalog read: see `bind_matching`.

    **A mask is read here, once, and that stayed true when a principal arrived.** Masks are resolved
    into the schemas and descriptions at build time because they cannot change between two calls of a
    process. M5 wrote that as a consequence of policies being deployment-scoped and predicted that
    "the day an attested principal arrives per call, this is one of the two places that stops being
    true — and the tool set becomes something assembled per caller rather than per process."

    **That prediction was wrong, and the milestone that could have made it true is the one that
    closed it off instead.** A mask *announces itself* — here, in the `filter` schema, and in
    `masked` on every result — so conditioning one on the caller would make the tool set a function
    of the caller rather than of the spec (§7). `governance.parse_policies` refuses `mask:` beside
    `when:` for exactly that reason, so the announcement half of a policy is per deployment forever
    and this build happens once. What arrives per call is `program`, and it reaches only the
    *handlers*, where it changes which rows come back and nothing about the tool."""
    tools: list[ToolSpec] = []
    for obj in resolver.ontology.object_types.values():
        tools.append(_get_tool(resolver, obj, program))
        tools.append(_search_tool(resolver, obj, program))
        tools.append(_list_tool(resolver, obj, program))
        if matcher is not None and obj.api_name in matcher.stores:
            tools.append(_match_tool(resolver, obj, matcher, program))
    if resolver.ontology.link_types:
        tools.append(_traverse_tool(resolver, program))
    if runtime is not None:
        for action in resolver.ontology.actions.values():
            tools.append(_run_tool(runtime, action, actor, program))
    return tools


# ---- status ---------------------------------------------------------------------

_STATUS_LABEL = {"deprecated": "DEPRECATED", "experimental": "EXPERIMENTAL"}


def _described(status: str, text: str) -> str:
    """A tool description, carrying the spec's `status` when it is not `active`.

    `status` is on every objectType, linkType and action, and until now nothing read it. It is read
    here, and the choice is to **label rather than hide** — for a reason that is about Loom's shape
    rather than about taste. Hiding a deprecated action would leave `loom run` able to run something
    the tool surface denies, which is the exact back door `loom run` exists to not be; the only way
    to hide it honestly would be to make the runtime refuse it, turning a surface label into a
    kill switch and making `status: deprecated` mean "broken". It is also the form that works on the
    caller this surface is for: an agent reads descriptions afresh every session and has no memory of
    a deprecation notice, so the notice has to be in the thing it reads.

    Narrowing a surface for a real deployment is a different question with its own answers —
    `mcp.writes` for the write half, §6's governance policies for the rest."""
    label = _STATUS_LABEL.get(status)
    return f"{label} — {text}" if label else text


def _withheld(masked: Sequence[str]) -> str:
    """The sentence a mask adds to a tool description, or nothing at all.

    A mask announces itself, and this is where — an agent reads descriptions afresh every session,
    the same reason a deprecation is labelled rather than hidden. What it announces is only what the
    surface already said: the property names are in the spec, so naming them as withheld tells a
    caller nothing the schema did not. A row predicate will add no sentence here, and the asymmetry
    is the rule in `governance.py`: the schema is public, the data is not."""
    if not masked:
        return ""
    return f" Withheld by governance policy: {', '.join(masked)}."


# ---- per-object tools ----------------------------------------------------------


def _subject(obj: ObjectType) -> str:
    """The phrase describing an object type in generated tool descriptions."""
    return f"{obj.display_name or obj.api_name}" + (f" — {obj.description}" if obj.description else "")


def _get_tool(resolver: Resolver, obj: ObjectType, program: PolicyProgram | None = None) -> ToolSpec:
    pk = obj.pk_property
    masked = resolver.masked(obj.api_name)
    schema = {
        "type": "object",
        "properties": {"key": {**pk.type.json_schema(), "description": f"the {pk.name} of the {obj.api_name}"}},
        "required": ["key"],
        "additionalProperties": False,
    }

    def handler(args: dict) -> Any:
        row = _reading(resolver, program).get(obj.api_name, args["key"])
        return {
            "objectType": obj.api_name,
            "key": json_safe(args["key"]),
            "found": row is not None,
            "object": json_safe(row) if row is not None else None,
            "masked": list(masked),
        }

    return ToolSpec(
        name=f"get_{snake_case(obj.api_name)}",
        description=_described(obj.status, f"Fetch one {_subject(obj)} by its {pk.name}.") + _withheld(masked),
        input_schema=schema,
        handler=handler,
    )


def _filterable(obj: ObjectType, masked: Sequence[str]) -> dict[str, Any]:
    """The properties a `filter` argument may name: declared searchable, minus what a policy hides.

    A masked property leaves the filter schema as well as the projection. The resolver refuses a
    filter on one whatever the schema says — that is the enforcement, and it is below MCP where
    `loom query` meets it too — but advertising an argument that fails on every call is the thing
    `cmd_serve` already refuses to do when an engine cannot serve a tool. Subtracting from the
    surface, never adding to it: the rule from `governance.py`, seen at the surface.

    Shared by `search_` and `match_`, because "which properties may be filtered" is one question with
    one answer — a ranked read that narrowed on a different set would be a second surface for a
    policy to be read off."""
    return {
        name: obj.properties[name]
        for name in obj.searchable
        if name in obj.properties and name not in masked
    }


def _filter_arg(filterable: Mapping[str, Any]) -> dict:
    """The `filter` argument's schema.

    Generated from the property type and `searchable`, and from nothing else — the same function
    `Resolver._filters` enforces against, so the surface cannot advertise an operator the resolver
    refuses or hide one it accepts. §7's namespace rule survives one level deeper than it was
    written: operator keys are Loom's vocabulary *below* a property name, never beside one, so an
    ontology may declare a property called `gte` without shadowing anything."""
    return {
        "type": "object",
        "properties": {
            name: filters.property_schema(prop, searchable=True)
            for name, prop in filterable.items()
        },
        "additionalProperties": False,
        "description": "property filters, ANDed together",
    }


def _paged(args: dict, count: int) -> dict:
    """The four keys every paged envelope carries, from the arguments that produced it.

    One function rather than three copies, which is what stops `hasMore` from being computed against
    a different cap in one of them: an agent has no other way to tell "that's everything" from "the
    page filled up", so the three surfaces that page must agree about when it is true.

    `hasMore` compares against the caller's own number because that number is now either the page
    size or an error — `Resolver._page_size` refuses a `limit` above `MAX_PAGE_SIZE` rather than
    clamping to it. While it clamped, this key was `count == min(limit, MAX_PAGE_SIZE)` and `limit`
    beside it echoed what the caller asked for, so an envelope could report a page that was never
    served and a client paging with `offset += limit` stepped past everything in between."""
    limit = args.get("limit") or DEFAULT_PAGE_SIZE
    return {
        "count": count,
        "limit": limit,
        "offset": args.get("offset", 0),
        "hasMore": count == limit,
    }


def _search_tool(resolver: Resolver, obj: ObjectType, program: PolicyProgram | None = None) -> ToolSpec:
    masked = resolver.masked(obj.api_name)
    filterable = _filterable(obj, masked)

    schema = {
        "type": "object",
        "properties": {"filter": _filter_arg(filterable), **_PAGE_SCHEMA},
        "additionalProperties": False,
    }

    def handler(args: dict) -> Any:
        rows = _reading(resolver, program).search(
            obj.api_name,
            args.get("filter") or {},
            limit=args.get("limit"),
            offset=args.get("offset", 0),
        )
        return _page(obj, rows, args, masked)

    if filterable:
        searchable = ", ".join(filterable)
    elif any(name in masked for name in obj.searchable):
        # Not "no properties are declared searchable", which would be false: they are declared, and
        # this deployment withholds them. A description that misreports the spec is worse than one
        # that reports a policy.
        searchable = "nothing — every searchable property is withheld here"
    else:
        searchable = "no properties are declared searchable"
    return ToolSpec(
        name=f"search_{snake_case(obj.api_name)}",
        description=_described(obj.status, f"Search {_subject(obj)} by {searchable}.") + _withheld(masked),
        input_schema=schema,
        handler=handler,
    )


def _list_tool(resolver: Resolver, obj: ObjectType, program: PolicyProgram | None = None) -> ToolSpec:
    masked = resolver.masked(obj.api_name)
    schema = {"type": "object", "properties": dict(_PAGE_SCHEMA), "additionalProperties": False}

    def handler(args: dict) -> Any:
        rows = _reading(resolver, program).list(
            obj.api_name, limit=args.get("limit"), offset=args.get("offset", 0)
        )
        return _page(obj, rows, args, masked)

    return ToolSpec(
        name=f"list_{snake_case(obj.api_name)}",
        description=_described(obj.status, f"List {_subject(obj)}, ordered by {obj.primary_key}.")
        + _withheld(masked),
        input_schema=schema,
        handler=handler,
    )


def _page(obj: ObjectType, rows: list[dict], args: dict, masked: Sequence[str] = ()) -> dict:
    return {
        "objectType": obj.api_name,
        **_paged(args, len(rows)),
        # Always present, empty when nothing is withheld. A key that appears only under a policy
        # would make "this deployment governs nothing" and "this Loom is too old to say"
        # indistinguishable, which is the one thing an envelope reporting a mask must not do.
        "masked": list(masked),
        "objects": json_safe(rows),
    }


# ---- match ---------------------------------------------------------------------


def _via_arg(resolver: Resolver, obj: ObjectType) -> dict | None:
    """The `via` argument's schema — one key per link out of this type — or None for a type with
    none.

    **Omitted rather than advertised empty**, which is the opposite of what `filter` does and for a
    reason the two do not share. A `filter` with no filterable properties still describes a real
    argument: the search takes one, and a deployment that withheld every property is a fact the
    description says out loud. A `via` on a type no link leaves has nothing it could ever accept, so
    advertising `{}` would be an argument whose only legal value says nothing — and `argument_refusal`
    then names it correctly as not an argument of this tool.

    Each hop's value is the far type's own `filter` schema, built by the same two functions the far
    type's own tools are built from. That is what carries a mask one join out without a second rule:
    a property withheld on `Customer` is absent from `via.placedBy` exactly as it is absent from
    `search_customer`'s `filter`, and `Resolver._filters` refuses it there for the same reason it
    refuses it here."""
    directions = resolver.links_of(obj.api_name)
    if not directions:
        return None
    hops: dict[str, Any] = {}
    for d in directions:
        far = resolver.ontology.object_types[d.target_object_type]
        schema = _filter_arg(_filterable(far, resolver.masked(far.api_name)))
        schema["description"] = (
            f"{far.api_name} filters, ANDed — keeps a {obj.api_name} only if at least one "
            f"{far.api_name} it links to by '{d.name}' matches. {{}} keeps the ones that have any"
        )
        hops[d.name] = schema
    return {
        "type": "object",
        "properties": hops,
        "additionalProperties": False,
        "description": (
            "narrow by the objects on the other end of a declared link, ANDed with `filter`"
        ),
    }


def _match_tool(
    resolver: Resolver, obj: ObjectType, matcher: Matcher, program: PolicyProgram | None = None
) -> ToolSpec:
    """One object type's ranked read: the question `contains` cannot ask.

    **The envelope's elements are `matches`, not `objects`, and that is a claim rather than a
    synonym.** Every other read returns objects; this returns a score paired with one, so calling
    the list `objects` would make an agent that unpacked it the same way get a dict with a `score`
    key it did not expect. The score sits *beside* the object for §7's namespace reason — see
    `resolver.Ranked` — and it is what a caller needs to tell a near miss from the best of a bad set,
    which a rank alone cannot say.

    **`embeddedAsOf` is in and a count of unembedded rows is not.** The count needs an anti-join over
    the admitted set on every call, and the deciding reason is that an agent cannot *act* on it — it
    cannot wait and it cannot trigger a reconcile, and this surface says things a caller can do
    something about. What that gives up is real and named here rather than discovered: `match_` can
    silently omit a row that exists, so the honesty moves to the operator and the reconcile has to be
    reliable rather than best-effort. `loom embed`'s output and the serve banner are where that count
    goes.

    **The tool is built from the spec and the deployment's provider, never from the lake.** Whether
    anything has actually been embedded is a fact that changes while the process runs, so it is
    answered per call — as a refusal, not an empty page. `Matcher.match` says why.

    **`via` is what makes the ranking answerable.** Without it this tool can rank orders by meaning
    and cannot say *belonging to a gold-tier customer*, which is the query anyone actually has — and
    the two halves are not interchangeable, because a ranking is over the rows that survive
    narrowing. It is announced in the description as well as the schema for the reason every other
    affordance is: an agent reads descriptions afresh every session, and a nested argument it does
    not know to look for is one it will not use."""
    masked = resolver.masked(obj.api_name)
    prop = obj.semantic_property
    assert prop is not None  # `matcher.stores` is keyed by the types that declare one
    filterable = _filterable(obj, masked)
    hops = _via_arg(resolver, obj)

    schema = {
        "type": "object",
        "properties": {
            TEXT_ARG: {
                "type": "string",
                "minLength": 1,
                "description": (
                    f"what you are looking for, in your own words — ranked against "
                    f"{obj.api_name}.{prop.name} by meaning, not by the words matching"
                ),
            },
            "filter": _filter_arg(filterable),
            **({VIA_ARG: hops} if hops else {}),
            **_PAGE_SCHEMA,
        },
        "required": [TEXT_ARG],
        "additionalProperties": False,
    }

    def handler(args: dict) -> Any:
        result = matcher.match(
            _reading(resolver, program),
            obj.api_name,
            args[TEXT_ARG],
            args.get("filter") or {},
            args.get(VIA_ARG) or {},
            limit=args.get("limit"),
            offset=args.get("offset", 0),
        )
        return {
            "objectType": obj.api_name,
            "property": result.property,
            # Named on every call, because a similarity is only meaningful against the model that
            # produced both sides of it — and because a deployment mid-model-swap ranks nothing, so
            # an empty page that names a model is diagnosable where a bare empty page is not.
            "model": result.model,
            "embeddedAsOf": json_safe(result.embedded_as_of),
            **_paged(args, len(result.matches)),
            "masked": list(masked),
            "matches": [
                {"score": m.score, "object": json_safe(m.object)} for m in result.matches
            ],
        }

    narrowed = (
        f" Narrow first with `filter` ({', '.join(filterable)}) — the filters apply *before* the "
        "ranking, so a filtered call ranks fewer rows rather than re-ranking the ones you kept."
        if filterable
        else ""
    )
    crossed = (
        f" Narrow by a linked object with `via` ({', '.join(hops['properties'])}) — "
        f"`via: {{{next(iter(hops['properties']))}: {{...}}}}` keeps a {obj.api_name} only if at "
        "least one object on the other end of that link matches, and an empty `{}` keeps the ones "
        "that have any."
        if hops
        else ""
    )
    return ToolSpec(
        name=f"match_{snake_case(obj.api_name)}",
        description=_described(
            obj.status,
            f"Rank {_subject(obj)} by how close {prop.name} is in meaning to your words. Use this "
            f"when the answer is in the text but you do not know the data's wording — "
            f"search_{snake_case(obj.api_name)} finds rows that *say* a word, this finds rows that "
            f"*mean* one." + narrowed + crossed + " Each result carries a `score` beside the object, nearest "
            "first: it is a cosine similarity, comparable between rows and between calls of this "
            "deployment and meaningless against any other model. Only rows that have been embedded "
            "can be ranked — `embeddedAsOf` says how current the oldest of these is.",
        )
        + _withheld(masked),
        input_schema=schema,
        handler=handler,
    )


# ---- traverse ------------------------------------------------------------------


def _traverse_tool(resolver: Resolver, program: PolicyProgram | None = None) -> ToolSpec:
    """One generic tool rather than one per link — and the rule that says so is narrower than it
    first looked.

    It used to read: *the link name is data, and enumerating object-type x link as separate tools
    would grow the surface an agent has to read for no gain.* That is true of an action name too, so
    as written it decides `run_<action>` the wrong way. The rule it was reaching for is about the
    **schema**, not the name:

        A generic tool is right exactly when the varying element does not change the input schema.

    For `traverse` it does not — `(objectType, key, link, page)` is the same tuple for every link in
    every ontology, and `link` is a string drawn from an enumerated set. For an action it *is* the
    schema: `upgradeTier` takes an objectRef and an enum of two values, `recordOrder` takes a string,
    an objectRef and a `decimal(12,2)`. Collapsing those into one `run(action, params)` means typing
    `params` as a free-form object, which would be the only place in the generated surface where an
    agent is handed an untyped bag and "declared types are honored on the way in" stops being
    structural. So the surface is per action, and the cost is real and paid deliberately: a spec with
    forty actions generates forty tools. What does not fix that is detyping them; what does is
    exposing fewer (`mcp.writes`, and eventually §6's policies).

    The status of a link goes in the route catalogue rather than in a prefix, for the same reason
    the tool is generic: one tool spans many links, and a non-active link is one route among them,
    not a property of the verb."""
    routes: dict[str, list[str]] = {}
    for name in resolver.ontology.object_types:
        directions = resolver.links_of(name)
        if directions:
            routes[name] = [
                f"{d.name} -> {d.target_object_type} ({d.link.cardinality}"
                + (f", {d.link.status}" if d.link.status != "active" else "")
                + ")"
                for d in directions
            ]
    catalogue = "; ".join(f"from {ot}: {', '.join(links)}" for ot, links in routes.items())

    schema = {
        "type": "object",
        "properties": {
            "objectType": {
                "type": "string",
                "enum": sorted(routes),
                "description": "the object type you are starting from",
            },
            # Generic across object types, so the key can be either spelling; the resolver coerces
            # it to the declared primary-key type once the object type is known.
            "key": {
                "type": ["string", "integer"],
                "description": "primary key of the object you are starting from",
            },
            "link": {"type": "string", "description": f"the link to follow. Available: {catalogue}"},
            **_PAGE_SCHEMA,
        },
        "required": ["objectType", "key", "link"],
        "additionalProperties": False,
    }

    def handler(args: dict) -> Any:
        direction = resolver.link_direction(args["objectType"], args["link"])
        rows = _reading(resolver, program).traverse(
            args["objectType"],
            args["key"],
            args["link"],
            limit=args.get("limit"),
            offset=args.get("offset", 0),
        )
        return {
            "objectType": args["objectType"],
            "key": json_safe(args["key"]),
            "link": args["link"],
            "targetObjectType": direction.target_object_type,
            "cardinality": direction.link.cardinality,
            **_paged(args, len(rows)),
            # The target's mask, not the source's: a traverse projects the objects at the other end,
            # so what is withheld here is a fact about where you landed. Read per call rather than
            # bound at build like the per-object tools', because this one tool spans every route.
            "masked": list(resolver.masked(direction.target_object_type)),
            "objects": json_safe(rows),
        }

    return ToolSpec(
        name=TRAVERSE_TOOL,
        description=(
            "Follow one declared link from an object to the objects on the other end. "
            f"Available routes: {catalogue}."
        ),
        input_schema=schema,
        handler=handler,
    )


# ---- run ------------------------------------------------------------------------


def _run_tool(
    runtime: ActionRuntime, action: Action, actor: str | None, program: PolicyProgram | None = None
) -> ToolSpec:
    """One declared action as one tool.

    Three things about it are decisions rather than defaults.

    **`parameters` is nested and `dryRun` sits beside it.** The module docstring carries the rule;
    the consequence worth naming here is that it is what makes `dryRun` safe to add at all. A flat
    `run_upgrade_tier(customer, newTier, dryRun)` reserves the name `dryRun` out of the spec's own
    vocabulary, so an ontology that declares a parameter by that name either loses it or fails to
    serve — a spec that validates and cannot be exposed is the worst seam available. Nested, the
    question cannot come up.

    **`dryRun` is an inspection verb, not an approval step.** It runs the first three of the four
    steps and stops before the write, which is exactly what `loom run` prints above its `y/N`. What
    it deliberately is *not* is a confirmation: nothing links a preview to a later run, no state is
    carried between them, and a previewed result holds no row and confers no permission — the next
    run does its own read and asserts *that* one (§4.1, "the prompt is outside the window"). That is
    also why an MCP caller can have this at all. §4.1 settled the concurrency design on the fact that
    `run_<action>` has no prompt; a preview that promised anything about the run after it would be
    the design that decision rejected. What approval there is happens where the human is — in the
    client's own tool-approval UI — and Loom's part is to make the shape of the change knowable
    before the write, which is what this is. Without it, `previewed` would be a status no MCP caller
    could ever see and an agent's only way to learn what an action does would be to do it.

    **The `actor` is bound here, once, from `mcp.actor`. The `principal` is not, and cannot be.**
    The runtime takes both per call and invents neither. `actor` is a string an operator declared
    about a deployment, so binding it into this closure loses nothing. A principal is a fact about
    *this exchange*: it is read inside the handler, on every call, from the context the transport
    populated. It is read for two things now — the name the edit log records, and the `PolicySet`
    this run is governed by — and the tool is unmoved by both: the schemas and the descriptions are
    still a function of the spec, exactly as §7 says, because a mask cannot be conditioned and a row
    filter announces nothing.
    """
    target = runtime.ontology.object_types[action.target_object_type]
    pk = target.pk_property

    props: dict[str, Any] = {}
    required: list[str] = []
    for name, param in action.parameters.items():
        fragment = dict(param.type.json_schema())
        if param.description:
            # The spec author's sentence wins over the type's generated one — `objectRef` and
            # `decimal` both generate a description, and neither knows what the parameter is for.
            fragment["description"] = param.description
        if param.default is not None:
            fragment["default"] = json_safe(param.default)
        props[name] = fragment
        if param.required:
            required.append(name)

    parameters_schema: dict[str, Any] = {
        "type": "object",
        "properties": props,
        "additionalProperties": False,
        "description": f"the declared parameters of {action.api_name}",
    }
    if required:
        parameters_schema["required"] = required

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            PARAMETERS_ARG: parameters_schema,
            DRY_RUN_ARG: {
                "type": "boolean",
                "description": (
                    "bind, read and validate, then stop before the write and report what would "
                    "have happened (status 'previewed'). Nothing is held: a run after a preview "
                    "reads again"
                ),
            },
        },
        "additionalProperties": False,
    }
    if required:
        # Required only when something inside it is. An action whose parameters all have defaults
        # can be called with no arguments at all, and saying otherwise would be the schema
        # contradicting the spec it was generated from.
        schema["required"] = [PARAMETERS_ARG]

    def handler(args: dict) -> Any:
        # The `actor` is closed over and the `principal` is read *now*, and the asymmetry is the
        # whole of what M6's first slice changed here. One is true about the deployment that built
        # this closure; the other is true about the exchange in flight, so it cannot be bound at
        # build time and must not be cached across calls. `current_principal()` is `None` on every
        # surface that cannot attest, which is every surface this closure served before today.
        caller = current_principal()
        # One read of the principal, two readers of it, and they take it in different shapes: the
        # edit log records a *name* (`label`), while governance needs the claims to decide a policy
        # with — and `select` gives back a decided set, so what reaches `run` is still no identity.
        result = runtime.governed_by(program.select(caller) if program else runtime.policies).run(
            action.api_name,
            args.get(PARAMETERS_ARG) or {},
            actor=actor,
            principal=None if caller is None else caller.label,
            dry_run=bool(args.get(DRY_RUN_ARG, False)),
        )
        # Serialized, not composed. `ActionResult` is the shape the runtime settled on for exactly
        # this caller; `json_safe` is the only thing this layer adds, because it is the layer that
        # knows a Decimal must not go out through a float.
        return json_safe(result.as_json())

    return ToolSpec(
        name=f"run_{snake_case(action.api_name)}",
        # The target's mask is announced here too: `before` and `after` come back as declared
        # properties minus what a policy withholds, so an agent reading this description is reading
        # the shape of the result it will get. It can never name a property this action touches —
        # that pairing is refused before a deployment starts — so the sentence only ever describes
        # neighbours on the row.
        description=_described(action.status, _run_description(action, target, pk.name))
        + _withheld(runtime.policies.masked(target.api_name)),
        input_schema=schema,
        handler=handler,
    )


def _run_description(action: Action, target: ObjectType, key_name: str) -> str:
    """What the action does, and what to do with what comes back.

    The second half is here because the input schema cannot carry it. A `run_` result is a typed
    `ActionResult`, and the protocol's `isError` is deliberately false for every run that reached
    the runtime (see `LoomMCPServer.call`) — so the agent has to be told, in the one place it reads,
    that the outcome is in the payload and which field carries it."""
    verb = {"create": "Creates", "modify": "Modifies", "delete": "Deletes"}[action.operation]
    subject = (action.description or f"The declared action {action.api_name}").rstrip(". ")
    return (
        f"{subject}. {verb} exactly one "
        f"{target.display_name or target.api_name}, addressed by {key_name}. "
        "Returns a typed result rather than a protocol error — branch on `status` "
        "('applied' the write committed · 'previewed' dryRun, nothing written · 'refused' a "
        "precondition said no and nothing was changed · 'failed' the write itself failed and it is "
        "unknown whether the row changed), then on `failures[].code`. Retry only where "
        "`failures[].retryable` is true, which is only ever 'conflict'."
    )
