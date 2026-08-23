[← Roadmap index](../ROADMAP.md)

# ✅ Done — M4: MCP write surface + transport hardening

*Goal: the action runtime shows up as tools; serve over more than stdio.*

The read tools, the registry, and stdio `loom serve` landed in M1; M2 and M3 are done, so what was
left here was a transport and the surface over a runtime that already exists. Two seams M3 left
pointing at this milestone: `ActionResult` is the shape `run_<action>` serializes rather than
composes, and `ActionRuntime.run` takes the `actor` this transport has to supply — an unauthenticated
one recording `unknown` is correct, and a served tool that never fills it in makes the edit log
useless without failing anything, which is worth a test here rather than a hope.

Both have landed. The surface came first (`run_<action>`), then the second transport — which turned
out to be about neither the tools nor the runtime, both unchanged, but about everything that stops
being true when a process stops belonging to the client that spawned it. The third slice is the one
box neither of them touched: whether the engine underneath can serve the surface at all.

- [x] Per action: `run_<action>` with JSON Schema from parameters, description from the spec.
- [x] Capability negotiation — validate what a spec demands against `engine.capabilities()`, at the
      point where the two are wired rather than at serve. *`negotiate.py`: three requirements, one
      of which is not a spec feature; a refusal rather than a narrowing; and the line around
      `native_merge` drawn where it belongs.*
- [x] HTTP transport alongside stdio. *`mcp.transport: http`, with an address in `loom.yaml` and a
      write surface bounded by the bind. The tool set, the registry and the runtime are unchanged;
      what changed is everything that stops being true when a process stops belonging to one caller.*
- [x] Structured tool errors — surface validation-rule failures and write conflicts as typed
      results an agent can act on, not opaque strings. *Both seams paid out as predicted: the tool
      serializes `ActionResult` rather than composing anything, and the actor was already an
      argument.*

The header was held by capability negotiation alone, and the box was kept out of the transport
slice on purpose: it has no transport content at all — the answer is identical over stdio and HTTP
— so bundling it would have meant a change about a port deciding whether an existing spec still
serves. It also carried an unresolved question of its own that deserved answering rather than
smuggling: `Capabilities` carries `joins`, `offset`, `case_insensitive_like` and `native_merge`,
nothing validated a spec against any of them, and `native_merge` is a *write-path* field sitting on
what looked like the read path's port. The third slice below is that question and its answer.

**Seven decisions taken in the first slice** (`run_<action>` — the surface over the runtime):

- **One tool per action, and the rule that says so is narrower than `traverse`'s was.** M1 justified
  a single generic `traverse` with "the link name is data, and enumerating object-type × link would
  grow the surface for no gain" — which is true of an action name too, so as written it decides this
  case wrongly. The rule it was reaching for is about the schema, not the name: *a generic tool is
  right exactly when the varying element does not change the input schema.* Every link takes the same
  `(objectType, key, link, page)`; every action takes something different. One `run(action, params)`
  has to type `params` as a free-form object, the only place in the generated surface where an agent
  gets an untyped bag and "declared types are honored on the way in" stops being structural. The
  sentence is rewritten where it is stated rather than qualified from a distance. The cost is real
  and stated: forty actions generate forty tools, and the answer to that is exposing fewer, never
  typing them less.

- **Two argument namespaces, which never mix — and `search_` was already built this way.** Names from
  the spec's vocabulary go inside a nested object (`filter`'s property filters, `parameters`'
  declared parameters); names Loom chose stay at the top (`key`, `limit`, `offset`, `objectType`,
  `link`, `dryRun`). Stating the rule rather than repeating the shape is what makes `dryRun` addable
  at all: an ontology may declare a parameter called `dryRun` and it can no more be shadowed than a
  property called `limit` can. The alternative — flat parameters with a reserved word — makes a spec
  that validates and cannot be served, which is the worst seam available.

