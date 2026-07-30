"""Ontology Model -> MCP tool set.

Nothing here is hand-authored per ontology: the tool names come from api names, the input schemas
from `PropType.json_schema()`, and the descriptions from the spec's own `description` fields. That
is the whole point of the spec being the single source of truth — the agent-facing contract is
*derived*, so it cannot drift from the model the resolver enforces.

The generated surface is fixed (spec §7) and read-only in M1:

    get_<object>      one object by primary key
    search_<object>   filter by declared `searchable` properties
    list_<object>     a page of objects
    traverse          one hop along a declared link

`run_<action>` joins them when the action runtime lands (M3/M4). There is deliberately no tool
that accepts a predicate, a column, a table, or a query string.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..model import ObjectType
from ..resolver import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Resolver

TRAVERSE_TOOL = "traverse"

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


def build_tools(resolver: Resolver) -> list[ToolSpec]:
    """Introspect the resolver's ontology into the full read tool set."""
    tools: list[ToolSpec] = []
    for obj in resolver.ontology.object_types.values():
        tools.append(_get_tool(resolver, obj))
        tools.append(_search_tool(resolver, obj))
        tools.append(_list_tool(resolver, obj))
    if resolver.ontology.link_types:
        tools.append(_traverse_tool(resolver))
    return tools


# ---- per-object tools ----------------------------------------------------------


def _subject(obj: ObjectType) -> str:
    """The phrase describing an object type in generated tool descriptions."""
    return f"{obj.display_name or obj.api_name}" + (f" — {obj.description}" if obj.description else "")


def _get_tool(resolver: Resolver, obj: ObjectType) -> ToolSpec:
    pk = obj.pk_property
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
        }

    return ToolSpec(
        name=f"get_{snake_case(obj.api_name)}",
        description=f"Fetch one {_subject(obj)} by its {pk.name}.",
        input_schema=schema,
        handler=handler,
    )


def _search_tool(resolver: Resolver, obj: ObjectType) -> ToolSpec:
    filterable = {name: obj.properties[name] for name in obj.searchable if name in obj.properties}
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
        return _page(obj, rows, args)

    searchable = ", ".join(filterable) or "no properties are declared searchable"
    return ToolSpec(
        name=f"search_{snake_case(obj.api_name)}",
        description=f"Search {_subject(obj)} by {searchable}.",
        input_schema=schema,
        handler=handler,
    )


def _list_tool(resolver: Resolver, obj: ObjectType) -> ToolSpec:
    schema = {"type": "object", "properties": dict(_PAGE_SCHEMA), "additionalProperties": False}

    def handler(args: dict) -> Any:
        rows = resolver.list(obj.api_name, limit=args.get("limit"), offset=args.get("offset", 0))
        return _page(obj, rows, args)

    return ToolSpec(
        name=f"list_{snake_case(obj.api_name)}",
        description=f"List {_subject(obj)}, ordered by {obj.primary_key}.",
        input_schema=schema,
        handler=handler,
    )


def _page(obj: ObjectType, rows: list[dict], args: dict) -> dict:
    limit = args.get("limit") or DEFAULT_PAGE_SIZE
    offset = args.get("offset", 0)
    return {
        "objectType": obj.api_name,
        "count": len(rows),
        "limit": limit,
        "offset": offset,
        # An agent has no other way to tell "that's everything" from "the page filled up".
        "hasMore": len(rows) == min(limit, MAX_PAGE_SIZE),
        "objects": json_safe(rows),
    }


# ---- traverse ------------------------------------------------------------------


def _traverse_tool(resolver: Resolver) -> ToolSpec:
    """One generic tool rather than one per link: the link name is data, and enumerating
    object-type x link as separate tools would grow the surface an agent has to read for no gain."""
    routes: dict[str, list[str]] = {}
    for name in resolver.ontology.object_types:
        directions = resolver.links_of(name)
        if directions:
            routes[name] = [
                f"{d.name} -> {d.target_object_type} ({d.link.cardinality})" for d in directions
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
