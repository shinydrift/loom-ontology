[← Spec index](../spec-v0.md)

# Open edges (v0 → v1)

Named deliberately so they're conscious deferrals, not gaps:

- ~~**`governance.policies` row predicates**~~ — **answered** by §6.1; this bullet outlived the
  slice that closed it. Both of the things it said had to be settled with the feature rather than
  after it were: the predicate is lowered into the query on the read path (so it filters before
  `LIMIT`/`OFFSET`, and `hasMore` does not lie) and evaluated in process over one row on the write
  path, and the two-valued/three-valued disagreement was resolved by a **third** answer rather than
  by making either plane imitate the other — true, false, or undecided, admitted only on true, with
  `==`/`!=` carried into SQL as `IS NOT DISTINCT FROM` so §5's "null is a value" survives intact.
- **Composite primary keys** — v0 assumes a single-property PK. Multi-column PKs touch `key`
  expressions and `objectRef` encoding.
- **Complex types** — `array`/`struct`/`map` (see §1).
- **Computed / derived properties** — properties backed by an expression rather than a column.
- **Multi-object actions** — the explicit post-v1 feature the §4 boundary reserves room for.
- **Row-level conflict detection** — §4.1's check is the *table's* snapshot, because Iceberg's
  commit protocol can assert a ref and nothing finer, so a run conflicts with unrelated writes to the
  same table. Narrowing it needs a row-level precondition the format does not have; comparing rows in
  the runtime instead would reopen the race the check exists to close, so it is not the answer.
