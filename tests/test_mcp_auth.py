"""Attested identity over HTTP — a caller this deployment checked, recorded by name.

`test_mcp_http.py` is the same transport with nobody named; this file is only what *attestation*
adds. It is a separate file for that file's own reason: what two transports share is asserted
without a transport, and what needs a socket is asserted over one. A token needs a socket.

**The load-bearing test here is `test_each_caller_sees_its_own_principal`**, and it is worth saying
why before the easy ones. The principal reaches a tool handler through a `contextvar` that the SDK's
`AuthContextMiddleware` sets per ASGI request. Contextvars propagate to tasks created *from* the
setting context and not to tasks that already exist, so "the handler observes the right principal"
is a claim about how the MCP session manager dispatches — not something the type system checks, and
not something that stays true by wishing. Two clients, two issuers' worth of difference in their
tokens, overlapping in the server's inbox, each finding its own name in its own edit record, is the
assertion that fails if that ever stops holding.

It is also the first thing in this codebase that differs between two calls of one process. Every
earlier claim about a served process was that it holds *one* of everything — one `Resolver`, one
`ActionRuntime`, one DuckDB connection under three global aliases — and that this is safe because
calls do not overlap inside it. That is all still true and still asserted where it was. What is new
is a value that is per *exchange* rather than per process.

**The last section is what that value is for.** M6's second slice conditions a policy on the caller,
and the tests under "a deployment whose policies name the caller" are the whole slice end to end: two
tokens, one process, one tool set, different rows. They are here rather than in `test_governance.py`
for this file's own reason — what needs a socket is asserted over one, and a claim signed by an
issuer and checked by a verifier needs the socket to be worth anything.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")
pytest.importorskip("duckdb", reason="needs the [duckdb] extra")
pytest.importorskip("mcp", reason="needs the [mcp] extra")
jwt = pytest.importorskip("jwt", reason="needs the [auth] extra")

import httpx2  # noqa: E402 - a transitive dependency of mcp, imported after the skip guard

from loom.catalog.base import EDIT_LOG_TABLE  # noqa: E402

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "retail"
TIMEOUT = 60
STARTUP_TIMEOUT = 45
ACTOR = "agent:auth-test"
ISSUER = "https://issuer.test"
AUDIENCE = "loom-retail"
KID = "test-key-1"
PROTOCOL_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---- an issuer ---------------------------------------------------------------------


@dataclass
class Issuer:
    """A local authorization server, reduced to the only thing Loom asks of one: a key set.

    Loom is a resource server and never an authorization server, so there is nothing else to
    imitate — no token endpoint, no consent, no client registry. This publishes a JWKS over loopback
    http, which `mcp.auth.jwks_uri` permits for exactly this case and refuses everywhere else."""

    jwks_uri: str
    private_pem: bytes

    def token(self, subject: str, **overrides) -> str:
        """A believable token, or one bent in exactly one way.

        Overrides land in the *claims* only. Bending the algorithm or the key is not expressible
        here on purpose: those produce a token this issuer did not sign, which is a different test
        with a different setup, and hiding it behind a keyword would make the two look alike."""
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": subject,
            "exp": now + 300,
            "iat": now,
            "azp": "client-under-test",
        }
        claims.update(overrides)
        return jwt.encode(claims, self.private_pem, algorithm="RS256", headers={"kid": KID})

    def headers(self, subject: str, **overrides) -> dict:
        return {"Authorization": f"Bearer {self.token(subject, **overrides)}"}


@pytest.fixture(scope="module")
def issuer():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    document = json.dumps({"keys": [jwk]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's own spelling
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(document)))
            self.end_headers()
            self.wfile.write(document)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield Issuer(jwks_uri=f"http://127.0.0.1:{server.server_port}/jwks.json", private_pem=private_pem)
    finally:
        server.shutdown()


# ---- a deployment that attests -------------------------------------------------------


@dataclass
class Served:
    url: str
    project: Path
    stderr: Path


@pytest.fixture(scope="module")
def served(tmp_path_factory, issuer):
    """One `loom serve` over HTTP with `mcp.auth` declared and writes on."""
    import importlib.util

    project = tmp_path_factory.mktemp("serve-auth") / "retail"
    shutil.copytree(EXAMPLE, project, ignore=shutil.ignore_patterns(".warehouse"))
    port = _free_port()
    config = project / "loom.yaml"
    config.write_text(
        config.read_text().replace("  transport: stdio\n", f"  transport: http\n  port: {port}\n")
        + f"  writes: true\n  actor: {ACTOR}\n"
        + "  auth:\n"
        + f"    issuer: {ISSUER}\n"
        + f"    audience: {AUDIENCE}\n"
        + f"    jwks_uri: {issuer.jwks_uri}\n"
    )

    spec = importlib.util.spec_from_file_location("auth_seed", project / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.seed(project)

    stdout, stderr = project / "serve.out", project / "serve.err"
    url = f"http://127.0.0.1:{port}/mcp"
    with stdout.open("w") as out, stderr.open("w") as err:
        process = subprocess.Popen(
            [sys.executable, "-m", "loom.cli", "serve", str(project / "ontology")], stdout=out, stderr=err
        )
        try:
            _await_listening(process, url, stderr, issuer)
            yield Served(url=url, project=project, stderr=stderr)
        finally:
            process.terminate()
            process.wait(timeout=30)


def _await_listening(process, url: str, stderr: Path, issuer: Issuer) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"loom serve exited with {process.returncode}:\n{stderr.read_text()}")
        try:
            httpx2.post(url, json={}, headers=PROTOCOL_HEADERS, timeout=2)
            return
        except httpx2.HTTPError:
            time.sleep(0.2)
    raise RuntimeError(f"loom serve never listened on {url}:\n{stderr.read_text()}")


async def _drive(url: str, headers: dict, calls: list[tuple[str, dict]]):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    # The bearer token rides on the client rather than on the call: it authenticates the *exchange*,
    # which includes the `initialize` this session opens with, and a transport that only presented
    # it on tool calls would be refused before it ever made one.
    async with httpx2.AsyncClient(headers=headers) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return [await session.call_tool(name, args) for name, args in calls]


def _run(url: str, headers: dict, calls: list[tuple[str, dict]]):
    return asyncio.run(asyncio.wait_for(_drive(url, headers, calls), TIMEOUT))


def _edits(served: Served) -> list[dict]:
    from loom.catalog import open_catalogs
    from loom.config import find_config, load_config
    from loom.errors import Diagnostics

    diag = Diagnostics()
    config = load_config(find_config(served.project / "ontology"), diag)
    return open_catalogs(config)["local"].scan(EDIT_LOG_TABLE).to_pylist()


# ---- what a token buys, and what its absence costs -----------------------------------


def test_a_request_without_a_token_never_reaches_a_tool(served):
    """401, and it is about the exchange rather than about a tool.

    §7's rule is that every tool outcome is a `200` carrying content, and that an HTTP status answers
    *did this exchange happen* while `isError` answers *did this call become a run*. An unauthenticated
    request produces no tool result at all, so the two never disagree here — there is nothing to
    disagree with."""
    response = httpx2.post(served.url, json={}, headers=PROTOCOL_HEADERS, timeout=10)
    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers


@pytest.mark.parametrize(
    "overrides, why",
    [
        ({"aud": "some-other-service"}, "a token minted for another service by the right issuer"),
        ({"iss": "https://attacker.test"}, "a token from an issuer this deployment does not name"),
        ({"exp": int(time.time()) - 60}, "an expired token"),
        ({"sub": None}, "a token naming no resource owner"),
    ],
)
def test_a_token_this_deployment_should_not_believe_is_refused(served, issuer, overrides, why):
    """Each of the four checks, one at a time, and `aud` is the one that matters most.

    Without the audience check the first case passes: right issuer, right signature, unexpired, and
    addressed to something else entirely."""
    response = httpx2.post(
        served.url,
        json={},
        headers={**PROTOCOL_HEADERS, **issuer.headers("alice", **overrides)},
        timeout=10,
    )
    assert response.status_code == 401, why


def test_a_symmetric_signature_is_refused_however_valid(served, issuer):
    """`HS256` with a key the caller chose. Refused on the algorithm, before any key is selected.

    This is the shape algorithm confusion takes: the token is well-formed and the claims are right,
    and the only question is whether the verifier will accept a signature it could also have
    produced. `auth.ALGORITHMS` answers before the decoder ever runs."""
    now = int(time.time())
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "alice", "exp": now + 300},
        "a-shared-secret-long-enough-to-not-warn",
        algorithm="HS256",
        headers={"kid": KID},
    )
    response = httpx2.post(
        served.url, json={}, headers={**PROTOCOL_HEADERS, "Authorization": f"Bearer {token}"}, timeout=10
    )
    assert response.status_code == 401


def test_an_attested_caller_can_read(served, issuer):
    """The ordinary case: a believable token, and the same tool set that was always there."""
    results = _run(served.url, issuer.headers("alice"), [("get_customer", {"key": "c1"})])
    assert json.loads(results[0].content[0].text)["found"] is True


def test_the_edit_log_records_the_caller_beside_the_deployment(served, issuer):
    """Both, and they are different answers to different questions.

    `actor` is what the operator declared this deployment to be; `principal` is who the transport
    checked. A log holding only the first cannot tell two callers of one deployment apart, which is
    the whole of what this milestone added."""
    _run(served.url, issuer.headers("alice"), [("run_upgrade_tier", {"parameters": {"customer": "c1", "newTier": "gold"}})])
    row = next(r for r in _edits(served) if r["object_key"] == "c1")
    assert row["actor"] == ACTOR
    assert row["principal"] == f"{ISSUER}#alice"


def test_the_principal_is_issuer_qualified_rather_than_a_bare_subject(served, issuer):
    """`{iss}#{sub}`, because a `sub` is unique only per issuer.

    Recording `alice` alone produces a trail that silently merges two people the day a second issuer
    is trusted, and the merge is invisible in the recorded value — there is nothing in the row to
    notice it by."""
    row = next(r for r in _edits(served) if r["object_key"] == "c1")
    assert row["principal"] == f"{ISSUER}#alice"
    assert row["principal"] != "alice"


def test_each_caller_sees_its_own_principal(served, issuer):
    """Two clients, two tokens, overlapping — and each run recorded under its own caller.

    The claim under test is that the principal is per *exchange* and never leaks across two of them.
    It could fail in two directions and both would be silent: a contextvar that does not propagate
    into the dispatching task gives every call `None`, and one set on a shared object gives the
    second caller the first one's name. Either would leave the edit log confidently wrong, which is
    the worst thing an audit record can be.

    Written against two *different* subjects on purpose. Two identical tokens would pass this test
    while proving nothing."""

    async def both():
        async with asyncio.timeout(TIMEOUT):
            return await asyncio.gather(
                _drive(served.url, issuer.headers("alice"), [("run_upgrade_tier", {"parameters": {"customer": "c2", "newTier": "gold"}})]),
                _drive(served.url, issuer.headers("bob"), [("run_upgrade_tier", {"parameters": {"customer": "c3", "newTier": "silver"}})]),
            )

    asyncio.run(both())
    rows = {r["object_key"]: r["principal"] for r in _edits(served)}
    assert rows["c2"] == f"{ISSUER}#alice"
    assert rows["c3"] == f"{ISSUER}#bob"


# ---- what attestation is, asserted rather than described -----------------------------


def test_only_asymmetric_algorithms_are_accepted():
    """The list is closed, and a symmetric entry would make Loom able to mint what it checks.

    Verifying an `HS*` signature uses the key that *signs* it, so a deployment holding one is an
    authorization server in the only sense that matters here. `none` is absent for the same reason
    it is not an algorithm. Asserted rather than left to the docstring, because the failure mode of
    a widened list is invisible: every token still verifies."""
    from loom.auth import ALGORITHMS

    assert ALGORITHMS
    assert not [a for a in ALGORITHMS if a.startswith("HS")]
    assert "none" not in [a.lower() for a in ALGORITHMS]
    assert all(a[:2] in ("RS", "ES", "PS") for a in ALGORITHMS)


def test_a_principal_is_issuer_qualified_and_never_a_bare_subject():
    """`sub` is unique only per issuer, so the recorded spelling has to carry both."""
    from loom.auth import Principal

    alice = Principal(subject="alice", issuer="https://a.test", client_id="c")
    other = Principal(subject="alice", issuer="https://b.test", client_id="c")
    assert alice.label != other.label
    assert alice.label == "https://a.test#alice"


def test_a_token_naming_no_resource_owner_yields_no_principal():
    """A token that authenticates a *client* and names no subject is a real OAuth shape, and the
    honest answer for it is that this call has no principal — the same answer stdio gives.
    Inventing one from `client_id` would put a deployment-shaped value back in the field this
    module exists to fill with a caller-shaped one."""
    from dataclasses import dataclass

    from loom.auth import Principal

    @dataclass
    class Token:
        client_id: str = "svc"
        subject: str | None = None
        claims: dict | None = None

    assert Principal.from_access_token(Token()) is None
    assert Principal.from_access_token(Token(subject="alice", claims={})) is None  # no issuer
    named = Principal.from_access_token(Token(subject="alice", claims={"iss": "https://a.test"}))
    assert named is not None and named.label == "https://a.test#alice"


def test_the_claims_a_token_carried_survive_onto_the_principal():
    """Carried whole rather than narrowed. Nothing reads them in this slice — they are what
    `governance.when:` will evaluate against, and the field is populated and asserted now so the
    slice that consumes it argues about the grammar and not about the shape."""
    from dataclasses import dataclass

    from loom.auth import Principal

    @dataclass
    class Token:
        client_id: str = "svc"
        subject: str | None = "alice"
        claims: dict | None = None

    p = Principal.from_access_token(Token(claims={"iss": "https://a.test", "groups": ["support"]}))
    assert p.claims["groups"] == ["support"]


def test_no_principal_is_current_without_a_transport():
    """`loom query`, `loom run` and stdio, all at once: nothing set a context, so nobody is named."""
    from loom.auth import current_principal

    assert current_principal() is None


# ---- a deployment whose policies name the caller -------------------------------------
#
# M6's second slice, end to end. Everything above proves a caller can be *named*; this proves the
# only thing that names one is worth doing — two callers of one process, one tool set, and
# different rows, decided by claims a real issuer signed and a real verifier checked.


GOVERNED = """
governance:
  policies:
    - name: own-orders
      objectType: Order
      rows: "object.customerId == principal.sub"
    - name: gold-desk
      objectType: Customer
      when: "principal.groups contains 'gold-desk'"
      rows: "object.tier == 'gold'"
