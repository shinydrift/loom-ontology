"""The `loom.yaml` project config — §6 of the spec grammar.

Not part of the ontology: this is the file the ontology's `backing.catalog` references resolve
against, plus the engine and MCP transport selection. Kept deliberately separate from the
ontology spec because it is *deployment* config — the same ontology should be servable against a
local warehouse in a test and a REST catalog in production with no spec edits.

Uses the same accumulate-all-errors Diagnostics as the ontology loader, so `loom validate`
reports spec and config problems in a single pass.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ._shape import check_keys, require, suggest
from .errors import Diagnostics, SourceLoc

CONFIG_FILENAME = "loom.yaml"
SPEC_VERSION = 0

# `iceberg-sql` is pyiceberg's SQL catalog (SQLite/Postgres-backed metastore over a filesystem
# or object-store warehouse). It exists so tests and examples can run a real Iceberg catalog with
# no services to stand up; it sits behind the same Catalog port as `iceberg-rest`.
CATALOG_TYPES = frozenset({"iceberg-rest", "iceberg-sql"})
ENGINE_TYPES = frozenset({"duckdb"})
TRANSPORTS = frozenset({"stdio"})

_TOP_KEYS = {"version", "catalogs", "engine", "mcp", "governance"}
_CATALOG_KEYS = {"type", "uri", "warehouse", "auth", "properties"}
_ENGINE_KEYS = {"type", "options"}
_MCP_KEYS = {"name", "transport", "writes", "actor"}
_GOVERNANCE_KEYS = {"policies"}


@dataclass(frozen=True)
class CatalogConfig:
    """One entry under `catalogs:`. `properties` is opaque to Loom — merged from `auth` and
    `properties` and handed straight to the catalog client."""

    name: str
    type: str
    uri: str
    warehouse: str | None = None
    properties: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class EngineConfig:
    type: str = "duckdb"
    options: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class McpConfig:
    """What `loom serve` exposes, and as whom.

    `writes` is **off by default**, and that is a decision rather than caution. Until the action
    runtime became a tool set, `loom serve` could not change anything, and people pointed it at real
    lakes on that basis. Turning writes on with the arrival of the tools would mean an upgrade plus
    a spec that declares an action — two things that happen for unrelated reasons — silently making
    a production lake mutable by any MCP client. So a deployment says so, in the file a deployment
    is configured by. It is the same posture as `loom apply` refusing to run unattended: don't write
    to somebody's lake because nobody was there to object.

    Deliberately not a CLI flag. A flag lets one `loom serve` invocation contradict the file an
    operator reviews, and "is this server writable" is exactly the question that file should answer.
    And deliberately not a governance policy: it names no principal and filters no row. It is a
    switch on a whole surface, which is a different kind of thing from what §6's `policies` will be —
    though M5 may well end up subsuming it.

    `actor` is what the edit log records for a run that arrives through a tool. It is **declared,
    never inferred**, which is the whole difference between it and `default_actor()`: that function
    falls back to the OS user, so on this path it would name whoever started the process while
    looking like a principal, and stamp every caller in the deployment with one string. An operator
    writing `actor: agent:support-bot` is instead making a true statement about a deployment — and
    over stdio it is exactly true, because one client spawns one process and the session has one
    principal. Unset, runs record `unknown`; see `action.log.UNKNOWN_ACTOR` for why that beats a
    confident wrong answer.
    """

    name: str = "loom"
    transport: str = "stdio"
    writes: bool = False
    actor: str | None = None


@dataclass(frozen=True)
class LoomConfig:
    catalogs: Mapping[str, CatalogConfig] = field(default_factory=dict)
    engine: EngineConfig = field(default_factory=EngineConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    version: int = SPEC_VERSION
    source: str | None = None  # path it was loaded from, for error messages


def find_config(ontology_path: str | Path) -> Path | None:
    """Locate `loom.yaml` for an ontology directory: inside it, then alongside it, then cwd.

    The ontology dir is conventionally `./ontology` with `loom.yaml` beside it at the project
    root, so the middle case is the common one."""
    p = Path(ontology_path)
    candidates = [p / CONFIG_FILENAME, p.parent / CONFIG_FILENAME, Path.cwd() / CONFIG_FILENAME]
    for c in candidates:
        if c.is_file():
            return c
    return None


def load_config(path: str | Path, diag: Diagnostics) -> LoomConfig | None:
    """Parse and validate a loom.yaml. Returns None only when the file is unusable as a whole
    (unreadable, not a mapping); field-level problems accumulate into `diag` instead."""
    path = Path(path)
    loc = SourceLoc(str(path))
    try:
        doc = yaml.safe_load(path.read_text())
    except OSError as e:
        diag.error(f"cannot read {CONFIG_FILENAME}: {e}", loc)
        return None
    except yaml.YAMLError as e:
        diag.error(f"invalid YAML: {e}", loc)
        return None
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        diag.error(f"top-level of {CONFIG_FILENAME} must be a mapping", loc)
        return None

    check_keys(doc, _TOP_KEYS, loc, diag, CONFIG_FILENAME)
    base = path.parent.resolve()

    version = doc.get("version", SPEC_VERSION)
    if version != SPEC_VERSION:
        diag.error(f"unsupported config version {version!r} (this build speaks {SPEC_VERSION})", loc)

    catalogs = _parse_catalogs(doc.get("catalogs"), loc, diag, base)
    engine = _parse_engine(doc.get("engine"), loc, diag)
    mcp = _parse_mcp(doc.get("mcp"), loc, diag)
    _check_governance(doc.get("governance"), loc, diag)

    return LoomConfig(
        catalogs=catalogs,
        engine=engine,
        mcp=mcp,
        version=SPEC_VERSION,
        source=str(path),
    )


def _resolve_local_path(value: str, scheme: str, base: Path) -> str:
    """Make a local `sqlite:///` or `file://` location relative to the config file, not the cwd.

    Without this, a config that says `warehouse: file://.warehouse` would resolve somewhere
    different depending on where `loom serve` was invoked from — which quietly points the ontology
    at an empty warehouse instead of failing."""
    if not value.startswith(scheme):
        return value
    remainder = value[len(scheme):]
    # Absolute already, or a SQLAlchemy pseudo-path like `:memory:` that isn't a file at all.
    if not remainder or remainder.startswith(("/", ":")):
        return value
    return scheme + str((base / remainder).resolve())


def _parse_catalogs(raw: object, loc: SourceLoc, diag: Diagnostics, base: Path) -> dict[str, CatalogConfig]:
    if raw is None:
        diag.error("no 'catalogs' declared — an ontology's backing tables have nowhere to resolve", loc)
        return {}
    if not isinstance(raw, dict):
        diag.error("'catalogs' must be a mapping of name -> catalog config", loc)
        return {}

    out: dict[str, CatalogConfig] = {}
    for name, body in raw.items():
        ctx = f"catalog '{name}'"
        if not isinstance(body, dict):
            diag.error(f"{ctx} must be a mapping", loc)
            continue
        check_keys(body, _CATALOG_KEYS, loc, diag, ctx)

        ctype = require(body, "type", loc, diag, ctx)
        if ctype is not None and ctype not in CATALOG_TYPES:
            diag.error(f"unknown catalog type '{ctype}' in {ctx}", loc, suggest(str(ctype), CATALOG_TYPES))
            ctype = None
        uri = require(body, "uri", loc, diag, ctx)
        warehouse = body.get("warehouse")

        # pyiceberg's SQL catalog needs a warehouse root to place table data under; REST
        # catalogs usually carry their own server-side default.
        if ctype == "iceberg-sql" and not warehouse:
            diag.error(f"{ctx}: catalog type 'iceberg-sql' requires 'warehouse'", loc)

        properties: dict[str, object] = {}
        for key in ("properties", "auth"):
            extra = body.get(key)
            if extra is None:
                continue
            if isinstance(extra, dict):
                properties.update(extra)
            else:
                diag.error(f"{ctx}: '{key}' must be a mapping", loc)

        if ctype is None or uri is None:
            continue
        out[str(name)] = CatalogConfig(
            name=str(name),
            type=str(ctype),
            uri=_resolve_local_path(str(uri), "sqlite:///", base),
            warehouse=_resolve_local_path(str(warehouse), "file://", base) if warehouse else None,
            properties=properties,
        )
    return out


def _parse_engine(raw: object, loc: SourceLoc, diag: Diagnostics) -> EngineConfig:
    if raw is None:
        return EngineConfig()
    if not isinstance(raw, dict):
        diag.error("'engine' must be a mapping", loc)
        return EngineConfig()
    check_keys(raw, _ENGINE_KEYS, loc, diag, "engine")

    etype = raw.get("type", "duckdb")
    if etype not in ENGINE_TYPES:
        diag.error(
            f"unknown engine type '{etype}' (available: {', '.join(sorted(ENGINE_TYPES))})",
            loc,
            suggest(str(etype), ENGINE_TYPES),
        )
        etype = "duckdb"

    options = raw.get("options") or {}
    if not isinstance(options, dict):
        diag.error("'engine.options' must be a mapping", loc)
        options = {}
    return EngineConfig(type=str(etype), options=options)


def _parse_mcp(raw: object, loc: SourceLoc, diag: Diagnostics) -> McpConfig:
    if raw is None:
        return McpConfig()
    if not isinstance(raw, dict):
        diag.error("'mcp' must be a mapping", loc)
        return McpConfig()
    check_keys(raw, _MCP_KEYS, loc, diag, "mcp")

    transport = raw.get("transport", "stdio")
    if transport not in TRANSPORTS:
        hint = "http transport is not implemented yet (M4)" if transport == "http" else suggest(str(transport), TRANSPORTS)
        diag.error(f"unsupported mcp transport '{transport}'", loc, hint)
        transport = "stdio"

    writes = raw.get("writes", False)
    if not isinstance(writes, bool):
        # Not coerced. `writes: "no"` is truthy in Python and would turn writes *on* — the one
        # misreading of this key that costs somebody a row.
        diag.error(f"'mcp.writes' must be true or false, got {writes!r}", loc)
        writes = False

    actor = raw.get("actor")
    if actor is not None and (not isinstance(actor, str) or not actor.strip()):
        diag.error(f"'mcp.actor' must be a non-empty string, got {actor!r}", loc)
        actor = None

    return McpConfig(
        name=str(raw.get("name") or "loom"),
        transport=str(transport),
        writes=writes,
        actor=actor.strip() if isinstance(actor, str) else None,
    )


def _check_governance(raw: object, loc: SourceLoc, diag: Diagnostics) -> None:
    """Governance is deliberately not in v0. Accept the key so specs can carry it forward, but
    refuse to start with non-empty policies — silently ignoring a declared access policy is a
    far worse failure than not booting."""
    if raw is None:
        return
    if not isinstance(raw, dict):
        diag.error("'governance' must be a mapping", loc)
        return
    check_keys(raw, _GOVERNANCE_KEYS, loc, diag, "governance")
    policies = raw.get("policies") or []
    if policies:
        diag.error(
            f"{len(policies)} governance policy/policies declared but policy enforcement is not "
            "implemented yet — refusing to start rather than silently ignoring them",
            loc,
            "remove them until M5 lands, or pin an older spec",
        )