- **An agent can preview, and a preview approves nothing.** `dryRun` runs bind → read → validate and
  stops, returning `previewed` — otherwise that status is one no MCP caller could observe and an
  agent's only way to learn what an action does is to do it. It is reconciled with §4.1 rather than
  bolted beside it: the prompt was put outside the concurrency window *because* `run_<action>` has no
  prompt, so a preview that reserved anything for a later call would be the design that decision
  rejected. Nothing is carried between a preview and a run; the run reads again and asserts that
  read. Approval of an agent's tool call belongs to the client's own UI, where the human is. A
  separate `preview_<action>` was the alternative and doubles a surface this slice just argued
  against doubling — and the two tools would have carried identical schemas, which is precisely when
  the first decision says one parameterized tool is right.

- **`isError` answers "did this call become a run?", never "did the run succeed?"** M1 already sent a
  `ResolverError` back as content because that is the form an agent can recover from; `ActionError`
  joins it. But a run that *reached* the runtime is never an error here whatever it returned. A
  refusal is the expected outcome of a precondition doing its job; the outcome is four-way and one
  code is retryable, neither of which a boolean carries; and **`applied` with a `log_failed` beside
  it is a real shape the boolean gets backwards** — `isError` would say the write did not happen when
  it did. So an agent branches on `status`, then `failures[].code`, then `retryable`, and the
  generated description says so because the input schema cannot.

- **What a serving process holds, said precisely enough to still be worth something.** M3 wrote "no
  serving process holds a row-writable handle between calls", which was true of a command that exits
  and is nearly vacuous for a long-lived one: the process holds `Catalog`s, and a real catalog
  implements every port, so it is one function call from being a row writer whatever the runtime
  does. The surviving narrow version is that nothing holds a row-writable *typed* reference, so
  `row_writer_for()` stays the one place the plane is named at a call site — and it is no longer the
  load-bearing claim. What replaces it is testable the way M3's port claims are: the runtime holds a
  `RowWriter` and an `EditLogWriter` and never a `CatalogWriter`, so **a serving process can change
  the rows the spec's actions declare and no schema at all.** The fake proves it, because a real
  catalog implements every port and can never show which one was used. The sentence is corrected in
  all three places it appears.

- **The actor is declared, never inferred — `mcp.actor`, and `unknown` when it is unset.** M3 kept
  `default_actor()` off this path because it falls back to the OS user, so it would name whoever
  started `loom serve` while looking like a principal. An operator writing `actor: agent:support-bot`
  is not doing that: it is a true statement about a deployment, and over stdio it is exactly true,
  because one client spawns one process and a session has one principal. Declared-versus-inferred is
  the distinction, not process-versus-caller. A client-supplied actor was the third option and is
  worse than `unknown` — an audit record whose subject fills in its own name is self-attestation, and
  MCP has no identity to attest with. What the edit log is worth over stdio is therefore written down
  rather than discovered: it answers what was done, to which row, when, with which parameters and
  whether it refused, and it does not answer *who*. That is a gap in the transport, and it closes per
  call when an authenticated one lands.

