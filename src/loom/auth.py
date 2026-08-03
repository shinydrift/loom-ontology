"""Attestation — the one source of a principal Loom will accept, and what it refuses to be.

`mcp.actor` names a **deployment**; `default_actor()` infers an **OS user**. Neither is a caller.
This module is the third kind spec-v0 named and M4 could not build: a principal **attested** by a
transport that checked it. It is the whole of what M6's first slice adds, and it adds nothing else —
a policy still names no principal, and `governance.when:` is still refused. What lands here is a
*source*, on the one transport that can carry one.

**Loom is a resource server. It is never an authorization server.** That line is what makes
"validate tokens yourself" and "refuse to be an auth server" the same decision rather than opposed
ones. Loom issues nothing, stores no credential, has no user store, no login, no refresh, no
consent, and no way to mint anything. It verifies a signature against a public key published by an
issuer an operator named, and it answers one question: *is this token one this deployment should
believe, and who does it say the caller is.*

**There is no honest middle, and it was looked for.** The obvious cheaper design is to put a proxy
in front that validates, and to trust a header it injects — `X-Auth-Subject: alice`. That requires
Loom to distinguish *this header came from the proxy* from *this header came from a client*, and on
any bind Loom can have it cannot: a loopback port is reachable by everything on the machine, which
is the same set `McpConfig` already says it cannot bound. To make the header trustworthy you need
mTLS or a shared secret — which is Loom validating a credential after all, with worse cryptography
than the one it was avoiding. So the middle collapses into either *read a header and trust a claim*,
which spec-v0 rejects by name as the client-supplied actor wearing a hat, or into this. **There is
no trusted-proxy mode, no header-trust key, and none is coming**; like a retention window, it leaves
the grammar rather than sitting in it, so a config cannot ask for it and get silence back.

**Asymmetric algorithms only**, and this is the second half of the same sentence rather than
hardening. A symmetric algorithm (`HS256`) verifies with the key that *signs*, so a deployment
holding one could mint tokens naming any principal it liked — which is being an authorization
server in the only sense that matters here. `ALGORITHMS` is therefore a closed allow-list of
asymmetric algorithms, passed explicitly to the decoder so that `alg: none` and algorithm-confusion
are refused by construction rather than by a check somebody has to remember. A config naming
anything outside it is refused at load, naming the algorithm.

**What is verified, and why each one is not optional.** `iss` and `aud` exactly, `exp`/`nbf` within
a declared skew, and the signature against the issuer's published key:

- **`aud` is the one implementations skip and the one that is load bearing.** Without it, a token
  minted for *any other service* by the same issuer is accepted here — the issuer is right, the
  signature is right, the expiry is right, and the token was never meant for this deployment. It is
  the check that makes a token *addressed* to Loom rather than merely valid somewhere.
- `iss` exactly, because the key is fetched per issuer and an unpinned issuer is a caller choosing
  who vouches for it.
- `exp`/`nbf` with a **bounded** skew (`MAX_SKEW`), because an unbounded one is an expiry check that
  does not check expiry, and the number is small for a reason: it exists for clock drift between two
  machines, not for a deployment that would like its tokens to last longer.

**Key rotation is handled by refetching on an unknown `kid`, rate limited.** An issuer rotates
without telling anybody, so a `kid` this process has never seen is the expected steady-state event
rather than an attack — refusing it would make every rotation an outage. Refetching on *every*
unknown `kid` is the other failure: a caller supplies the `kid`, so an attacker with no valid token
at all could drive one HTTP request to the issuer per call. `_Jwks` therefore refetches at most once
per `MIN_REFETCH_SECONDS`, and a `kid` still unknown after a refetch is refused. That is the whole
of the rotation story, and it is bounded in both directions.

**What this module deliberately does not do.** No OIDC discovery: `jwks_uri` is configured rather
than derived from `iss` by fetching `/.well-known/openid-configuration`, because discovery makes
starting a server depend on an extra network round trip to a document that can redirect, and the
one field Loom needs from it is one an operator can paste. No scope checks — a scope names what a
caller may do, and what a caller may do is `mcp.writes` and (later) `governance`, which are Loom's
own vocabulary rather than an issuer's. No introspection endpoint: that is a network call on the
path of every tool call, and a JWT signed by a key Loom already holds needs no second opinion.

**Where the principal goes, and where it does not.** It reaches `ActionRuntime.run` and lands in the
edit log beside `mcp.actor` — see `log.EditRecord.principal` for why *beside* rather than instead
of. It does **not** reach the resolver, and that is permanent: what will vary per call when
`governance.when:` lands is a decided `PolicySet`, selected above the resolver, never an identity
threaded into it. See `governance.py` for the argument and `ROADMAP.md` for the slice that builds
it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import McpAuth

ALGORITHMS: tuple[str, ...] = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512")
"""The signature algorithms this deployment will accept, and the reason the list is closed.