- ~~**Governance and the carry-across**~~ — **answered** by §6.1. A masked column is *carried*,
  exactly as an unmapped one is: the alternative destroys the data the policy exists to protect. It
  is withheld from the account of the write (`before`/`after`, and therefore §9.2's record), never
  from the write. What made that safe rather than merely convenient is the fourth refusal in §6.1 —
  an action that writes a masked property is refused where the spec and the deployment are paired —
  so §9.2's *what the record does not name, the run did not change* stays true word for word.
- **Edit-log erasure** — retitled, because "retention" named the wrong operation. Two of the three
  questions here are now **answered**. Masking: the log masks under the same policies as a read,
  because `before`/`after` are built by the same projection, and it costs the record nothing (see
  the carry-across edge above). Expiry-by-deletion: **refused, permanently.** It would make an
  expired record and a lost one the same sight to a reader holding a stamped snapshot with no
  matching row, which is the one property §9.2's write-then-log ordering exists to buy — so no port
  verb removes a row from `_loom_meta.edits`, and there is no `retain:` key in §6.1's grammar and
  none coming. A config key that is only a default for a command nobody runs is also the shape this
  codebase has been bitten by once already (`loom.managed`, written by `apply` and read by nothing
  for two milestones), and nothing in Loom runs on a schedule, so there is no actor for a window to
  belong to.

  What is genuinely left is **erasure**, which does not require deletion: declared properties are
  somebody's data and this table outlives the row it describes, so a `delete` action erases a
  customer and leaves the ontology's account of them behind. The shape that keeps §9.2's invariant
  is a **redaction in place** — keep the row and empty `parameters`/`before`/`after`/`object_key` —
  so a stamp still finds a row and what it finds says an edit happened, by whom, to what, and
  nothing about the person. That is a rewrite rather than an append, so it belongs to a command with
  a port of its own; the action runtime never gains the verb, which is what keeps "an action can
  reach `_loom_meta.edits` and nothing else" true. Nothing is deferred about the record's shape: the
  columns are fixed (§9.2) because the table is only ever created.
- ~~**A per-caller identity over MCP**~~ — **answered**, in two slices. `mcp.auth` gave a caller a
  source: `run_<action>` still records `mcp.actor`, which names a *deployment*, and now records an
  attested `principal` **beside** it over the one transport that can carry one. `when:` then gave a
  policy something to condition on it, and §6.1 carries the grammar. What this entry did not
  anticipate is the shape of the answer: **half a policy may name a caller.** `rows:` may be
  conditioned and `mask:` may not, because a mask announces itself into the tool description and the
  `filter` schema, and a per-caller announcement makes the tool set a function of the caller rather
  than of the spec (§7). That refusal also **retired a prediction made in the registry**: the day an
  attested principal arrived was supposed to be the day the tool set became something assembled per
  caller, and it is instead the day that was closed off.

  What was learned in narrowing it is that there are **three** kinds of answer here and only one is
  worth having, which the earlier wording did not distinguish. An actor may be *declared* by an
  operator (`mcp.actor` — true about a deployment, silent about a caller); *inferred* by Loom
  (`default_actor()`'s OS user — rejected, because it looks like a principal and is not one); or
  **attested** by a transport that checked it — not declared and not inferred, and the only one of
  the three worth more than `unknown`. MCP's authorization is an OAuth 2.1 resource-server profile,
  so attested means a bearer token validated on issuer, audience, expiry and signature against an
  authorization server. Anything short of that — reading a header, trusting a claim — is the
  client-supplied actor rejected above, wearing a hat.

  What was missing was therefore specific — the validation, and the config to describe an
  authorization server — and that is exactly what shipped: `mcp.auth` names an issuer, an audience
  and a key set, and `auth.TokenVerifier` checks `iss`, `aud`, `exp`/`nbf` and the signature against
  it. Two things this entry got right are worth marking as *held*: nothing else had to move
  (`ActionRuntime.run` already took the argument per call), and **the last sentence has now come
  true rather than been revised** — a public bind may write, and only once `mcp.auth` is declared.
  That refusal narrowed rather than moved: the bind still decides whether the question is asked;
  attestation is the first answer to it other than no.

  Two things this entry did **not** anticipate, both found by building it:

  - **A resource server does not have to be an authorization server, and the middle between them
    does not exist.** "Validate tokens yourself" and "refuse to be an auth server" read as opposed
    options and are the same decision: Loom verifies a signature against a public key and issues,
    stores and mints nothing. The cheaper-looking alternative — a proxy in front that validates and
    injects a header — requires Loom to distinguish *this header came from the proxy* from *this
    header came from a client*, which on any bind it can have it cannot. So there is no
    trusted-proxy mode and none is coming, and `ALGORITHMS` is asymmetric-only, because a symmetric
    algorithm verifies with the key that signs and would make Loom able to mint what it checks.
  - **`aud` is the check that carries this, and it is the one that looks skippable.** Without it a
    token minted for any other service by the same issuer is accepted: right issuer, right
    signature, unexpired, and never addressed here.

  Governance (§6.1) turned out **not** to be the other half of this, and the correction is worth
  keeping because it was written the other way round here first. The prediction was that policies
  filter by principal, so the identity would have to reach the resolver. What actually landed
  filters by *deployment*, for a reason that is structural rather than a matter of ordering:
  `loom query` and `loom run` have no transport at all, so an identity can never be attested to
  them, and building governance on one would have left the direct half of §7's second invariant
  ungovernable. So the resolver still receives no identity, deliberately and now permanently for
  everything §6.1 can express — and `when:` is reserved for exactly the policies that this edge
  unblocks, refused until then rather than approximated against `mcp.actor`.

  **The prediction about what a principal would cost was wrong in the same direction, and larger.**
  This entry said that because a principal varies per call, the two one-per-process things would have
  to move with it: `build_server`'s single `Resolver` and `ActionRuntime`, and `DuckDBEngine`'s one
  connection registering every scan under the global aliases `t0`/`t1`/`m0`. Attestation landed and
  **neither moved.** Two forces were being treated as one. What would force per-caller objects is a
  policy that varies *by* caller; what forces the alias fix is two calls *in flight at once*. A
  per-call principal is neither — it is a value threaded through objects that stay shared, and the
  handlers are still synchronous, so nothing overlaps. The alias problem belongs to whatever
  milestone makes a handler `async`, which is a different milestone with a different reason.

  **A principal never reaches the resolver**, and that held exactly as written. A conditional policy
  is resolved into a decided policy set *above* it, because a principal is constant for the duration
  of a call, so everything it conditions — including a predicate that names the caller — folds to a
  literal before the call begins. §6.1's "the resolver receives no identity" is true by construction
  rather than by scope, and every enforcement site in `Resolver` and `_Run` was left untouched. The
  cost landed where this entry said it would: `loom query`, `loom run` and a stdio server can never
  attest anybody, so a config whose policies name a caller is **refused** for them rather than
  filtered differently.

  One thing that prediction got slightly wrong is worth marking, because it shaped where the refusal
  lives. It reads as a check about surfaces, and a check about surfaces would have had to be spelled
  as an argument to the function that pairs a spec with a deployment — reopening *`loom query`
  refuses exactly what `loom serve` refuses*, and getting the case wrong anyway, since an attesting
  *config* read by `loom query` still attests nobody. What refuses is one step lower and names no
  surface: **a read needs a decided policy set, and asking for one while naming nobody is what
  fails.** The pairing stayed surface-blind, and the invariant needed narrowing rather than
  reopening — it is a claim about pairings, and what differs between the two commands is an ability,
  not a check.

  What that leaves open here is nothing. The remaining identity-shaped question — a claim declared
  nowhere a spec can see — was answered by declaring claims in `loom.yaml` rather than by letting the
  expression language reference something undeclared: `mcp.auth.claims`, beside the issuer that mints
  them, in the same file as the policy that reads them.
- ~~**Refusing to act when the log is unavailable**~~ — **answered** by §6.1's `edit_log`, and the
  prediction written here was wrong in two places worth correcting rather than quietly replacing.
  It said the clause was a *policy* and belonged with the other policies: it is a switch on a whole
  deployment, names no objectType, and sits beside `policies:` rather than in it, for the same
  reason `mcp.writes` does. And it said the bound was "convert a predictable unloggability into a
  refusal that changes nothing", which was right about the limit and wrong about the shape — *all*
  of it is spent before any row is written, because a per-write probe turned out to be nearly blind
  (the log shares a catalog with the row, so an unreachable catalog already fails the write itself).
  What was right is the part that survived unchanged: a clause reading "every applied run is logged"
  would promise something Iceberg's lack of a cross-table transaction does not allow, so `edit_log`
  promises about a deployment instead, and an append that fails after the commit still reports
  `log_failed`.
- **Chained renames** — `renamedFrom` is one hop (§2.1). Widening it to a list of prior names
  would be backward-compatible with every spec written against v0, if a lake that routinely skips
  applies ever makes it worth the cost.

---

[← §9 `_loom_meta`](./09-loom-meta.md)
