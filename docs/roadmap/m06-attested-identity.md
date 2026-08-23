[← Roadmap index](../ROADMAP.md)

# ✅ Done — M6: A per-caller identity over MCP

*Goal: a caller this deployment checked, and a policy that can name one.*

M5 closed with `when:` as the only key left in `RESERVED_KEYS`, and with a sentence on both sides of
the same contradiction: the resolver receives no identity "deliberately and now permanently for
everything §6.1 can express", while `when:` is by construction outside that set. Four questions had
to be settled before any of it could be built, and two of them ended in refusals.

**1. Does a principal reach the resolver? No — a decided `PolicySet` does.**

M5 promised two things and only one is load bearing. *The resolver receives no identity* is the
claim; *the `PolicySet` is the same for every call* was a **consequence**, and `PolicySet.masks`
already named the condition under which it lapses ("the thing that stops being true when a principal
arrives per call"). So the consequence gives way and the claim survives.

`bind_policies` splits by *time*, not by responsibility. Bind time keeps every static spec × config
refusal — all four mask refusals, the predicate subset, undeclared properties, the primary-key rule —
in `build_resolver`, unchanged, firing whether or not a caller ever arrives. Per call adds only
*selection*: which already-bound policies apply. Every enforcement site (`Resolver._projection`,
`Resolver._table`, `_Run._project`, `_Run._admitted`) is untouched, because what reaches them is
still a set that is already decided.

What makes this sound rather than a technicality: **a principal is constant for the duration of a
call**, so everything it conditions folds before the call begins — including the hardest case, a row
predicate naming the caller (`object.ownerId == principal.subject`), which substitutes to a literal
at selection time. There is no policy shape that needs an identity *at* the enforcement site. The
alternative — threading a principal down into the resolver — would add a second axis to "enforced one
rung below every surface that asks" and buy nothing.

The assertion that makes it cheap: a program with no conditional policies returns the **same
`PolicySet` object** for every caller, so M5's deployment-scoped path is provably unchanged rather
than argued to be equivalent.

**2. A surface that cannot attest refuses a `when:` config. (Refusal.)**

The tempting alternative — treat an unattested caller as principal-less and apply only the
unconditional policies — is disqualified by M5's own invariant rather than by taste. **Policies
subtract, never add.** Every `when:` policy is *"under condition C, withhold X"*, so skipping the
conditional ones gives the unattested caller **less subtraction — it sees more**. `loom query`
becomes precisely the way to read what the governed MCP surface withholds: the back door the whole
read path was built not to be.

Fail-closed (an undecidable `when:` applies) was considered and rejected. It mirrors §6.1's "a row is
admitted only on true", but that rule exists because *per row there is no channel and per call the
report is itself an oracle*. Neither bind holds here: `loom query` knows, before reading anything,
that it can never attest anybody. That is not undecided — it is **decidably unattestable, at bind,
with a channel to report it**, and where this codebase can decide at pairing time and has somewhere
to print, its posture is refuse.

Two things this sharpens. It is **not** "direct commands vs MCP": a spawned stdio server carries no
bearer token either, so the predicate is *can this surface attest* — which is why it landed as
`McpConfig.attests` rather than as a condition three call sites re-derive. And it is the **first
surface-conditioned refusal** in the codebase; `writes` on a non-loopback bind is config-level, so
that precedent is weaker than it looks. The defence is the distinction `governance.py` has now drawn
three times: this makes the file mean **one** thing and makes two surfaces refuse it *loudly*. A
refusal is loud; a filter is silent. "One meaning, two refusals" is not "two meanings" — nothing
reads differently anywhere; some things do not read at all.

**3. Loom validates tokens itself, as a resource server that is never an authorization server.
(Refusal.)**

The middle was looked for and does not exist. A proxy that validates and injects a header requires
Loom to distinguish *from the proxy* from *from a client*, and on any bind it can have it cannot — a
loopback port is reachable by everything on the machine, which `McpConfig` already says it cannot
bound. Making the header trustworthy needs mTLS or a shared secret, which is Loom validating a
credential after all, with worse cryptography than the one it was avoiding. So the middle collapses
into *read a header and trust a claim* — the client-supplied actor spec-v0 rejects by name — or into
this. **There is no trusted-proxy mode and none is coming.**

"Validate" and "refuse to be an auth server" turn out to be the same decision, not opposed ones, and
the line that makes them so is MCP's own profile: Loom issues nothing, stores no credential, has no
user store, no login, no refresh, no consent, and no way to mint anything. Its second half is
`ALGORITHMS`: **asymmetric only**, because a symmetric algorithm verifies with the key that signs,
and a deployment holding one would be an authorization server in the only sense that matters.

**4. Per-call scope, not per-call construction — and the alias problem is not this milestone's.**

Per-call construction fails on its own merits: a `Resolver` per call means catalogs and an engine per
call, and it does not fix the `t0`/`t1`/`m0` race — it multiplies the racers.

More importantly, **a prediction made twice was wrong and is corrected where it was written**
(`build_server`, `build_mcp_server`, and spec-v0's open edge). Those said the milestone attesting a
principal would have to make the per-process objects per-caller *and* fix the DuckDB aliases "anyway".
It did not. Two forces were being treated as one: what forces per-caller objects is a policy that
varies *by* caller; what forces the alias fix is two calls *in flight at once*. A per-call principal
is neither. Handlers are still synchronous, nothing overlaps, and the alias problem belongs to
whatever milestone makes a handler `async`. That correction is worth roughly half the milestone.

---

## First slice — attestation, with a source and a reader

Sliced this way for a reason worth recording, because it is **not** the seam-first plan this
milestone was scoped with. A seam-only slice — `PolicyProgram`, per-call selection, `when:` still
refused — would introduce a `Principal` type nothing produces and a selection with one possible
argument: structure whose second case does not exist, which is this codebase's own
*no field written and never read*, one level up. The seam's only consumer is `when:`, and `when:`
cannot ship before a principal has a source. So the source ships first; the seam ships with the
clause that needs it. Decisions 1 and 4 are *settled* here and *built* next, which is the order the
milestone asked for — settle before the token work, not after.

- **`mcp.auth`** — `issuer`, `audience`, `jwks_uri`, `clock_skew`. All required, none derived.
  Discovery is deliberately absent: it makes startup follow a redirectable document to find a URL it
  will then fetch keys from, and it is the only part of this that could silently *move* where keys
  come from.
- **`auth.TokenVerifier`** — `iss`, `aud`, `exp`/`nbf` within a bounded skew, signature, closed
  algorithm allow-list, JWKS refetch on an unknown `kid` rate-limited to once a minute. The rate
  limit is the whole defence there: the `kid` is caller-supplied, so without it a caller holding no
  valid token could drive one issuer fetch per call. **`aud` is the load-bearing check** — without
  it, a token minted for any other service by the same issuer is accepted here.
- **The MCP SDK supplies the plumbing and none of the judgement.** `BearerAuthBackend`,
  `AuthContextMiddleware` and `RequireAuthMiddleware` were already there; what no SDK can decide is
  whether a token is *believable*, which is the whole of `auth.py`. A token is **required** where
  `auth:` is declared — accepting unauthenticated callers beside authenticated ones would give one
  deployment two classes of caller, and the un-tokened class would run the same writes recorded as
  nobody.
- **`principal` in `_loom_meta.edits`, beside `actor` and never instead of it.** `actor` is true
  about a deployment and `principal` about a caller; both are true at once, and a log holding only
  the first cannot tell two callers of one deployment apart. Issuer-qualified (`{iss}#{sub}`),
  because a `sub` is unique only per issuer and a bare one silently merges two people the day a
  second issuer is trusted.
- **A public bind may write, once its callers are attested.** spec-v0 promised exactly this ("a
  public one may not, *until this closes*"). The M4 refusal narrowed rather than moved: the bind
  still decides whether the question is asked.

Three things found by building it, each of which changed the code:

- **A pre-existing log table would have swallowed the principal in silence.** `append_edit` builds
  its Arrow batch against the *table's own* schema and `pa.Table.from_pylist` drops keys that schema
  lacks, so a log created before this slice accepts every append, reports success, and discards the
  caller — leaving a record indistinguishable from a run that genuinely had none. That is the trap
  this module already named as *the columns are forever*. The fix is a **refusal**
  (`require_principal_column`, in `build_runtime`, only when the deployment attests), not a widened
  port: giving `EditLogWriter` a verb that alters a table would spend the guarantee that keeps DDL
  out of the action runtime's reach. A test pins the silent drop, so the refusal can go the day it
  stops being true.
- **`RequireAuthMiddleware` does not guard on `scope["type"]`.** Mounted app-wide it answers the ASGI
  *lifespan* scope with a `401`, the session manager's task group never starts, and every request
  fails with *Task group is not initialized* — a startup failure that surfaces as a `500` on the
  first tool call. Starlette's own `AuthenticationMiddleware` guards against exactly this. So the
  stack wraps the **route's endpoint** rather than the app.
- **The contextvar reaches a synchronous handler, and that is now asserted rather than assumed.**
  Contextvars propagate to tasks created *from* the setting context and not to tasks that already
  exist, so "the handler sees the right principal" is a claim about how the SDK dispatches. Two
  overlapping clients with different subjects, each finding its own name in its own edit record, is
  the test that fails if it stops holding. It is also the first value in this codebase that differs
  between two calls of one process — the shape a policy will later be selected by.

`mcp.actor` keeps both properties M5 asserted: **declared, never inferred** (an attested subject is
neither — it is the third kind spec-v0 named), and it still reaches the edit log. What it no longer
does is reach it *alone*.

## Second slice — `when:`, and the half of a policy that may name a caller (this PR)

Everything the first slice's four decisions predicted, plus one refusal they did not: **half a
policy may name the caller.** `rows:` may be conditioned — by a `when:` guard, by a
`principal.<claim>` inside the predicate, or both — and `mask:` may not, ever.

**1. A conditional mask is refused, and the argument is §6.1's own first rule.** *The schema is
public; the data is not.* A mask announces itself in the tool description, in the `filter` schema and
in `masked` on every result, and §7 says the tool set and its argument namespaces are a function of
the spec. A per-caller mask therefore has three possible spellings, and each is something this
codebase already refuses somewhere else: assemble the tool set per caller (the surface becomes a
function of the caller); announce the worst case to everyone (narrowing the surface to fit, which §6
will not do even for an engine); or stop announcing (the rule a mask exists under). A row predicate
announces nothing, which is exactly why conditioning it costs the surface nothing at all. *HR sees
`ssn` and nobody else does* keeps M5's answer: two deployments.

This also **retires a prediction** made in `build_tools`: "the day an attested principal arrives per
call, this is one of the two places that stops being true — and the tool set becomes something
assembled per caller rather than per process." It is instead the day that was closed off, and the
docstring now says so where it said the other thing. That is the third such correction this milestone
and the fourth in two slices.

**2. The refusal for an unattestable surface lives in `select(None)`, and `build_resolver`'s
invariant needed narrowing rather than reopening.** The obvious spelling — a `surface=` argument on
the pairing function — would have reopened *`loom query` refuses exactly what `loom serve` refuses*,
and would have got the case wrong anyway: `McpConfig.attests` is true for an attesting config that
`loom query` still cannot attest anybody with. What refuses instead is one step lower and names no
surface: a read needs a **decided** policy set, and asking for one while naming nobody is what fails.
So `bind_reads`/`bind_writes` are the pairing, surface-blind, holding every static refusal; and
`build_resolver` = `bind_reads(...).for_(None)`, `build_runtime` = `bind_writes(...).for_(None)`.
`loom query`, `loom run` and a stdio `loom serve` reach it at build, before anything is read; an
HTTP server with `mcp.auth` never reaches it, because it selects per call. The invariant is corrected
where it is written: it is a claim about **pairings**, and what differs between the two commands is
an *ability*, not a check.

**3. A missing claim fails closed, and the rule that reconciles it with decision 2.** An attested
caller whose token lacks a claim a guard names leaves the guard **undecided**, and an undecided guard
**applies** the policy — the direction that subtracts more, and the same direction `admits` fails in
for a row. That is the opposite of decision 2's *refuse*, and both are right under one rule:
**decidable at pairing time with somebody to tell → refuse; decidable only per call, with only the
caller to tell → withhold silently.** An operator is present at bind and reads stderr; per call the
only party in the exchange is the caller, and "a policy did or did not apply to you" is §6.1's
existence oracle. Two consequences worth stating: absence is **not** `null` (if it were,
`principal.dept != null` would be *false* for a caller with no `dept`, and a missing claim would have
*widened* what they see), and a claim whose value contradicts its declared type is treated as absent
rather than compared.

**4. Claims are declared, in `loom.yaml`.** This is the first time the expression language would have
referenced something no declaration describes, and the answer is to declare it rather than to make an
exception: `mcp.auth.claims` names each claim and its type (`string`, `string[]`, `boolean`), beside
the issuer that mints them and in the same file as the policy that reads them. The ontology still
references only what the ontology declares — `principal.` is **refused in a spec** — so the language
keeps one rule for all three reference forms: *a reference is legal where its declaration is in
scope.* Without it a typo'd claim would be caught by nothing and would fail closed *and* silent,
which is the mask-typo failure inverted. `sub` and `iss` are built in (the verifier requires them)
and cannot be redeclared.

**5. `contains`, and the subset rule restated rather than bent.** Group membership needs `contains`
over a list claim, and `predicate.py` refuses operators on the rule that *a predicate is lowerable
only when Loom, not the engine, decides what every operator means*. That rule is about expressions
answered **twice**. A `when:` guard is answered **once**, in process, over a list only Loom holds —
no engine sees it — so `contains` is legal in a guard and refused in `rows:`, where it would need an
IR node and a second evaluator to agree with. A scalar claim inside `rows:` needs neither: it folds
to a `Const` the lowerable subset already carries, so the slice adds **no new SQL shape**.

Two things found by building it:

- **A missing claim inside `rows:` has to become an undecided *leaf*, not a deny-all policy.**
  Substituting `null` is wrong in the dangerous direction — `==` is null-safe here, so
  `object.ownerId == principal.sub` would come back *true* for every row whose owner is null. A
  `DENY_ALL` sentinel would have to be understood at both enforcement sites, which is the one thing
  the milestone promised not to touch, and it over-subtracts under `||`. What the fold emits instead
  is `null < null`: SQL answers `NULL`, §5 refuses to order a null, both planes call it undecided by
  rules they already had, and Kleene propagation does the rest. The differential test covers it.
- **The announcement set needed a name and a refusal.** The tool set and the banner are built from
  masks, which no caller changes — but they were being built from a `Resolver`, and a resolver
  holding policies nobody selected would fail *open* by one conditional policy. So
  `PolicyProgram.announcements()` is `decided=False`, and `Resolver._table` refuses to read with it.
  Every read goes through that method, which makes the check total rather than a habit.

`RESERVED_KEYS` and `Reserved` are **deleted** with the last of their entries, as this milestone said
they would be. The partition test they anchored is replaced by the stronger statement it stood in
for: every key `POLICY_KEYS` accepts is read into a field of `Policy`. `MOVED_KEYS`/`audit` is
unaffected, and `check_keys` still refuses a key nobody declared.

---

[← M5](./m05-governance.md) · [M7 →](./m07-typed-filters.md) · [backlog](./backlog.md)
