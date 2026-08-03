"""Ontology Model -> MCP tool set.

Nothing here is hand-authored per ontology: the tool names come from api names, the input schemas
from `PropType.json_schema()`, and the descriptions from the spec's own `description` fields. That
is the whole point of the spec being the single source of truth — the agent-facing contract is
*derived*, so it cannot drift from the model the resolver enforces.

The generated surface is fixed (spec §7):

    get_<object>      one object by primary key
    search_<object>   filter by declared `searchable` properties
    list_<object>     a page of objects
    traverse          one hop along a declared link
    run_<action>      one declared action, against one row

There is deliberately no tool that accepts a predicate, a column, a table, or a query string.

**Two argument namespaces, and they never mix.** Names that come from the spec's vocabulary live
inside a nested object; names Loom chose live at the top level. `search_<object>` was already built
this way — declared property filters under `filter`, `limit`/`offset` beside it — and
`run_<action>` follows it: declared parameters under `parameters`, `dryRun` beside it. Stating the
rule rather than repeating the shape is what makes the collision impossible: an action may declare
a parameter called `dryRun`, or `limit`, or `filter`, and none of them can shadow an argument Loom
means something by. `get_<object>` and `traverse` are the same rule seen from the other side — their
top-level names (`key`, `objectType`, `link`) are Loom's words, and only the *types* behind them come
from the spec.

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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from ..auth import current_principal
from ..model import Action, ObjectType
from ..resolver import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Resolver

if TYPE_CHECKING:
    from ..action import ActionRuntime

TRAVERSE_TOOL = "traverse"

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
        "description": f"max rows to return (default {DEFAULT_PAGE_SIZE}, hard cap {MAX_PAGE_SIZE})",
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


def build_tools(
    resolver: Resolver,
    runtime: ActionRuntime | None = None,
    actor: str | None = None,
) -> list[ToolSpec]:
    """Introspect the ontology into the tool set this deployment exposes.

    The read tools always. The `run_` tools only when a runtime is supplied, which is `loom serve`'s
    way of saying `mcp.writes` is on — the surface is what the deployment permits, not what the spec
    declares, and the banner counts what was built rather than what could have been.

    **A policy is read here, once, and that is a claim with a lifetime.** Masks are resolved into
    the schemas and descriptions at build time because they cannot change between two calls of a
    process: policies are deployment-scoped, so there is exactly one answer for every caller of this
    server (`governance.py` says why none of them names a principal). The day an attested principal
    arrives per call, this is one of the two places that stops being true — the other being the one
    `Resolver` and one `ActionRuntime` `build_server` holds — and the tool set becomes something
    assembled per caller rather than per process."""
    tools: list[ToolSpec] = []
    for obj in resolver.ontology.object_types.values():
        tools.append(_get_tool(resolver, obj))
        tools.append(_search_tool(resolver, obj))
        tools.append(_list_tool(resolver, obj))
    if resolver.ontology.link_types:
        tools.append(_traverse_tool(resolver))
    if runtime is not None:
        for action in resolver.ontology.actions.values():
            tools.append(_run_tool(runtime, action, actor))
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


def _get_tool(resolver: Resolver, obj: ObjectType) -> ToolSpec:
    pk = obj.pk_property
    masked = resolver.masked(obj.api_name)
    schema = {
        "type": "object",
        "properties": {"key": {**pk.type.json_schema(), "description": f"the {pk.name} of the {obj.api_name}"}},
        "required": ["key"],
        "additionalProperties": False,
    }

    def handler(args: dict) -> Any:
        row = resolver.get(obj.api_name, args["key"])
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


def _search_tool(resolver: Resolver, obj: ObjectType) -> ToolSpec:
    masked = resolver.masked(obj.api_name)
    # A masked property leaves the filter schema as well as the projection. The resolver refuses a
    # filter on one whatever the schema says — that is the enforcement, and it is below MCP where
    # `loom query` meets it too — but advertising an argument that fails on every call is the thing
    # `cmd_serve` already refuses to do when an engine cannot serve a tool. Subtracting from the
    # surface, never adding to it: the rule from `governance.py`, seen at the surface.
    filterable = {
        name: obj.properties[name]
        for name in obj.searchable
        if name in obj.properties and name not in masked
    }
    filter_props = {}
    for name, prop in filterable.items():
        match = "case-insensitive substring match" if prop.type.kind == "string" else "exact match"
        filter_props[name] = {**prop.type.json_schema(), "description": f"{match} on {name}"}

    schema = {
        "type": "object",
        "properties": {
            "filter": {
                "type": "object",
                "properties": filter_props,
                "additionalProperties": False,
                "description": "property filters, ANDed together",
            },
            **_PAGE_SCHEMA,
        },
        "additionalProperties": False,
    }

    def handler(args: dict) -> Any:
        rows = resolver.search(
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


def _list_tool(resolver: Resolver, obj: ObjectType) -> ToolSpec:
    masked = resolver.masked(obj.api_name)
    schema = {"type": "object", "properties": dict(_PAGE_SCHEMA), "additionalProperties": False}

    def handler(args: dict) -> Any:
        rows = resolver.list(obj.api_name, limit=args.get("limit"), offset=args.get("offset", 0))
        return _page(obj, rows, args, masked)

    return ToolSpec(
        name=f"list_{snake_case(obj.api_name)}",
        description=_described(obj.status, f"List {_subject(obj)}, ordered by {obj.primary_key}.")
        + _withheld(masked),
        input_schema=schema,
        handler=handler,
    )


def _page(obj: ObjectType, rows: list[dict], args: dict, masked: Sequence[str] = ()) -> dict:
    limit = args.get("limit") or DEFAULT_PAGE_SIZE
    offset = args.get("offset", 0)
    return {
        "objectType": obj.api_name,
        "count": len(rows),
        "limit": limit,
        "offset": offset,
        # An agent has no other way to tell "that's everything" from "the page filled up".
        "hasMore": len(rows) == min(limit, MAX_PAGE_SIZE),
        # Always present, empty when nothing is withheld. A key that appears only under a policy
        # would make "this deployment governs nothing" and "this Loom is too old to say"
        # indistinguishable, which is the one thing an envelope reporting a mask must not do.
        "masked": list(masked),
        "objects": json_safe(rows),
    }


# ---- traverse ------------------------------------------------------------------


def _traverse_tool(resolver: Resolver) -> ToolSpec:
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
        rows = resolver.traverse(
            args["objectType"],
            args["key"],
            args["link"],
            limit=args.get("limit"),
            offset=args.get("offset", 0),
        )
        limit = args.get("limit") or DEFAULT_PAGE_SIZE
        return {
            "objectType": args["objectType"],
            "key": json_safe(args["key"]),
            "link": args["link"],
            "targetObjectType": direction.target_object_type,
            "cardinality": direction.link.cardinality,
            "count": len(rows),
            "limit": limit,
            "offset": args.get("offset", 0),
            "hasMore": len(rows) == min(limit, MAX_PAGE_SIZE),
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


def _run_tool(runtime: ActionRuntime, action: Action, actor: str | None) -> ToolSpec:
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
    populated. That is the first thing in this module that differs between two calls of one process,
    and it is deliberately the only one — the tool set, the schemas and the descriptions are still a
    function of the spec, exactly as §7 says, because what varies is a value passed through a tool
    rather than anything about the tool.
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
        result = runtime.run(
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
