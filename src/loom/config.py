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
from .auth import BUILT_IN_CLAIMS, CLAIM_TYPES, MAX_SKEW, ClaimType
from .errors import Diagnostics, SourceLoc
from .governance import (
    EDIT_LOG_OPTIONAL,
    INGEST_REFUSED,
    Policy,
    parse_edit_log,
    parse_ingest_posture,
    parse_policies,
)

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

INGEST_APPEND = "append"
INGEST_MERGE = "merge"
INGEST_REPLACE = "replace"
INGEST_MODES = (INGEST_APPEND, INGEST_MERGE, INGEST_REPLACE)
"""What an `ingest[].mode` may say. No default — see `_parse_ingest`."""

INGEST_FORMATS = frozenset({"parquet", "ndjson", "csv"})
"""The source shapes `loom ingest` can read.

A short list on purpose: each one is a *file*, and that is the boundary this milestone draws. Loom
does not connect to Kafka, crawl an object store, or open a JDBC connection — a pipeline hands it a
batch and Loom decides whether that batch may become rows. Widening this set is a decision about
formats; adding a *source* would be a decision about what Loom is."""

_TOP_KEYS = {"version", "catalogs", "engine", "mcp", "governance", "ingest"}
_INGEST_KEYS = {"name", "objectType", "mode", "format", "columns"}
_CATALOG_KEYS = {"type", "uri", "warehouse", "auth", "properties"}
_ENGINE_KEYS = {"type", "options"}
_MCP_KEYS = {"name", "transport", "writes", "actor", "host", "port", "path", "allowed_hosts", "auth"}
_AUTH_KEYS = {"issuer", "audience", "jwks_uri", "clock_skew", "claims"}
_ADDRESS_KEYS = ("host", "port", "path", "allowed_hosts")
"""The keys that only mean something once the transport has an address. stdio has none, and a
config that sets them under `transport: stdio` is refused rather than ignored — the rule
`_check_governance` states, applied to a second set of keys that would otherwise be silently
dropped."""
_GOVERNANCE_KEYS = {"policies", "edit_log", "ingest"}


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
    auth: McpAuth | None = None
    """The authorization server this deployment believes, or None — meaning it attests nobody.

    None is not a weaker `auth:`; it is the whole of what every deployment before this one was, and
    it stays the default. What it costs is stated where the cost is enforced: without it, a
    non-loopback bind may not write, because `actor` names a deployment and nobody checked who
    called."""

    @property
    def is_loopback(self) -> bool:
        return is_loopback(self.host)

    @property
    def attests(self) -> bool:
        """Whether a caller of this surface can ever be named.

        The one predicate the rest of the codebase should ask, rather than re-deriving it from
        `transport` and `auth` in three places that can drift. False for stdio whatever `auth` says —
        a spawned server carries no bearer token, so there is no exchange to carry one on — which is
        why this is a property of the *surface* rather than of the config's auth block."""
        return self.auth is not None and self.transport == "http"

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
class McpAuth:
    """The authorization server this deployment believes, and the three things it must name.

    All three are required and none is derived, which is the same posture `mcp.actor` takes for a
    different reason. `issuer` and `audience` are what a token is checked *against*, and a default
    for either would be Loom choosing who vouches for a caller and what the token was addressed to —
    the two questions only a deployment can answer.

    **`jwks_uri` is configured rather than discovered**, and that is a decision with a cost worth
    stating. OIDC publishes it at `{issuer}/.well-known/openid-configuration`, so Loom could fetch
    it. Doing so makes `loom serve` start by following a redirectable document to find a URL it will
    then fetch keys from, which is two network dependencies at startup in place of one line an
    operator pastes once. Discovery is also the only part of this that could silently *move* where
    keys come from, which is the last thing that should be dynamic.

    **`clock_skew` defaults to zero**, so a deployment that needs leeway says how much. The bound is
    `auth.MAX_SKEW` and it is small on purpose: this is for drift between two machines, not for
    extending an expiry, and a config asking for an hour is asking the wrong system for a longer
    session.

    **There is no `algorithms` key.** The accepted set is closed and asymmetric-only (`auth.ALGORITHMS`)
    — see that module for why a symmetric algorithm would make Loom able to mint the tokens it
    checks. A key here could only ever narrow a list that is already the safe one, or widen it back
    to the thing this milestone refuses, and the second is what a key invites.

    And there is no `header` key, no `trusted_proxy`, and none is coming: reading a header and
    trusting a claim is the client-supplied actor spec-v0 rejects by name.

    **`claims` is the one key here a policy reads**, and it is a declaration rather than a
    requirement — see `_parse_claims` and `auth.ClaimType`. It is under `auth:` rather than under
    `governance:` because a claim is a fact about the tokens this issuer mints, and the deployment
    that changes issuers changes both together."""

    issuer: str
    audience: str
    jwks_uri: str
    clock_skew: int = 0
    claims: Mapping[str, ClaimType] = field(default_factory=dict)


