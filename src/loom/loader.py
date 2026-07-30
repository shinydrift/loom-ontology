"""YAML -> typed model, structural pass (§0, §2-§5 shape rules).

The loader is the compiler front-end: it parses each file, enforces the *structural* grammar
(one kind per file, required fields, known keys, enum needs values, expressions parse), and
produces model objects whose cross-references are not yet resolved. Referential and semantic
rules (do links point at real objects? does an action stay single-object?) live in validator.py.

It never raises on the first problem — it accumulates into Diagnostics so `loom validate`
reports everything at once.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ._shape import check_keys as _check_keys
from ._shape import require as _require
from ._shape import suggest as _suggest
from .errors import Diagnostics, SourceLoc
from .expr import ExprError
from .expr import parse as parse_expr
from .model import (
    Action,
    Effect,
    LinkEnd,
    LinkType,
    ObjectType,
    Parameter,
    Property,
    ThroughTable,
    ValidationRule,
)
from .types import ALL_KINDS, PropType

KINDS = ("objectType", "linkType", "action")

_OBJECT_KEYS = {"apiName", "displayName", "description", "primaryKey", "title", "status", "backing", "properties", "searchable"}
_BACKING_KEYS = {"catalog", "table"}
_PROPERTY_KEYS = {"name", "type", "column", "nullable", "unique", "values", "precision", "scale", "description"}
_LINK_KEYS = {"apiName", "displayName", "description", "cardinality", "from", "to", "reverseName", "through", "status"}
_END_KEYS = {"objectType", "property"}
_THROUGH_KEYS = {"catalog", "table", "fromColumn", "toColumn"}
_ACTION_KEYS = {"apiName", "displayName", "description", "targetObjectType", "operation", "parameters", "validation", "effects", "status"}
_PARAM_KEYS = {"name", "type", "objectType", "required", "default", "values", "precision", "scale", "description"}
_EFFECT_OPS = {"createObject", "modifyObject", "deleteObject"}
_CARDINALITIES = {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}
_STATUSES = {"active", "deprecated", "experimental"}


class _Loaded:
    def __init__(self) -> None:
        self.objects: dict[str, ObjectType] = {}
        self.links: dict[str, LinkType] = {}
        self.actions: dict[str, Action] = {}


def load_dir(root: str | Path, diag: Diagnostics) -> _Loaded:
    root = Path(root)
    files = sorted(p for p in root.rglob("*.yaml")) + sorted(p for p in root.rglob("*.yml"))
    out = _Loaded()
    for f in files:
        _load_file(f, diag, out)
    return out


def _load_file(path: Path, diag: Diagnostics, out: _Loaded) -> None:
    rel = str(path)
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        diag.error(f"invalid YAML: {e}", SourceLoc(rel))
        return
    if doc is None:
        return
    if not isinstance(doc, dict):
        diag.error("top-level of a spec file must be a mapping", SourceLoc(rel))
        return
    present = [k for k in doc if k in KINDS]
    unknown_top = [k for k in doc if k not in KINDS]
    if len(present) != 1:
        diag.error(
            f"a spec file must declare exactly one of {KINDS}; found {present or 'none'}",
            SourceLoc(rel),
        )
        return
    for k in unknown_top:
        diag.error(f"unexpected top-level key '{k}'", SourceLoc(rel), _suggest(k, KINDS))
    kind = present[0]
    body = doc[kind]
    if not isinstance(body, dict):
        diag.error(f"'{kind}' must be a mapping", SourceLoc(rel, kind))
        return
    if kind == "objectType":
        obj = _parse_object(body, rel, diag)
        if obj:
            _register(out.objects, obj.api_name, obj, "objectType", rel, diag)
    elif kind == "linkType":
        link = _parse_link(body, rel, diag)
        if link:
            _register(out.links, link.api_name, link, "linkType", rel, diag)
    else:
        act = _parse_action(body, rel, diag)
        if act:
            _register(out.actions, act.api_name, act, "action", rel, diag)


def _register(registry: dict, name: str, value: object, kind: str, rel: str, diag: Diagnostics) -> None:
    if name in registry:
        diag.error(f"duplicate {kind} apiName '{name}'", SourceLoc(rel, kind, name))
        return
    registry[name] = value


# ---- shared helpers ------------------------------------------------------------


def _api_name(raw: dict, loc: SourceLoc, diag: Diagnostics, ctx: str) -> str | None:
    v = _require(raw, "apiName", loc, diag, ctx)
    return v if isinstance(v, str) else None


def _parse_type(raw: dict, loc: SourceLoc, diag: Diagnostics, ctx: str) -> PropType | None:
    t = _require(raw, "type", loc, diag, ctx)
    if not isinstance(t, str):
        return None
    if t not in ALL_KINDS:
        diag.error(f"unknown type '{t}' in {ctx}", loc, _suggest(t, ALL_KINDS))
        return None
    values = precision = scale = object_type = None
    if t == "enum":
        vals = raw.get("values")
        if not isinstance(vals, list) or not vals:
            diag.error(f"enum type in {ctx} requires a non-empty 'values' list", loc)
            return None
        if len(set(vals)) != len(vals):
            diag.error(f"enum 'values' in {ctx} contain duplicates", loc)
        values = tuple(str(v) for v in vals)
    elif t == "decimal":
        precision, scale = raw.get("precision"), raw.get("scale")
        if not isinstance(precision, int) or precision < 1:
            diag.error(f"decimal in {ctx} requires integer 'precision' >= 1", loc)
            return None
        if not isinstance(scale, int) or not (0 <= scale <= precision):
            diag.error(f"decimal in {ctx} requires integer 'scale' in 0..precision", loc)
            return None
    elif t == "objectRef":
        object_type = raw.get("objectType")
        if not isinstance(object_type, str):
            diag.error(f"objectRef in {ctx} requires 'objectType'", loc)
            return None
    return PropType(kind=t, values=values, object_type=object_type, precision=precision, scale=scale)


def _parse_expr_field(text: object, loc: SourceLoc, diag: Diagnostics, ctx: str):
    if not isinstance(text, str):
        diag.error(f"{ctx} must be a string expression", loc)
        return None
    try:
        return parse_expr(text)
    except ExprError as e:
        diag.error(f"invalid expression in {ctx}: {e}", loc)
        return None


# ---- objectType ----------------------------------------------------------------


def _parse_object(raw: dict, rel: str, diag: Diagnostics) -> ObjectType | None:
    name = _api_name(raw, SourceLoc(rel, "objectType"), diag, "objectType")
    loc = SourceLoc(rel, "objectType", name)
    _check_keys(raw, _OBJECT_KEYS, loc, diag, "objectType")
    if name is None:
        return None

    backing = _require(raw, "backing", loc, diag, "objectType")
    catalog = table = None
    if isinstance(backing, dict):
        _check_keys(backing, _BACKING_KEYS, loc, diag, "backing")
        catalog = _require(backing, "catalog", loc, diag, "backing")
        table = _require(backing, "table", loc, diag, "backing")

    props_raw = _require(raw, "properties", loc, diag, "objectType")
    properties: dict[str, Property] = {}
    if isinstance(props_raw, list):
        for p in props_raw:
            prop = _parse_property(p, loc, diag)
            if prop is None:
                continue
            if prop.name in properties:
                diag.error(f"duplicate property name '{prop.name}'", loc)
            else:
                properties[prop.name] = prop
    elif props_raw is not None:
        diag.error("'properties' must be a list", loc)

    status = raw.get("status", "active")
    if status not in _STATUSES:
        diag.error(f"invalid status '{status}'", loc, _suggest(str(status), _STATUSES))

    primary_key = _require(raw, "primaryKey", loc, diag, "objectType")
    searchable = tuple(raw.get("searchable", []) or ())
    if not isinstance(raw.get("searchable", []), list):
        diag.error("'searchable' must be a list", loc)
        searchable = ()

    if name is None or catalog is None or table is None or primary_key is None:
        return None
    title = raw.get("title") or primary_key
    return ObjectType(
        api_name=name,
        primary_key=primary_key,
        title=title,
        backing_catalog=catalog,
        backing_table=table,
        properties=properties,
        searchable=searchable,
        display_name=raw.get("displayName") or name,
        description=raw.get("description"),
        status=status,
        loc=loc,
    )


def _parse_property(raw: object, loc: SourceLoc, diag: Diagnostics) -> Property | None:
    if not isinstance(raw, dict):
        diag.error("each property must be a mapping", loc)
        return None
    _check_keys(raw, _PROPERTY_KEYS, loc, diag, "property")
    name = _require(raw, "name", loc, diag, "property")
    column = _require(raw, "column", loc, diag, "property")
    ptype = _parse_type(raw, loc, diag, f"property '{name}'")
    if name is None or column is None or ptype is None:
        return None
    return Property(
        name=name,
        type=ptype,
        column=column,
        nullable=bool(raw.get("nullable", False)),
        unique=bool(raw.get("unique", False)),
        description=raw.get("description"),
        loc=loc,
    )


# ---- linkType ------------------------------------------------------------------


def _parse_link(raw: dict, rel: str, diag: Diagnostics) -> LinkType | None:
    name = _api_name(raw, SourceLoc(rel, "linkType"), diag, "linkType")
    loc = SourceLoc(rel, "linkType", name)
    _check_keys(raw, _LINK_KEYS, loc, diag, "linkType")
    if name is None:
        return None

    cardinality = _require(raw, "cardinality", loc, diag, "linkType")
    if cardinality is not None and cardinality not in _CARDINALITIES:
        diag.error(f"invalid cardinality '{cardinality}'", loc, _suggest(str(cardinality), _CARDINALITIES))
        cardinality = None

    frm = _parse_end(raw.get("from"), "from", loc, diag)
    to = _parse_end(raw.get("to"), "to", loc, diag)

    through = None
    if "through" in raw and raw["through"] is not None:
        th = raw["through"]
        if isinstance(th, dict):
            _check_keys(th, _THROUGH_KEYS, loc, diag, "through")
            c = _require(th, "catalog", loc, diag, "through")
            t = _require(th, "table", loc, diag, "through")
            fc = _require(th, "fromColumn", loc, diag, "through")
            tc = _require(th, "toColumn", loc, diag, "through")
            if None not in (c, t, fc, tc):
                through = ThroughTable(c, t, fc, tc)
        else:
            diag.error("'through' must be a mapping", loc)

    status = raw.get("status", "active")
    if status not in _STATUSES:
        diag.error(f"invalid status '{status}'", loc, _suggest(str(status), _STATUSES))

    if name is None or cardinality is None or frm is None or to is None:
        return None
    return LinkType(
        api_name=name,
        cardinality=cardinality,
        frm=frm,
        to=to,
        reverse_name=raw.get("reverseName"),
        through=through,
        display_name=raw.get("displayName") or name,
        description=raw.get("description"),
        status=status,
        loc=loc,
    )


def _parse_end(raw: object, side: str, loc: SourceLoc, diag: Diagnostics) -> LinkEnd | None:
    if not isinstance(raw, dict):
        diag.error(f"'{side}' must be a mapping with objectType and property", loc)
        return None
    _check_keys(raw, _END_KEYS, loc, diag, side)
    ot = _require(raw, "objectType", loc, diag, side)
    prop = _require(raw, "property", loc, diag, side)
    if ot is None or prop is None:
        return None
    return LinkEnd(object_type=ot, property=prop)


# ---- action --------------------------------------------------------------------


def _parse_action(raw: dict, rel: str, diag: Diagnostics) -> Action | None:
    name = _api_name(raw, SourceLoc(rel, "action"), diag, "action")
    loc = SourceLoc(rel, "action", name)
    _check_keys(raw, _ACTION_KEYS, loc, diag, "action")
    if name is None:
        return None

    target = _require(raw, "targetObjectType", loc, diag, "action")
    operation = _require(raw, "operation", loc, diag, "action")
    if operation is not None and operation not in ("create", "modify", "delete"):
        diag.error(f"invalid operation '{operation}'", loc, _suggest(str(operation), {"create", "modify", "delete"}))
        operation = None
    # 'description' is the MCP tool description the LLM sees — required for good ergonomics.
    description = _require(raw, "description", loc, diag, "action")

    parameters: dict[str, Parameter] = {}
    params_raw = raw.get("parameters", []) or []
    if isinstance(params_raw, list):
        for p in params_raw:
            param = _parse_param(p, loc, diag)
            if param is None:
                continue
            if param.name in parameters:
                diag.error(f"duplicate parameter name '{param.name}'", loc)
            else:
                parameters[param.name] = param
    else:
        diag.error("'parameters' must be a list", loc)

    validation = _parse_validation(raw.get("validation", []) or [], loc, diag)
    effect = _parse_effects(raw.get("effects"), operation, loc, diag)

    status = raw.get("status", "active")
    if status not in _STATUSES:
        diag.error(f"invalid status '{status}'", loc, _suggest(str(status), _STATUSES))

    if name is None or target is None or operation is None or effect is None or description is None:
        return None
    return Action(
        api_name=name,
        target_object_type=target,
        operation=operation,
        parameters=parameters,
        effect=effect,
        validation=validation,
        display_name=raw.get("displayName") or name,
        description=description if isinstance(description, str) else "",
        status=status,
        loc=loc,
    )


def _parse_param(raw: object, loc: SourceLoc, diag: Diagnostics) -> Parameter | None:
    if not isinstance(raw, dict):
        diag.error("each parameter must be a mapping", loc)
        return None
    _check_keys(raw, _PARAM_KEYS, loc, diag, "parameter")
    name = _require(raw, "name", loc, diag, "parameter")
    ptype = _parse_type(raw, loc, diag, f"parameter '{name}'")
    if name is None or ptype is None:
        return None
    has_default = "default" in raw and raw["default"] is not None
    return Parameter(
        name=name,
        type=ptype,
        required=bool(raw.get("required", True)) and not has_default,
        default=raw.get("default"),
        description=raw.get("description"),
    )


def _parse_validation(raw: object, loc: SourceLoc, diag: Diagnostics) -> tuple[ValidationRule, ...]:
    if not isinstance(raw, list):
        diag.error("'validation' must be a list", loc)
        return ()
    rules: list[ValidationRule] = []
    for item in raw:
        if not isinstance(item, dict):
            diag.error("each validation rule must be a mapping", loc)
            continue
        _check_keys(item, {"rule", "message"}, loc, diag, "validation rule")
        rule_text = _require(item, "rule", loc, diag, "validation rule")
        message = _require(item, "message", loc, diag, "validation rule")
        expr = _parse_expr_field(rule_text, loc, diag, "validation rule") if rule_text else None
        if expr and message:
            rules.append(ValidationRule(expr=expr, message=message, raw=str(rule_text)))
    return tuple(rules)


def _parse_effects(raw: object, operation: str | None, loc: SourceLoc, diag: Diagnostics) -> Effect | None:
    if not isinstance(raw, list) or len(raw) != 1:
        diag.error("'effects' must be a list with exactly one entry (single-object writeback)", loc)
        return None
    entry = raw[0]
    if not isinstance(entry, dict) or len(entry) != 1:
        diag.error("an effect entry must be a mapping with exactly one operation key", loc)
        return None
    op = next(iter(entry))
    if op not in _EFFECT_OPS:
        diag.error(f"unknown effect '{op}'", loc, _suggest(op, _EFFECT_OPS))
        return None
    expected = {"create": "createObject", "modify": "modifyObject", "delete": "deleteObject"}.get(operation or "")
    if expected and op != expected:
        diag.error(f"operation '{operation}' requires effect '{expected}', found '{op}'", loc)
    spec = entry[op] or {}
    if not isinstance(spec, dict):
        diag.error(f"'{op}' body must be a mapping", loc)
        return None

    key_expr = None
    set_values: dict[str, object] = {}
    if op == "createObject":
        _check_keys(spec, {"set"}, loc, diag, op)
    else:
        _check_keys(spec, {"key", "set"} if op == "modifyObject" else {"key"}, loc, diag, op)
        key_raw = _require(spec, "key", loc, diag, op)
        key_expr = _parse_expr_field(key_raw, loc, diag, f"{op}.key") if key_raw is not None else None
        if key_expr is None:
            return None
    if op in ("createObject", "modifyObject"):
        set_raw = spec.get("set")
        if op == "createObject" and not isinstance(set_raw, dict):
            diag.error("createObject requires a 'set' mapping", loc)
            return None
        if isinstance(set_raw, dict):
            for prop, val in set_raw.items():
                e = _parse_expr_field(val, loc, diag, f"{op}.set.{prop}")
                if e is not None:
                    set_values[prop] = e
        elif set_raw is not None:
            diag.error("'set' must be a mapping", loc)

    return Effect(op=op, key=key_expr, set_values=set_values)
