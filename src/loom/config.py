"""The `loom.yaml` project config — §6 of the spec grammar.

Not part of the ontology: this is the file the ontology's `backing.catalog` references resolve
against, plus the engine and MCP transport selection. Kept deliberately separate from the
ontology spec because it is *deployment* config — the same ontology should be servable against a
local warehouse in a test and a REST catalog in production with no spec edits.

Uses the same accumulate-all-errors Diagnostics as the ontology loader, so `loom validate`
reports spec and config problems in a single pass.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping, Sequence
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
TRANSPORTS = frozenset({"stdio", "http"})

DEFAULT_HTTP_PORT = 8000
DEFAULT_HTTP_PATH = "/mcp"
DEFAULT_HTTP_HOST = "127.0.0.1"

_TOP_KEYS = {"version", "catalogs", "engine", "mcp", "governance"}
_CATALOG_KEYS = {"type", "uri", "warehouse", "auth", "properties"}
_ENGINE_KEYS = {"type", "options"}
_MCP_KEYS = {"name", "transport", "writes", "actor", "host", "port", "path", "allowed_hosts"}
_ADDRESS_KEYS = ("host", "port", "path", "allowed_hosts")
"""The keys that only mean something once the transport has an address. stdio has none, and a
config that sets them under `transport: stdio` is refused rather than ignored — the rule
`_check_governance` states, applied to a second set of keys that would otherwise be silently
dropped."""
_GOVERNANCE_KEYS = {"policies"}


def is_loopback(host: str) -> bool:
    """Does binding to `host` keep the server reachable only from this machine?

    Fails **closed**: a name Loom cannot resolve to a loopback address — including every DNS name
    other than `localhost` — is treated as not loopback. Two refusals in `_parse_mcp` hang off this
    answer, and both are the kind that should err towards refusing to start."""
    h = host.strip().strip("[]")
    if h.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


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
    """What `loom serve` exposes, where, and as whom.

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
    looking like a principal. An operator writing `actor: agent:support-bot` is instead making a
    statement about a deployment. Unset, runs record `unknown`; see `action.log.UNKNOWN_ACTOR` for
    why that beats a confident wrong answer.

    M4's first slice justified that with "over stdio it is exactly true, because one client spawns
    one process and the session has one principal", and the HTTP transport is where that sentence
    has to be corrected rather than extended — **it was already doing less work than it looked
    like.** This key lives in `loom.yaml`, which is per *deployment*, so three stdio clients reading
    one config file already record one string for three callers. Many callers under one name is not
    what a socket introduces. What survives untouched is the distinction that was actually load
    bearing: declared versus inferred.

    What HTTP *does* change is **reachability** — who is permitted to be one of those callers. Over
    stdio the set is "whoever can run the binary and read this file"; over a loopback bind it is very
    nearly the same set; over `0.0.0.0` it is not remotely the same set, and there `actor:` names a
    deployment nobody bounded. So the limit is drawn on the bind address rather than on the
    transport, and `_parse_mcp` refuses `writes: true` on a non-loopback bind. What that check can
    honestly claim is narrow and worth saying: it constrains what Loom *binds*, not what *reaches*
    it. A proxy in front of a loopback bind is outside anything this file can see.

    ---

    **The address, and why all of it is here rather than on the command line.** `host`, `port`,
    `path`. M4's first slice put `writes` in config on the argument that a flag lets one invocation
    contradict the file an operator reviews, and a port number is the weakest case that argument has
    to survive — a port is not a posture. It goes here anyway, because a file that describes half an
    address does not describe the server, and reviewing it would mean reading the unit file too. The
    host is the strongest case: it *is* the posture, and it is exactly the question the reviewed file
    has to answer.

    `host` defaults to `127.0.0.1` for the same reason `loom apply` refuses to run unattended — do
    not put somebody's lake on a network because nobody said to. `0.0.0.0` is a deliberate act, and
    it costs the write surface until an authenticated transport lands.

    `allowed_hosts` is the `Host` header allow-list backing DNS-rebinding protection, which stays on.
    It is optional exactly where it can be derived: a loopback bind knows its own names
    (`host_allow_list`). A non-loopback bind does not know what hostname the world reaches it by, so
    it is required there rather than guessed — the same shape as the `writes` refusal above.

    There is no TLS key. `loom serve` speaks cleartext HTTP and terminating TLS is a job for whatever
    sits in front, which is the third reason the default bind is loopback.
    """

    name: str = "loom"
    transport: str = "stdio"
    writes: bool = False
    actor: str | None = None
    host: str = DEFAULT_HTTP_HOST
    port: int = DEFAULT_HTTP_PORT
    path: str = DEFAULT_HTTP_PATH
    allowed_hosts: tuple[str, ...] = ()

    @property
    def is_loopback(self) -> bool:
        return is_loopback(self.host)

    def address(self) -> str:
        """The URL the banner prints. Cleartext by construction — see the class docstring."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}{self.path}"

    def host_allow_list(self) -> tuple[str, ...]:
        """The `Host` header values the transport accepts.

        Declared wins. Otherwise the bind is loopback — `_parse_mcp` refuses any other bind that
        leaves this unset — so the three spellings a local client can reach it by are the complete
        set, and a browser rebinding `evil.example` onto 127.0.0.1 sends a `Host` that is not among
        them."""
        if self.allowed_hosts:
            return self.allowed_hosts
        return tuple(f"{name}:{self.port}" for name in ("127.0.0.1", "localhost", "[::1]"))


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
    declared = transport in TRANSPORTS
    if not declared:
        diag.error(f"unsupported mcp transport '{transport}'", loc, suggest(str(transport), TRANSPORTS))
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

    host, port, path, allowed_hosts = _parse_address(raw, loc, diag)
    config = McpConfig(
        name=str(raw.get("name") or "loom"),
        transport=str(transport),
        writes=writes,
        actor=actor.strip() if isinstance(actor, str) else None,
        host=host,
        port=port,
        path=path,
        allowed_hosts=allowed_hosts,
    )
    if declared:
        _check_transport_posture(config, raw, loc, diag)
    return config