- **`status` is read at last: labelled, not hidden.** A non-`active` objectType, link or action still
  becomes a tool, with `DEPRECATED — ` / `EXPERIMENTAL — ` in front of the spec's own description.
  Hiding a deprecated action would leave `loom run` able to run something the tool surface denies —
  the exact back door `loom run` exists to not be — and hiding it *honestly* would mean making the
  runtime refuse it, turning a surface label into a kill switch and making `deprecated` mean broken.
  A label is also the form that works on this caller: an agent reads descriptions afresh every
  session and has no memory of a deprecation notice, so the notice has to be in what it reads.

  **And serving writes is a choice, off by default** (`mcp.writes`, §6). `loom serve` was provably
  read-only and deployments were pointed at real lakes on that basis; defaulting it on would let an
  upgrade plus an unrelated spec edit silently make a production lake mutable. It is a config key
  rather than a CLI flag, because a flag lets an invocation contradict the file an operator reviews.
  It belongs to *this* slice rather than the capability-negotiation box below: that box is about what
  the engine can do, and this is about what the deployment permits. It is deliberately not a
  governance policy either — it names no principal and filters no row — though M5 may subsume it.
  *(M5's first slice answered that: it does not. Policies subtract and never add, so a policy can
  only ever deny further than this switch already does — see below.)*

  One thing this slice could not test the way it wanted to: a **conflict produced by a real race over
  the wire**. It needs a competing commit inside the window between the served read and the served
  write. The reason given here was "nothing a client can schedule over the protocol reaches inside a
  spawned process" — *and the transport slice below found that reason wrong, so it is corrected
  here rather than left standing.* The MCP SDK dispatches tool calls concurrently; HTTP
  demonstrably can carry an interleave, and stdio was never what prevented one. What prevents one is
  that Loom's own dispatch is synchronous top to bottom, so a served process answers one call at a
  time whatever the transport — and a commit from *outside* the process would still have to land
  inside a millisecond window, three attempts running, which nothing outside can schedule without
  the hook M3 declined to add (a hook nothing in production calls is a hook that drifts). The
  conclusion is unchanged and the argument for it is now the true one. The conflict's wire form
  stays asserted against `LoomMCPServer.call`, which is the exact function both adapters call and
  whose `(text, is_error)` pair is what goes on the wire.

**Six decisions taken in the second slice** (the HTTP transport — the same tool set, reachable
by anyone who can reach the port):

- **A served process answers one tool call at a time, and it is proved rather than assumed.** This
  was measured before it was decided: the MCP SDK dispatches `on_call_tool` concurrently, and two
  clients on one HTTP server genuinely interleave. So the serialization is entirely Loom's, and it
  comes from one rung down — dispatch is a plain function, every `ToolSpec.handler` is a plain
  function, and a synchronous callable cannot be interleaved. That premise is asserted structurally
  in `test_mcp_registry.py`, so making any handler `async` fails a test instead of quietly changing
  what the process guarantees.

  It stays serialized because the fix is not a transport's to make, and three pieces of shared state
  say why. `DuckDBEngine` holds **one** connection and registers every scan under `t0` / `t1` /
  `m0` — constants in `resolver.py`, so the *same three names* for every object type in every
  ontology; two overlapping reads would not merely contend, the loser would answer with the winner's
  rows. `build_server` builds one `Resolver` and one `ActionRuntime` for the process. And making
  those per-caller is the same change M5 needs to filter by principal — an argument for doing it
  once, there, rather than half of it here. The cost is real and is *said*, in the banner and the
  README, rather than discovered: a slow query blocks the server instead of queueing beside another
  call. An HTTP server that answers one request at a time is a scaling claim, and one that does it
  silently is a support ticket.

  A lock was the obvious alternative and is worse: over synchronous handlers it can never be
  contended, so it is code with no behaviour whose only effect would be to keep the guarantee alive
  the day somebody makes a handler `async` — turning a correctness question into an unexplained
  performance one. The assertion fails loudly instead.

- **`mcp.actor`'s justification was already weaker than it read, and gets corrected rather than
  extended.** The first slice defended it with "over stdio it is exactly true, because one client
  spawns one process and a session has one principal". But this key lives in `loom.yaml`, which
  configures a *deployment*: three stdio clients reading one file already record one string for
  three callers. One name for many callers is not what a socket introduces, and declared-versus-
  inferred — the part that was load-bearing — survives untouched. What a socket changes is
  **reachability**: who is permitted to *be* one of those callers.

