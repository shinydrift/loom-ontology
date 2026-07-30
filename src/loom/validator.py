"""Referential + semantic validation (§2-§5 cross-object rules).

Runs after the structural loader. Everything here needs more than one declaration in scope:
do links point at real objects and properties? does an action stay single-object? do
expressions reference only declared parameters and the target object's properties? Physical
checks (does the backing table/column actually exist, is the type promotion-compatible) need a
live catalog and are deferred to plan/serve — see check_physical() stub at the bottom.
"""

from __future__ import annotations

from .errors import Diagnostics, SourceLoc
from .expr import FUNCTIONS, Binary, Call, Expr, Literal, Ref, Unary
from .loader import _Loaded
from .model import Action, LinkType, ObjectType
from .types import PropType

_COMPARISONS = {"==", "!=", "<", "<=", ">", ">="}
_BOOLEANS = {"&&", "||"}
_ARITH = {"+", "-", "*", "/"}


def validate(loaded: _Loaded, diag: Diagnostics) -> None:
    for obj in loaded.objects.values():
        _validate_object(obj, diag)
    for link in loaded.links.values():
        _validate_link(link, loaded, diag)
    for act in loaded.actions.values():
        _validate_action(act, loaded, diag)


# ---- objectType ----------------------------------------------------------------


def _validate_object(obj: ObjectType, diag: Diagnostics) -> None:
    loc = obj.loc
    props = obj.properties

    if obj.primary_key not in props:
        diag.error(f"primaryKey '{obj.primary_key}' is not a declared property", loc)
    else:
        pk = props[obj.primary_key]
        if pk.nullable:
            diag.error(f"primary key '{pk.name}' must not be nullable", loc)
        if not pk.unique:
            diag.error(f"primary key '{pk.name}' must be declared unique: true", loc)

    if obj.title not in props:
        diag.error(f"title '{obj.title}' is not a declared property", loc)

    seen_cols: dict[str, str] = {}
    for p in props.values():
        if p.column in seen_cols:
            diag.error(f"column '{p.column}' mapped by both '{seen_cols[p.column]}' and '{p.name}'", loc)
        else:
            seen_cols[p.column] = p.name

    for s in obj.searchable:
        if s not in props:
            diag.error(f"searchable entry '{s}' is not a declared property", loc)
        elif props[s].type.kind not in ("string", "enum"):
            diag.error(f"searchable property '{s}' must be string or enum, got '{props[s].type.kind}'", loc)


# ---- linkType ------------------------------------------------------------------


def _validate_link(link: LinkType, loaded: _Loaded, diag: Diagnostics) -> None:
    loc = link.loc
    from_obj = loaded.objects.get(link.frm.object_type)
    to_obj = loaded.objects.get(link.to.object_type)
    if from_obj is None:
        diag.error(f"from.objectType '{link.frm.object_type}' does not exist", loc)
    if to_obj is None:
        diag.error(f"to.objectType '{link.to.object_type}' does not exist", loc)

    from_prop = from_obj.properties.get(link.frm.property) if from_obj else None
    to_prop = to_obj.properties.get(link.to.property) if to_obj else None
    if from_obj and from_prop is None:
        diag.error(f"from.property '{link.frm.property}' not on '{link.frm.object_type}'", loc)
    if to_obj and to_prop is None:
        diag.error(f"to.property '{link.to.property}' not on '{link.to.object_type}'", loc)
    if from_prop and to_prop and not from_prop.type.comparable_to(to_prop.type):
        diag.error(
            f"join types differ: {link.frm.object_type}.{from_prop.name} is "
            f"'{from_prop.type.kind}' but {link.to.object_type}.{to_prop.name} is '{to_prop.type.kind}'",
            loc,
        )

    is_m2m = link.cardinality == "many_to_many"
    if is_m2m and link.through is None:
        diag.error("many_to_many link requires a 'through' mapping table", loc)
    if not is_m2m and link.through is not None:
        diag.error(f"'through' is only valid for many_to_many, not {link.cardinality}", loc)

    # reverseName must not collide with anything already exposed on the `to` object.
    if link.reverse_name and to_obj is not None:
        if link.reverse_name in to_obj.properties:
            diag.error(f"reverseName '{link.reverse_name}' collides with a property on '{to_obj.api_name}'", loc)
        for other in loaded.links.values():
            if other is link:
                continue
            if other.api_name == link.reverse_name and other.to.object_type == to_obj.api_name:
                diag.error(f"reverseName '{link.reverse_name}' collides with link apiName on '{to_obj.api_name}'", loc)
            if other.reverse_name == link.reverse_name and other.to.object_type == to_obj.api_name:
                diag.error(f"reverseName '{link.reverse_name}' duplicated on '{to_obj.api_name}'", loc)

    # Advisory: the "one" side of a to-one/one-to link should be unique, else silent fan-out.
    if link.cardinality == "many_to_one" and to_prop and not to_prop.unique:
        diag.warn(f"many_to_one target '{link.to.object_type}.{to_prop.name}' is not unique (possible fan-out)", loc)
    if link.cardinality == "one_to_many" and from_prop and not from_prop.unique:
        diag.warn(f"one_to_many source '{link.frm.object_type}.{from_prop.name}' is not unique (possible fan-out)", loc)