Every entry is asymmetric: verification uses a public key, so holding one lets Loom *check* a
signature and never *make* one. `HS256` and its siblings are absent by decision rather than by
omission — see the module docstring — and so is `none`, which is not an algorithm but the absence of
one. Passed explicitly to the decoder on every call, because a decoder that infers the algorithm
from the token's own header lets the token choose how it is checked."""

MAX_SKEW = 300
"""Seconds of clock skew a deployment may declare, at most.

For drift between two machines' clocks, which is seconds. A deployment wanting minutes is asking for
its tokens to outlive their expiry, which is a thing to say to the issuer rather than here."""

MIN_REFETCH_SECONDS = 60
"""How often an unknown `kid` may cost an HTTP request to the issuer.

The rate limit is the whole defence: the `kid` is caller-supplied, so without one, a caller holding
no valid token could drive one fetch per call."""

JWKS_TIMEOUT = 10
"""Seconds to wait for the issuer's key set. Bounded so a slow issuer cannot pin a tool call open."""


class AuthError(RuntimeError):
    """A token this deployment will not believe, or a verifier it cannot build.

    One type for both because they are refused in the same direction and neither is ever shown to a
    caller: a failed verification becomes a `401` with no detail (see `Principal.attest`), and a
    verifier that cannot be built refuses `loom serve` at startup with the whole message, where the
    reader is the operator who wrote the config."""