"""


@pytest.fixture(scope="module")
def governed(tmp_path_factory, issuer):
    """A second `loom serve`, with policies that name the caller and the claims they may name."""
    import importlib.util

    project = tmp_path_factory.mktemp("serve-governed") / "retail"
    shutil.copytree(EXAMPLE, project, ignore=shutil.ignore_patterns(".warehouse"))
    port = _free_port()
    config = project / "loom.yaml"
    config.write_text(
        config.read_text().replace("  transport: stdio\n", f"  transport: http\n  port: {port}\n")
        + "  auth:\n"
        + f"    issuer: {ISSUER}\n"
        + f"    audience: {AUDIENCE}\n"
        + f"    jwks_uri: {issuer.jwks_uri}\n"
        + "    claims:\n      groups: string[]\n"
        + GOVERNED
    )

    spec = importlib.util.spec_from_file_location("governed_seed", project / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.seed(project)

    stdout, stderr = project / "serve.out", project / "serve.err"
    url = f"http://127.0.0.1:{port}/mcp"
    with stdout.open("w") as out, stderr.open("w") as err:
        process = subprocess.Popen(
            [sys.executable, "-m", "loom.cli", "serve", str(project / "ontology")], stdout=out, stderr=err
        )
        try:
            _await_listening(process, url, stderr, issuer)
            yield Served(url=url, project=project, stderr=stderr)
        finally:
            process.terminate()
            process.wait(timeout=30)


def _objects(result) -> list[dict]:
    return json.loads(result.content[0].text)["objects"]


def test_two_callers_of_one_deployment_are_filtered_by_who_they_are(governed, issuer):
    """**The slice, whole.** One process, one tool set, two tokens, two answers.

    `own-orders` folds the caller into a predicate: `object.customerId == principal.sub` is a
    comparison against a *literal* by the time the query is compiled, which is what "a principal
    never reaches the resolver" means in practice. The seeded orders are two for `c1` and three for
    `c2`, so the counts are the assertion — same tool, same schema, different rows."""
    for subject, expected in (("c1", 2), ("c2", 3), ("c9", 0)):
        (orders,) = _run(governed.url, issuer.headers(subject, groups=["support"]), [("list_order", {})])
        assert [row["customerId"] for row in _objects(orders)] == [subject] * expected


def test_a_guard_decides_whether_a_policy_applies_to_this_caller(governed, issuer):
    """`when:` is an **implication**: a caller the guard excludes has the policy withhold nothing
    from them, which is exactly why it cannot be sugar for a longer `rows:` — the same text inside
    the predicate would withhold everything instead.

    The seeded customers are one gold, one silver, one bronze."""
    inside = issuer.headers("c1", groups=["gold-desk", "support"])
    outside = issuer.headers("c1", groups=["support"])
    (restricted,) = _run(governed.url, inside, [("list_customer", {})])
    (unrestricted,) = _run(governed.url, outside, [("list_customer", {})])
    assert [row["tier"] for row in _objects(restricted)] == ["gold"]
    assert sorted(row["tier"] for row in _objects(unrestricted)) == ["bronze", "gold", "silver"]


def test_a_caller_whose_token_lacks_the_claim_gets_the_policy_applied(governed, issuer):
    """**A missing claim is not a false one**, and it fails in the withholding direction.

    There is nobody to report an undecided guard to — telling this caller that a policy did or did
    not apply to them is §6.1's existence oracle, and the operator is not in the exchange. So the
    policy applies, which subtracts more, which is the safe direction. Note it is the *opposite*
    disposition from a surface that cannot attest at all: that one is decidable at pairing time with
    an operator to tell, and there this codebase refuses."""
    (result,) = _run(governed.url, issuer.headers("c1"), [("list_customer", {})])
    assert [row["tier"] for row in _objects(result)] == ["gold"]


def test_the_tool_set_is_the_same_for_every_caller(governed, issuer):
    """§7's claim, under the one feature that could have broken it.

    A `rows:` predicate announces nothing — a withheld row is simply absent — so conditioning it
    costs the surface nothing. A *mask* announces itself in the description, the `filter` schema and
    `masked`, which is why `mask:` beside `when:` is refused: the alternative is a tool set that is
    a function of the caller."""

    async def tools(headers):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with httpx2.AsyncClient(headers=headers) as http_client:
            async with streamable_http_client(governed.url, http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    return [(t.name, t.description, t.input_schema) for t in listed.tools]

    one = asyncio.run(asyncio.wait_for(tools(issuer.headers("c1", groups=["gold-desk"])), TIMEOUT))
    two = asyncio.run(asyncio.wait_for(tools(issuer.headers("c2", groups=["support"])), TIMEOUT))
    assert one == two
    assert not any("Withheld" in (description or "") for _, description, _ in one)


def test_loom_query_refuses_the_config_this_server_is_running(governed):
    """**Decision 2, at the surface that cannot attest anybody.**

    The same `loom.yaml` the server above is serving, read by the command that has no transport.
    Applying only the unconditional policies would give this caller *less* subtraction — policies
    subtract and never add — so `loom query` would become the way to read what the served surface
    withholds. It refuses instead, before reading anything, with an operator there to read why."""
    result = subprocess.run(
        [sys.executable, "-m", "loom.cli", "query", "Order", str(governed.project / "ontology")],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )
    assert result.returncode == 1
    assert "own-orders" in result.stderr and "nobody is attested here" in result.stderr