@dataclass(frozen=True)
class IngestEntry:
    """One entry under `ingest:` — a named, declared way that rows get into one object type.

    **It lives in `loom.yaml` and not in the ontology, and that placement is the design.** §7 says
    the tool set, its names and its argument namespaces are a function of the spec. Put ingest in the
    spec and something has to decide whether an `ingest_<type>` tool appears on the MCP surface — and
    the answer has to be *no*, for the reason `loom serve` exposes no raw-SQL tool: a verb that
    writes an arbitrary batch is not a declared single-object action, and handing one to an agent
    gives back everything §4's boundary was built to withhold. Keeping the declaration in the
    deployment config means no tool can be *derived* from it, structurally rather than by a rule
    someone has to remember not to break. The precedent is `governance.policies`, which also lives
    here and also names an `objectType`.

    What it therefore is: a fact about a **deployment** — this warehouse gets its Orders from a
    nightly Parquet drop — rather than a fact about the ontology, which is the same test that put
    catalogs and engines here.

    `object_type` is spelled `objectType:` in YAML, matching a policy's subject for the same reason.

    `columns` maps **property name -> source column name**, in the spec's direction (a declaration
    names the property and says where its value comes from, exactly as `Property.column` does one
    level down). Absent, it is the identity on property names. A source column no property claims is
    refused at load time rather than dropped — see `ingest.runtime`, where the data is.

    `mode` and `format` are both **required and never inferred**. A format could be guessed from a
    file extension and is not: a `.dat` has no extension to guess from, a `.csv` that is really TSV
    guesses wrong, and the guess would be made per invocation rather than declared once in the file
    an operator reviews. A mode could default to `append` and must not, because the three modes
    differ in what they *destroy* and a default would make the safest reading of an under-specified
    config the one nobody wrote down."""

    name: str
    object_type: str
    mode: str
    format: str
    columns: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LoomConfig:
    catalogs: Mapping[str, CatalogConfig] = field(default_factory=dict)
    engine: EngineConfig = field(default_factory=EngineConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    # `governance:` had exactly one key in v0 and now has three, and they still arrive flat rather
    # than behind a config object: they are unrelated facts about a deployment — what it withholds
    # from a caller, whether it will write without recording, and whether it bulk-loads at all —
    # read by different planes, not one thing with three fields.
    #
    # `policies` is unresolved on purpose: these are policies as *written*, and nothing here has an
    # ontology to check them against — `build_resolver` and `build_runtime` bind them.
    policies: tuple[Policy, ...] = ()
    edit_log: str = EDIT_LOG_OPTIONAL
    """Whether this deployment refuses to run when it cannot record what it writes.

    Read by `build_runtime` and `build_ingest`, and by nothing on the read plane: it produces no
    records, so `build_resolver` has nothing to check here. It used to be *the one governance key
    that binds a single plane*, and ingest is where that sentence narrows rather than breaks — it
    still binds no read plane, and it now binds both write planes, because it was always a demand
    about writes and there is now a second kind of one."""
    ingest: tuple[IngestEntry, ...] = ()
    """The declared loads, as written. Unresolved here for `policies`' reason: nothing in this
    module has an ontology to check an `objectType` against."""
    ingest_posture: str = INGEST_REFUSED
    """Whether this deployment performs the loads it declares. Default-refused — see
    `governance.INGEST_POSTURES` for why this default points the opposite way to `edit_log`'s."""
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
    policies, edit_log, ingest_posture = _parse_governance(doc.get("governance"), loc, diag)
    ingest = _parse_ingest(doc.get("ingest"), loc, diag)

    return LoomConfig(
        catalogs=catalogs,
        engine=engine,
        mcp=mcp,
        policies=policies,
        edit_log=edit_log,
        ingest=ingest,
        ingest_posture=ingest_posture,
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
        auth=_parse_auth(raw.get("auth"), loc, diag),
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


def _parse_auth(raw: object, loc: SourceLoc, diag: Diagnostics) -> McpAuth | None:
    """`mcp.auth`, shape-checked without opening a socket.

    Nothing here reaches the issuer. Whether the key set is fetchable is a fact about a network at
    the moment somebody asks, and `build_verifier` refuses on it at startup; whether the config
    *names* an issuer, an audience and a key set is a fact about the file, and this is where facts
    about the file are found. The same two-phase split `_parse_governance` describes."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        diag.error("'mcp.auth' must be a mapping", loc)
        return None
    check_keys(raw, _AUTH_KEYS, loc, diag, "mcp.auth")

    values: dict[str, str] = {}
    for key in ("issuer", "audience", "jwks_uri"):
        value = require(raw, key, loc, diag, "mcp.auth")
        if value is not None and (not isinstance(value, str) or not value.strip()):
            diag.error(f"'mcp.auth.{key}' must be a non-empty string, got {value!r}", loc)
            value = None
        values[key] = value.strip() if isinstance(value, str) else ""

    skew = raw.get("clock_skew", 0)
    if isinstance(skew, bool) or not isinstance(skew, int) or not 0 <= skew <= MAX_SKEW:
        diag.error(f"'mcp.auth.clock_skew' must be an integer from 0 to {MAX_SKEW} seconds, got {skew!r}", loc)
        skew = 0

    if values["jwks_uri"] and not _key_set_is_protected(values["jwks_uri"]):
        # Fetching verifying keys over cleartext hands the whole scheme to anyone on the path: swap
        # the key set and you mint principals. Loopback is the one exception, and it is the same
        # exception `host` already makes for the same reason — what cannot leave the machine cannot
        # be intercepted off it.
        diag.error(
            f"'mcp.auth.jwks_uri' is {values['jwks_uri']!r}, which is not https",
            loc,
            "keys fetched over cleartext can be replaced in transit, and a replaced key set mints "
            "principals — use https, or a loopback address for local testing",
        )

    claims = _parse_claims(raw.get("claims"), loc, diag)

    if not all(values.values()):
        return None
    return McpAuth(
        issuer=values["issuer"],
        audience=values["audience"],
        jwks_uri=values["jwks_uri"],
        clock_skew=skew,
        claims=claims,
    )


def _parse_claims(raw: object, loc: SourceLoc, diag: Diagnostics) -> Mapping[str, ClaimType]:
    """`mcp.auth.claims` — the claims a policy of this deployment may name.

    A declaration rather than a filter: it says nothing to the verifier and never makes a token
    unbelievable. A caller whose token is missing a declared claim is attested exactly as before, and
    what happens to a policy that names the missing claim is `predicate.guard_truth`'s business — it
    fails closed. Requiring a declared claim on every token was considered and rejected: it converts
    *withhold more from this caller* into *serve nobody*, and denies service to exactly the callers a
    policy would simply have subtracted more from.

    Empty is the whole of "this deployment's policies name no caller", and it is the default."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        diag.error("'mcp.auth.claims' must be a mapping of claim name to type", loc)
        return {}
    out: dict[str, ClaimType] = {}
    for name, spelling in raw.items():
        if not isinstance(name, str) or not name.strip():
            diag.error(f"'mcp.auth.claims' has a claim name that is not a string: {name!r}", loc)
            continue
        name = name.strip()
        if name in BUILT_IN_CLAIMS:
            # Not merely redundant. A config redeclaring `sub` as something else would be describing
            # a token this deployment's own verifier would have refused, and the honest reading of
            # the two statements together is that one of them is wrong.
            diag.error(
                f"'mcp.auth.claims' redeclares '{name}', which every believable token carries",
                loc,
                f"'{name}' is always available to a policy as '{BUILT_IN_CLAIMS[name].spelling}' — "
                "the verifier requires it, so it needs no declaration and cannot have another type",
            )
            continue
        if not isinstance(spelling, str) or spelling.strip() not in CLAIM_TYPES:
            allowed = ", ".join(sorted(CLAIM_TYPES))
            diag.error(
                f"'mcp.auth.claims.{name}' must be one of {allowed}, got {spelling!r}",
                loc,
                suggest(spelling, CLAIM_TYPES) if isinstance(spelling, str) else None,
            )
            continue
        out[name] = CLAIM_TYPES[spelling.strip()]
    return out


def _key_set_is_protected(uri: str) -> bool:
    """Whether a key set can be fetched without something on the path being able to replace it."""
    from urllib.parse import urlsplit

    parts = urlsplit(uri)
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and is_loopback((parts.hostname or "").strip())


def _check_transport_posture(config: McpConfig, raw: dict, loc: SourceLoc, diag: Diagnostics) -> None:
    """The three questions that need more than one key to answer.

    All of them are *errors*, so `loom validate` reports them and `loom serve` never starts — which
    is the posture `cmd_serve` already takes when a catalog will not open. A warning on the way past
    is worth nothing here: nobody reads the third line of a banner on a server that came up."""
    address_keys = [k for k in _ADDRESS_KEYS if k in raw]
    if config.transport == "stdio":
        if config.auth is not None:
            # The same rule the address keys get, applied to the key that would be most damaging to
            # ignore: a stdio deployment carrying `auth:` would read, to whoever wrote it, exactly
            # like one whose callers are authenticated. None of them is — a spawned server is handed
            # a pipe, and there is no exchange for a token to ride on.
            diag.error(
                "'mcp.auth' is set but transport is 'stdio', which carries no bearer token",
                loc,
                "a spawned server's caller cannot be attested at all — set 'transport: http' to "
                "authenticate callers, or drop 'auth:' and accept that this deployment names none",
            )
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
    if config.writes and not config.attests:
        # **This refusal narrowed rather than moved, and it is what M6's first slice bought.** M4
        # drew the limit on the bind because the bind was the only thing it could see: `actor` names
        # a deployment, so a public write surface recorded every caller under one name nobody
        # checked, and the only available answer was not to serve one. spec-v0's open edge said this
        # in as many words — "a public one may not, until this closes". `auth:` is what closes it.
        # The bind is still the thing that decides *whether the question is asked*; what changed is
        # that there is now an answer other than no.
        diag.error(
            f"'mcp.writes' is true on a non-loopback bind ({config.host!r}) with no 'mcp.auth' — "
            "refusing to serve a write surface to whoever can reach the port",
            loc,
            "declare 'mcp.auth' so every caller is attested and recorded by name, bind 127.0.0.1, "
            "or set 'writes: false'. `mcp.actor` names a deployment, not a caller, so without "
            "authentication every write here would be recorded under one name nobody checked",
        )


def _parse_ingest(raw: object, loc: SourceLoc, diag: Diagnostics) -> tuple[IngestEntry, ...]:
    """`ingest:` as written, shape-checked here and resolved against an ontology later.

    The same two-phase split `_parse_governance` describes: whether the file *names* an object type,
    a mode and a format is a fact about the file, and whether that object type exists is a fact about
    a pairing. `build_ingest` does the second.

    A **list** rather than a mapping keyed by name, unlike `catalogs:`, because a load is ordered
    prose an operator reads top to bottom and because `policies:` — the other list of named things
    that name an `objectType` — settled the shape already. Duplicate names are refused for the reason
    a policy's are: a refusal names the entry, and two entries answering to one name make the message
    ambiguous exactly when it matters."""
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        diag.error("'ingest' must be a list of load declarations", loc)
        return ()

    out: list[IngestEntry] = []
    seen: set[str] = set()
    for index, body in enumerate(raw):
        ctx = f"ingest[{index}]"
        if not isinstance(body, dict):
            diag.error(f"{ctx} must be a mapping", loc)
            continue
        check_keys(body, _INGEST_KEYS, loc, diag, ctx)

        name = require(body, "name", loc, diag, ctx)
        if name is not None and (not isinstance(name, str) or not name.strip()):
            diag.error(f"{ctx}: 'name' must be a non-empty string, got {name!r}", loc)
            name = None
        if isinstance(name, str):
            name = name.strip()
            ctx = f"ingest '{name}'"
            if name in seen:
                diag.error(f"two ingest entries are both named '{name}'", loc)
                name = None
            else:
                seen.add(name)

        object_type = require(body, "objectType", loc, diag, ctx)
        if object_type is not None and (not isinstance(object_type, str) or not object_type.strip()):
            diag.error(f"{ctx}: 'objectType' must be a non-empty string, got {object_type!r}", loc)
            object_type = None

        mode = require(body, "mode", loc, diag, ctx)
        if mode is not None and (not isinstance(mode, str) or mode.strip() not in INGEST_MODES):
            diag.error(
                f"{ctx}: 'mode' must be one of {', '.join(INGEST_MODES)}, got {mode!r}",
                loc,
                suggest(mode, INGEST_MODES) if isinstance(mode, str) else None,
            )
            mode = None

        fmt = require(body, "format", loc, diag, ctx)
        if fmt is not None and (not isinstance(fmt, str) or fmt.strip() not in INGEST_FORMATS):
            diag.error(
                f"{ctx}: 'format' must be one of {', '.join(sorted(INGEST_FORMATS))}, got {fmt!r}",
                loc,
                suggest(fmt, INGEST_FORMATS) if isinstance(fmt, str) else None,
            )
            fmt = None

        columns = _parse_ingest_columns(body.get("columns"), ctx, loc, diag)

        if name is None or object_type is None or mode is None or fmt is None:
            continue
        out.append(
            IngestEntry(
                name=name,
                object_type=str(object_type).strip(),
                mode=str(mode).strip(),
                format=str(fmt).strip(),
                columns=columns,
            )
        )
    return tuple(out)


def _parse_ingest_columns(
    raw: object, ctx: str, loc: SourceLoc, diag: Diagnostics
) -> dict[str, str]:
    """`ingest[].columns` — property name -> source column name.

    Empty is the identity mapping and is the default, so the common case (a file whose headers are
    already the property names) declares nothing. Two properties reading one source column is
    refused: it is expressible, it is almost certainly a copy-paste, and the alternative to refusing
    is silently writing one value into two places."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        diag.error(f"{ctx}: 'columns' must be a mapping of property name to source column", loc)
        return {}
    out: dict[str, str] = {}
    sources: dict[str, str] = {}
    for prop, source in raw.items():
        if not isinstance(prop, str) or not prop.strip():
            diag.error(f"{ctx}: 'columns' has a property name that is not a string: {prop!r}", loc)
            continue
        if not isinstance(source, str) or not source.strip():
            diag.error(
                f"{ctx}: 'columns.{prop}' must be a non-empty source column name, got {source!r}", loc
            )
            continue
        prop, source = prop.strip(), source.strip()
        if source in sources:
            diag.error(
                f"{ctx}: properties '{sources[source]}' and '{prop}' both read source column "
                f"'{source}'",
                loc,
                "one source column cannot fill two properties — only one of them is the mapping",
            )
            continue
        sources[source] = prop
        out[prop] = source
    return out


def _parse_governance(
    raw: object, loc: SourceLoc, diag: Diagnostics
) -> tuple[tuple[Policy, ...], str, str]:
    """`governance.policies`, `governance.edit_log` and `governance.ingest`, shape-checked here.

    The policies are resolved against an ontology later; the two postures are statements about a
    deployment and have nothing in a spec to be resolved against, so what is checked here is the
    whole of what can be checked without opening a catalog.

    This used to refuse every declared policy outright, on the grounds that silently ignoring an
    access policy is far worse than not booting. That rule did not go away when enforcement landed —
    it moved down a level, and `governance.RESERVED_KEYS` is where it lives now: a policy is refused
    key by key for exactly what Loom cannot yet enforce of it, instead of wholesale.

    The two-phase split is `negotiate`'s, seen from the other side. What can be checked without an
    ontology is checked here, where a config is read and diagnostics accumulate; what needs the spec
    is checked in `build_resolver`, which is the one place the two are paired."""
    if raw is None:
        return (), EDIT_LOG_OPTIONAL, INGEST_REFUSED
    if not isinstance(raw, dict):
        diag.error("'governance' must be a mapping", loc)
        return (), EDIT_LOG_OPTIONAL, INGEST_REFUSED
    check_keys(raw, _GOVERNANCE_KEYS, loc, diag, "governance")
    return (
        parse_policies(raw.get("policies"), loc, diag),
        parse_edit_log(raw.get("edit_log"), loc, diag),
        parse_ingest_posture(raw.get("ingest"), loc, diag),
    )