@dataclass(frozen=True)
class Principal:
    """A caller a transport checked, as everything above the transport sees it.

    Loom's own type rather than the SDK's `AccessToken`, for the reason every port in this codebase
    is Loom's own: what reaches the edit log and (later) a policy is a Loom concept with Loom's
    guarantees, and the SDK's model is a wire format that may gain fields for reasons that are not
    Loom's. The mapping happens once, at the boundary, in `from_access_token`.

    **`subject` is not a name and must never be recorded alone.** A `sub` is unique only *per
    issuer* — two issuers may both mint `alice`, and they are two people. So `label` is the
    issuer-qualified spelling, and it is what the edit log stores. Recording the bare subject would
    produce an audit trail that silently merges principals the moment a deployment trusts a second
    issuer, and the merge would be invisible in the recorded value.

    `claims` is what the token said, after verification, and it is carried whole rather than
    narrowed. Nothing reads it in this slice — it is what `governance.when:` will evaluate against,
    and it is here now because a `Principal` that dropped it would have to be widened later by a
    slice that also has to argue about the grammar. That is the one exception this module makes to
    *no field written and never read*, and it is made explicitly rather than quietly: the field is
    populated, asserted in tests, and named in the slice that consumes it."""

    subject: str
    issuer: str
    client_id: str
    claims: Mapping[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """The issuer-qualified principal, which is the only spelling safe to record or compare.

        `{issuer}#{subject}`. `#` because it cannot appear in a URL's authority or path and so
        cannot be produced by an issuer that is a URL, which is what every OIDC issuer is."""
        return f"{self.issuer}#{self.subject}"

    @classmethod
    def from_access_token(cls, token: Any) -> Principal | None:
        """The SDK's verified `AccessToken` as a `Principal`, or None if it names nobody.

        None rather than a placeholder when `subject` or the issuer claim is missing: a token that
        authenticates a *client* but names no resource owner is a real and valid OAuth shape, and
        the honest answer for it is that this call has no principal — the same answer stdio gives.
        Inventing one from `client_id` would put a deployment-shaped value back in the field this
        whole module exists to fill with a caller-shaped one."""
        claims = dict(getattr(token, "claims", None) or {})
        subject = getattr(token, "subject", None)
        issuer = claims.get("iss")
        if not subject or not issuer:
            return None
        return cls(subject=str(subject), issuer=str(issuer), client_id=str(token.client_id), claims=claims)


def current_principal() -> Principal | None:
    """The principal of the call in flight, or None when nothing attested one.

    Reads the MCP SDK's own auth contextvar rather than keeping a second one beside it. Two
    contextvars carrying the same fact is two things that can disagree, and the SDK's is the one the
    transport actually populates — Loom's verifier is what fills it, so this is the same value seen
    from the other end rather than a copy.

    None is the honest answer everywhere attestation cannot happen, and it is the *only* answer over
    stdio, under `loom run`, and under `loom query`. Nothing in this slice branches on it except the
    edit log, which records what it got."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
    except ImportError:  # pragma: no cover - reached only without the `mcp` extra installed
        return None
    token = get_access_token()
    return None if token is None else Principal.from_access_token(token)


@dataclass
class _Jwks:
    """One issuer's published keys, fetched lazily and refetched on an unknown `kid`.

    Not a general cache: it holds exactly one URL's key set for the life of the process, because a
    deployment names exactly one issuer. A dict keyed by issuer would be the shape for a deployment
    that trusts several, which is not a thing `McpAuth` can express and so not a thing this should
    pretend to support.

    The lock makes a refetch happen once rather than once per concurrent caller. It is real
    contention rather than the decorative kind `build_mcp_server` argues against: `verify_token` is
    `async` and awaits a network call, so two requests genuinely interleave here — which is exactly
    the property the *tool handlers* deliberately do not have."""

    uri: str
    _keys: dict[str, Any] = field(default_factory=dict)
    _fetched_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def key_for(self, kid: str | None) -> Any:
        """The verifying key for a token's `kid`, fetching or refetching if it is not known.

        A token with no `kid` is refused rather than tried against every key. Trying them all is how
        a key that was rotated *out* keeps working, and the header field exists precisely so nobody
        has to guess."""
        if not kid:
            raise AuthError("token header carries no 'kid', so no published key can be selected for it")
        with self._lock:
            if kid in self._keys:
                return self._keys[kid]
            now = time.monotonic()
            if self._fetched_at and now - self._fetched_at < MIN_REFETCH_SECONDS:
                # Rate limited rather than refused-forever: the `kid` is caller-supplied, so an
                # unknown one is not evidence of a rotation, and one fetch per call would be a
                # request amplifier anybody could drive without holding a token at all.
                raise AuthError(f"token names an unknown key '{kid}' and the key set was refetched too recently")
            self._keys = self._fetch()
            self._fetched_at = now
            if kid not in self._keys:
                raise AuthError(f"token names key '{kid}', which the issuer's published key set does not contain")
            return self._keys[kid]

    def _fetch(self) -> dict[str, Any]:
        import json
        import urllib.request

        from jwt import PyJWK

        try:
            with urllib.request.urlopen(self.uri, timeout=JWKS_TIMEOUT) as resp:  # noqa: S310 - scheme is checked at load
                document = json.loads(resp.read())
        except Exception as e:
            raise AuthError(f"could not fetch the issuer's key set from '{self.uri}': {e}") from e
        keys = {}
        for raw in document.get("keys", []):
            kid = raw.get("kid")
            if not kid or raw.get("kty") == "oct":
                # A symmetric key in a *published* key set is either a mistake or a trap, and either
                # way it is a key that could sign. Skipped here as well as refused by `ALGORITHMS`,
                # so it cannot be selected even by name.
                continue
            try:
                keys[str(kid)] = PyJWK(raw).key
            except Exception:  # pragma: no cover - a malformed entry beside good ones
                continue
        if not keys:
            raise AuthError(f"the key set at '{self.uri}' published no usable asymmetric keys")
        return keys


@dataclass
class TokenVerifier:
    """Loom's implementation of the MCP SDK's `TokenVerifier` protocol — the attestation itself.

    The SDK supplies the plumbing this rides on and none of the judgement: `BearerAuthBackend` pulls
    the header apart, `AuthContextMiddleware` puts the result where a handler can read it, and
    `RequireAuthMiddleware` turns its absence into a `401`. What none of them does is decide whether
    a token is *believable*, because that is a deployment's question about an issuer. This is that
    decision, and it is the reason this module exists rather than a config key pointing at the SDK.

    Returns the SDK's `AccessToken` because that is the protocol's return type; `Principal` is what
    Loom reads back out at the boundary. Returning `None` is the protocol's spelling of "not
    authenticated", and every failure funnels into it — a caller learns `401` and nothing more,
    while the operator's log line carries the reason. Telling a caller *why* a token was refused
    tells an attacker which of five things to change next."""

    auth: McpAuth
    _jwks: _Jwks | None = None

    def __post_init__(self) -> None:
        self._jwks = _Jwks(uri=self.auth.jwks_uri)

    async def verify_token(self, token: str) -> Any:
        """Verify a bearer token. Returns an `AccessToken`, or None if it is not believable.

        `async` because the protocol is, not because anything here awaits — the fetch is a blocking
        call inside a lock, which is the honest shape for something that happens once per rotation
        rather than once per call. Making it `await`-native would buy concurrency on the one path
        where serialization is free and the alternative is a second HTTP client dependency."""
        import jwt

        try:
            claims = self._decode(jwt, token)
        except AuthError:
            return None
        except Exception:
            # Every decoder failure is one answer to the caller. The distinctions the library draws
            # — expired, bad audience, bad signature — are exactly the distinctions worth *not*
            # publishing.
            return None
        return self._access_token(token, claims)

    def _decode(self, jwt: Any, token: str) -> dict[str, Any]:
        header = jwt.get_unverified_header(token)
        if header.get("alg") not in ALGORITHMS:
            # Refused before the key is even selected, so an `alg` this deployment does not accept
            # can never reach the decoder — which is what makes algorithm confusion unreachable
            # rather than merely unlikely.
            raise AuthError(f"token is signed with '{header.get('alg')}', which is not an accepted algorithm")
        assert self._jwks is not None
        key = self._jwks.key_for(header.get("kid"))
        return jwt.decode(
            token,
            key=key,
            algorithms=list(ALGORITHMS),
            issuer=self.auth.issuer,
            audience=self.auth.audience,
            leeway=self.auth.clock_skew,
            options={"require": ["exp", "iss", "aud", "sub"], "verify_signature": True},
        )

    @staticmethod
    def _access_token(token: str, claims: Mapping[str, Any]) -> Any:
        from mcp.server.auth.provider import AccessToken

        scopes = claims.get("scope")
        return AccessToken(
            token=token,
            # The OAuth client, which is not the principal: `azp`/`client_id` names the software,
            # `sub` names who it is acting for. Both are recorded because they answer different
            # questions, and conflating them is what `mcp.actor` already does for a deployment.
            client_id=str(claims.get("azp") or claims.get("client_id") or claims.get("aud") or ""),
            scopes=scopes.split() if isinstance(scopes, str) else list(scopes or []),
            expires_at=int(claims["exp"]) if "exp" in claims else None,
            subject=str(claims.get("sub")) if claims.get("sub") else None,
            claims=dict(claims),
        )


def build_verifier(auth: McpAuth) -> TokenVerifier:
    """The verifier for a deployment's `mcp.auth`, or a refusal naming what is missing.

    Refuses here rather than on the first request for `build_resolver`'s reason, which this codebase
    has now paid for four times: a deployment that cannot authenticate should not reach the point of
    advertising tools it will `401` on every call. The import check is the same shape as an engine
    capability — `auth:` without the `[auth]` extra is a config asking for something this
    installation cannot do, and the message says which command fixes it."""
    try:
        import jwt  # noqa: F401
    except ImportError as e:
        raise AuthError(
            "'mcp.auth' needs the JWT verifier, which is not installed — "
            "install it with: pip install 'loom-ontology[auth]'"
        ) from e
    return TokenVerifier(auth=auth)