def _parse_address(raw: dict, loc: SourceLoc, diag: Diagnostics) -> tuple[str, int, str, tuple[str, ...]]:
    """`host` / `port` / `path` / `allowed_hosts`, each falling back to its default on a bad value.

    Nothing here knows about the transport; `_check_transport_posture` owns every question that
    needs to see more than one key at once."""
    host = raw.get("host", DEFAULT_HTTP_HOST)
    if not isinstance(host, str) or not host.strip():
        diag.error(f"'mcp.host' must be a non-empty string, got {host!r}", loc)
        host = DEFAULT_HTTP_HOST
    host = host.strip()

    port = raw.get("port", DEFAULT_HTTP_PORT)
    # `isinstance(True, int)` is True, and `port: yes` is a plausible YAML typo for a port number.
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        diag.error(f"'mcp.port' must be an integer from 1 to 65535, got {port!r}", loc)
        port = DEFAULT_HTTP_PORT

    path = raw.get("path", DEFAULT_HTTP_PATH)
    if not isinstance(path, str) or not path.startswith("/"):
        diag.error(f"'mcp.path' must be a string beginning with '/', got {path!r}", loc)
        path = DEFAULT_HTTP_PATH
    # `/mcp/` and `/mcp` mount the same endpoint; normalising here keeps the banner, the allow-list
    # and the route from disagreeing about which one this deployment is.
    path = "/" + path.strip("/")

    raw_hosts = raw.get("allowed_hosts")
    allowed_hosts: tuple[str, ...] = ()
    if raw_hosts is not None:
        if not isinstance(raw_hosts, Sequence) or isinstance(raw_hosts, (str, bytes)):
            diag.error(f"'mcp.allowed_hosts' must be a list of host[:port] strings, got {raw_hosts!r}", loc)
        elif not all(isinstance(h, str) and h.strip() for h in raw_hosts):
            diag.error(f"'mcp.allowed_hosts' entries must be non-empty strings, got {list(raw_hosts)!r}", loc)
        else:
            allowed_hosts = tuple(h.strip() for h in raw_hosts)
    return host, port, path, allowed_hosts


def _check_transport_posture(config: McpConfig, raw: dict, loc: SourceLoc, diag: Diagnostics) -> None:
    """The three questions that need more than one key to answer.

    All of them are *errors*, so `loom validate` reports them and `loom serve` never starts — which
    is the posture `cmd_serve` already takes when a catalog will not open. A warning on the way past
    is worth nothing here: nobody reads the third line of a banner on a server that came up."""
    address_keys = [k for k in _ADDRESS_KEYS if k in raw]
    if config.transport == "stdio":
        if address_keys:
            # `_check_governance`'s rule, applied to a second set of keys: silently ignoring
            # something a config declared is a worse failure than refusing to boot. And a stdio
            # server that quietly ignored `host: 0.0.0.0` would read, to the person who wrote it,
            # exactly like one that honoured it.
            diag.error(
                f"mcp.{', mcp.'.join(address_keys)} set but transport is 'stdio', which has no address",
                loc,
                "set 'transport: http' to serve over a socket, or drop the address keys",
            )
        return

    if config.is_loopback:
        return

    # From here the bind is reachable from off this machine, and two things stop being derivable.
    if not config.allowed_hosts:
        diag.error(
            f"'mcp.host' is {config.host!r}, which is not loopback, and 'mcp.allowed_hosts' is unset",
            loc,
            "a non-loopback bind cannot derive the Host headers to accept — declare them, e.g. "
            f"allowed_hosts: [loom.internal:{config.port}]",
        )
    if config.writes:
        diag.error(
            f"'mcp.writes' is true on a non-loopback bind ({config.host!r}) — refusing to serve a "
            "write surface to whoever can reach the port",
            loc,
            "bind 127.0.0.1 and put authentication in front, or set 'writes: false'. `mcp.actor` "
            "names a deployment, not a caller, so every write here would be recorded under one "
            "name nobody checked",
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