# ---- action --------------------------------------------------------------------


def _validate_action(act: Action, loaded: _Loaded, diag: Diagnostics) -> None:
    loc = act.loc
    target = loaded.objects.get(act.target_object_type)
    if target is None:
        diag.error(f"targetObjectType '{act.target_object_type}' does not exist", loc)

    # objectRef parameters must name a real object type.
    param_types: dict[str, PropType] = {}
    for name, p in act.parameters.items():
        param_types[name] = p.type
        if p.type.kind == "objectRef" and p.type.object_type not in loaded.objects:
            diag.error(f"parameter '{name}' references unknown objectType '{p.type.object_type}'", loc)

    # Current-object properties are only in scope for modify/delete (create has no prior object).
    object_props = None
    if target is not None and act.operation in ("modify", "delete"):
        object_props = {n: pr.type for n, pr in target.properties.items()}

    checker = _ExprChecker(param_types, object_props, act, diag, loc, loaded.objects)

    for rule in act.validation:
        t = checker.check(rule.expr)
        if t is not None and t.kind != "boolean":
            diag.error(f"validation rule '{rule.raw}' must be boolean, inferred '{t.kind}'", loc)

    eff = act.effect
    if eff.key is not None:
        kt = checker.check(eff.key)
        if target is not None and kt is not None:
            pk_type = target.pk_property.type
            if not kt.comparable_to(pk_type):
                diag.error(f"effect key type '{kt.kind}' is not comparable to primary key type '{pk_type.kind}'", loc)

    # set keys must be real properties of the target; values must type-match.
    if target is not None:
        for prop_name, val_expr in eff.set_values.items():
            if prop_name not in target.properties:
                diag.error(f"effect set '{prop_name}' is not a property of '{target.api_name}'", loc)
                checker.check(val_expr)  # still surface reference errors
                continue
            vt = checker.check(val_expr)
            ptype = target.properties[prop_name].type
            if vt is not None and not vt.comparable_to(ptype):
                diag.error(f"effect set '{prop_name}': value type '{vt.kind}' != property type '{ptype.kind}'", loc)
        # create must cover PK + every non-nullable, non-defaulted property.
        if eff.op == "createObject":
            required = {
                n for n, pr in target.properties.items() if not pr.nullable
            } | {target.primary_key}
            missing = sorted(required - set(eff.set_values))
            if missing:
                diag.error(f"createObject must set required properties: {', '.join(missing)}", loc)