- **So the limit is drawn on the bind, not the transport — and `writes: true` on a non-loopback bind
  refuses to start.** Over stdio the caller set is "whoever can run the binary and read the config";
  over loopback HTTP it is very nearly the same set; over `0.0.0.0` it is not remotely the same set,
  and there `actor:` names a deployment nobody bounded. A refusal rather than a warning, because
  `cmd_serve` already refuses to start rather than advertise tools that will fail, and because
  nobody reads the third line of a banner on a server that came up. It is honest about its own
  limit: it constrains what Loom *binds*, not what *reaches* it, and a proxy in front of a loopback
  bind is outside anything the config can see.

  The third way out — an identity **attested** by a transport that checked it, which is neither
  declared nor inferred and the only one of the three worth more than `unknown` — is named and not
  built. MCP's authorization is an OAuth 2.1 resource-server profile, so attesting means validating
  a bearer token on issuer, audience, expiry and signature; reading a header instead is the
  client-supplied actor the first slice rejected by name, wearing a hat. That is a milestone, not a
  slice, so spec-v0's open edge is **rewritten** rather than closed: it now names the three
  categories, says what is missing (the validation, and config for an authorization server), and
  records that a loopback server may write today because its callers are the set stdio's were.

- **The address is all config, including the port, and defaults to loopback.** The first slice's
  argument — a flag lets one invocation contradict the file an operator reviews — is weakest for a
  port number, which is not a posture. It goes in config anyway: a file describing half an address
  does not describe the server. The host is the strongest case rather than the weakest, and
  `127.0.0.1` is the default for the reason `_confirmed()` refuses without a terminal — don't put
  somebody's lake on a network because nobody said to. There is no TLS key; termination belongs in
  front, which is a second reason the default bind is local. `allowed_hosts` backs DNS-rebinding
  protection and is required exactly where it cannot be derived: a loopback bind knows its three
  names, a public one does not know the name the world reaches it by.

- **The stderr rule stays and its reason is replaced.** "stdout is the transport" is false the
  moment a transport has an address instead of a pipe. The banner stays on stderr because it is
  *diagnostics*, and one output shape is worth more than one that is right for two transports and
  open again for the third — whatever collects those lines should not need to know how the tools are
  being served. uvicorn's access log, the one thing that would have written to stdout, is off.

- **An HTTP status never disagrees with `isError`.** A transport with real status codes invites
  re-litigating a decision the first slice took, and it does not get to: the status answers *did
  this exchange happen* and `isError` answers *did this call become a run* — different layers, never
  two votes on one thing. Every tool outcome is a `200` carrying content; a non-`200` is only ever a
  rejected `Host`, a rejected `Origin`, a malformed body or an unknown session. Mapping a refusal
  onto a 4xx would make an agent's transport raise before its own branch on `status` ever ran.
  Asserted with a raw HTTP client, because an SDK client hides the number.

  Two things this slice deliberately did **not** move. The principal stops exactly where it did —
  `mcp.actor` reaches the edit log and nothing else, and the resolver is handed no identity, because
  inventing a per-call principal with no source and no reader is the mistake `expect_snapshot_id`
  was kept out of `RowWriter` to avoid. And the surface does not branch on transport: both adapters
  are handed one assembled server from `build_mcp_server`, which is asserted with no socket in
  sight, and `test_no_tool_can_take_a_query`'s walk is re-run over the schemas as received across
  the wire.

**Five decisions taken in the third slice** (capability negotiation — the one box the milestone was
waiting on, and the question it was carrying):

- **Three requirements, and one of them is not a spec feature — so the box's own wording is
  corrected rather than satisfied.** It read "validate spec features vs. `engine.capabilities()`",
  and two of the three are exactly that: `joins` is demanded by declaring a link, because a traverse
  joins two backing tables; `case_insensitive_like` by declaring a *string* property searchable,
  because `Resolver._filters` emits a `Contains` for that condition and an `Eq` for everything else
  — so a searchable **enum** demands nothing, and `Customer.tier` is the case that shows the rule is
  about the property's type rather than the `searchable:` list. `offset` is not a spec feature at
  all: every generated `search_` / `list_` / `traverse` tool carries the page arguments for every
  ontology there is, because they are Loom's own vocabulary and not the spec's (§7's two argument
  namespaces, seen from the third side). It is a constant requirement of the **surface**. It is
  checked anyway, because the question a deployment is asking is not "does my spec use features this
  engine has" but "can this engine serve the tools I am about to advertise" — and the answer has to
  cover the parts of that surface no spec chose. Strip every link and every searchable property from
  an ontology and `offset` is still required; there is a test that does exactly that.

- **A refusal, never a narrowing — and the third degradation is the one that decides it.** Loom
  already refuses rather than degrades in three places argued separately (`cmd_serve` would rather
  not start than advertise tools that fail on every call; `loom apply` refuses a breaking plan whole
  with no `--force`; `mcp.writes: true` refuses a non-loopback bind), and what makes them agree is
  visible from here. The narrowings available are dropping `traverse`, stripping `offset` out of the
  page schema, and compiling `Contains` down to `Eq`. The first two make the generated surface a
  function of the **engine**, which spends the one claim — the surface is a function of the spec and
  nothing else — that the transport slice just proved survives a second transport, and spends it on
  a config mismatch. The third is worse than both and worse than failing: an exact match where the
  spec promised substring **returns rows**, so nothing errors, nothing retries, and the agent
  believes an answer that is wrong. A capability mismatch is also the worse *shape* of failure to
  leave running — an engine without `OFFSET` serves page 1 of everything and fails page 2, so it
  works until it doesn't, and by then a client is holding the tool list.

- **`native_merge` is a routing hint, and the line gets drawn around negotiation rather than around
  the port.** A **requirement** is something a spec can demand and an engine can fail. Nothing can
  demand `native_merge`: writes go through the catalog's `RowWriter`, which every catalog
  implements, so an engine that cannot `MERGE` is a slower way to serve an ontology and never a
  reason to refuse one — it selects an implementation, not a possibility. Which means the complaint
  ("a write-path field on the read path's port") had the wrong premise: `Capabilities` was never the
  read path's structure. It describes an **engine**, and this is where an engine is asked what it
  is; that the engine only reads today does not make the question a read-path question. What was
  actually wrong was `Engine`'s docstring, which called `capabilities()` "what the serve-time
  negotiation reads" as though that were all of it, and it is fixed where it was written. The
  distinction is then made checkable rather than conventional: `NEGOTIATED | NOT_NEGOTIATED` must
  cover the dataclass exactly, under a test, so a fourth flag fails until somebody says which kind
  of fact it is. Without that, the quiet answer available is "a third kind: unread" — which is how
  `loom.managed` got written by `apply` and read by nothing for two milestones.

- **It happens where a spec and an engine are wired, not at serve.** "At serve" is where a mismatch
  is *observed*, not where it belongs. `build_resolver` is the one function that pairs the two, so
  checking there means `loom query` refuses exactly what `loom serve` refuses; checking in
  `cmd_serve` would leave a dev command reading successfully out of an engine the served surface
  will not stand on, which is the shape of back door `loom query` was deliberately built not to be.
  It is the same principle M5 states for governance — enforce below MCP so a direct call and an
  agent call get the same answer — arriving one milestone early because this is the first check that
  had a choice about which rung to sit on. It is deliberately *not* an invariant of `Resolver`,
  which stays constructible from any engine: the pairing is what has to be checked, not the pair,
  and that is what lets a test drive the resolver with a fake and an adapter be exercised before
  anybody has decided what it will serve.

- **The write path is not negotiated, and that is not an omission.** `ActionRuntime` reads a whole
  row and writes it back through the catalog's ports; it never compiles a plan, so it asks the
  engine for nothing and there is nothing to check. An engine that fails negotiation still runs
  actions — `loom run` is unaffected. `loom serve` refuses anyway, because it builds both halves and
  one of them cannot stand, which is the honest answer for a surface that is advertised as a set.

---

[← M3](./m03-action-runtime.md) · [M5 →](./m05-governance.md) · [backlog](./backlog.md)