class _ExprChecker:
    """Validates references/functions in an expression and best-effort infers its type.
    Returns None when the type is unknown (never a guess), so callers avoid false positives."""

    def __init__(self, params, object_props, act: Action, diag: Diagnostics, loc: SourceLoc, objects):
        self.params = params
        self.object_props = object_props
        self.act = act
        self.diag = diag
        self.loc = loc
        self.objects = objects

    def check(self, expr: Expr) -> PropType | None:
        return self._infer(expr.root)

    def _resolve(self, t: PropType | None) -> PropType | None:
        """An objectRef travels on the wire as the referenced object's primary key, so it
        compares and stores as that PK's type."""
        if t is not None and t.kind == "objectRef":
            ref = self.objects.get(t.object_type)
            if ref is not None:
                return ref.pk_property.type
        return t

    def _infer(self, node) -> PropType | None:
        if isinstance(node, Literal):
            return self._literal_type(node.value)
        if isinstance(node, Ref):
            return self._ref_type(node)
        if isinstance(node, Call):
            return self._call_type(node)
        if isinstance(node, Unary):
            inner = self._infer(node.operand)
            if node.op == "!":
                return PropType("boolean")
            return inner  # unary minus preserves numeric type
        if isinstance(node, Binary):
            lt = self._infer(node.left)
            rt = self._infer(node.right)
            if node.op in _COMPARISONS or node.op in _BOOLEANS:
                return PropType("boolean")
            if node.op in _ARITH:
                if node.op == "+" and (self._is_string(lt) or self._is_string(rt)):
                    return PropType("string")
                return lt or rt
        return None

    def _literal_type(self, v) -> PropType | None:
        if isinstance(v, bool):
            return PropType("boolean")
        if isinstance(v, int):
            return PropType("long")
        if isinstance(v, float):
            return PropType("double")
        if isinstance(v, str):
            return PropType("string")
        return None  # null

    def _ref_type(self, ref: Ref) -> PropType | None:
        if len(ref.path) == 1:
            name = ref.path[0]
            if name in self.params:
                return self._resolve(self.params[name])
            self.diag.error(f"expression references unknown parameter '{name}' in action '{self.act.api_name}'", self.loc)
            return None
        if len(ref.path) == 2 and ref.path[0] == "object":
            if self.object_props is None:
                self.diag.error(
                    f"'object.{ref.path[1]}' is not available in a {self.act.operation} action "
                    f"(no current object)", self.loc)
                return None
            prop = ref.path[1]
            if prop not in self.object_props:
                self.diag.error(f"'object.{prop}' is not a property of the target object", self.loc)
                return None
            return self.object_props[prop]
        self.diag.error(f"unsupported reference '{'.'.join(ref.path)}' (use a parameter or object.<prop>)", self.loc)
        return None

    def _call_type(self, call: Call) -> PropType | None:
        for a in call.args:
            self._infer(a)  # validate nested references
        if call.name not in FUNCTIONS:
            self.diag.error(f"unknown function '{call.name}()'", self.loc)
            return None
        lo, hi = FUNCTIONS[call.name]
        n = len(call.args)
        if n < lo or (hi is not None and n > hi):
            want = f"{lo}" if lo == hi else (f">={lo}" if hi is None else f"{lo}..{hi}")
            self.diag.error(f"function '{call.name}()' expects {want} args, got {n}", self.loc)
        return {
            "now": PropType("timestamp"),
            "lower": PropType("string"),
            "upper": PropType("string"),
            "len": PropType("int"),
            "coalesce": self._infer(call.args[0]) if call.args else None,
        }.get(call.name)

    @staticmethod
    def _is_string(t: PropType | None) -> bool:
        return t is not None and t.kind in ("string", "enum")


def check_physical(loaded: _Loaded, catalog, diag: Diagnostics) -> None:  # pragma: no cover - deferred
    """Physical-plane validation against a live Iceberg catalog: table/column existence and
    type promotion-compatibility (§2 rule 7). Deferred until the catalog module lands; the
    rule set is specified, only the introspection is missing."""
    raise NotImplementedError("physical validation requires a bound Iceberg catalog (post-v0)")
