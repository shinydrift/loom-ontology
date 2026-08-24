# Quickstart — the whole stack against a real Iceberg table

Install Loom, then run every layer of it against a local Iceberg warehouse: a spec, a migration, a
query through DuckDB, a write, and an MCP server. Nothing here needs a service running.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,iceberg,duckdb,mcp]"

pytest                              # 1091 tests
loom validate tests/fixtures/valid  # → ok — 2 object type(s), 1 link type(s), 3 action(s)
```

Then run the whole stack against a real Iceberg table. `examples/retail` ships the worked example
plus a seed script that builds a local Iceberg warehouse — SQLite metastore, filesystem storage,
no services to start:

```bash
python examples/retail/seed.py                        # loom apply, then loom sequence, then two
                                                      # columns added by something that isn't Loom
loom validate --physical examples/retail/ontology     # check the spec against live metadata
loom query Customer examples/retail/ontology --key c1 # → one row, through DuckDB
loom query Customer examples/retail/ontology --key c2 --link orders   # → a link traversal
loom query DailySalesPerformance examples/retail/ontology --key 2026-02-11 # → precomputed daily KPIs
loom run upgradeTier examples/retail/ontology \
  --param customer=c3 --param newTier=gold            # → one row rewritten, one row recorded
loom serve examples/retail/ontology                   # → 10 MCP tools over stdio
```

The retail example also demonstrates a small production-shaped analytics workflow. `seed.py`
materializes `sales.daily_sales_performance` from the current `sales.orders` Iceberg snapshot and
Loom exposes it as the ordinary typed `DailySalesPerformance` object (including
`get_daily_sales_performance`, `search_daily_sales_performance`, and
`list_daily_sales_performance`). Revenue, order count, and unique-customer count are therefore
computed once during refresh, not on every agent request. Every materialized row carries
`refreshedAt`, `sourceTable`, and `sourceSnapshotId`, so a retrieved answer says exactly when and
from which source snapshot it was derived.

**It lands through the `daily-sales` entry**, in both places that produce it — `seed.py`'s
`materialize` and the dashboard's `POST /api/refresh`. `write_daily_sales_performance(catalog, path)`
computes the rollup and stops at a Parquet file; the declared entry does the rest. Loom does not
compute this, and the file is where that boundary sits —

```bash
loom ingest daily-sales daily.parquet examples/retail/ontology   # → checked, one commit, recorded
```

`refresh_daily_sales_performance` in the same file is the hand-rolled comparison — a schema kept in
lockstep by hand, a `txn.overwrite`, and a write nothing in the lake records. Nothing ships calling
it any more, and it is kept because the comparison is *checkable*: an acceptance test runs both
against one orders snapshot and asserts the same table comes out. Same rows. What the declared load
adds is the two things the overwrite has no way to produce — every value checked against the
ontology's declared types before it lands, and a row in `_loom_meta.loads` saying which file became
which commit.

Note the identity rule cutting both ways here. The seed drops under `data/` are checked in, so their
bytes are fixed and re-loading one is refused as the same load twice. The aggregate stamps a fresh
`refreshedAt` on every recompute, so its bytes differ and a second refresh is a second load. Same
`derive_load_id`, opposite answers, both correct.

That run also created `_loom_meta.edits` and appended to it — no `loom apply` in this lake's history
at all, because the log is created by whatever run needs it first rather than by a migration:

```
$ loom run upgradeTier examples/retail/ontology --param customer=c3 --param newTier=gold --yes
...
note: recorded in _loom_meta.edits as cb24ed913c28437a8e658b1e1ea1d7bd.
applied · Customer 'c3'
```

The record holds the actor, the action, the key, the status, the attempt count, the snapshot the
write asserted, the bound parameters, and before/after **as the ontology sees them** — never the
physical row, which would make the log an unabridged copy of the data that outlives the row it
describes. Refused runs are in there too: a log of successes cannot say who *tried*. And the row
write stamps `loom.edit_id` into its own Iceberg snapshot summary, so a record and the commit it
describes can always be tied back together.

Point any MCP client at that last command and the ontology shows up as typed tools:

```
$ loom serve examples/retail/ontology
loom serve — 4 object type(s), 3 link type(s), 4 action(s) → 14 tool(s) over stdio
  get_customer  get_daily_sales_performance  get_order  get_support_ticket
  list_customer  list_daily_sales_performance  list_order  list_support_ticket
  match_support_ticket  search_customer  search_daily_sales_performance  search_order
  search_support_ticket  traverse
  read-only · mcp.writes is false, so 4 declared action(s) are not exposed
    (`loom run` still reaches them — the runtime is not what is switched off, the surface is)
  semantic search · SupportTicket.body via local/BAAI/bge-small-en-v1.5
    (match_ ranks by brute force · the arithmetic is linear in the filtered set, so a narrow filter is the lever)
    (no vector index · the whole sidecar is read on every call, filtered or not — that is the I/O floor,
     and it grows with the embedded rows rather than with the answer)
    (a row with no vector is absent from match_, silently — `loom embed` is what reports how many,
     and how far behind)
```

`match_support_ticket` is there because one property in the spec says `semantic: body` and this
deployment configures a provider — both halves, which is why the three lines under it are
disclosures rather than settings. Comment `mcp.embedding` out and the tool is simply absent; the
server still starts, exactly as `writes: false` serves without actions.

Three actions and no `run_` tools, because **serving writes is a choice a deployment makes**, not
one a spec makes. `loom serve` used to be incapable of changing anything and people pointed it at
real lakes on that basis; letting an upgrade plus an unrelated spec edit quietly make one mutable is
not a default worth having. Add two lines under `mcp:` in `loom.yaml` and the same command serves
four more:

```
$ loom serve examples/retail/ontology     # mcp: { writes: true, actor: agent:support-bot }
loom serve — 4 object type(s), 3 link type(s), 4 action(s) → 18 tool(s) over stdio
  get_customer  get_daily_sales_performance  get_order  get_support_ticket
  list_customer  list_daily_sales_performance  list_order  list_support_ticket
  match_support_ticket  run_delete_ticket  run_forget_customer  run_record_order
  run_upgrade_tier  search_customer  search_daily_sales_performance  search_order
  search_support_ticket  traverse
  writes enabled · 4 action(s) exposed, every run recorded as actor 'agent:support-bot'
  semantic search · SupportTicket.body via local/BAAI/bge-small-en-v1.5
    ...
```

The banner counts what is actually exposed rather than what the spec declares, and says which mode
it is in either way — "how many tools" does not answer "can this change my lake".

Change one more line and the same tools are served over a socket instead of a pipe:

```
$ loom serve examples/retail/ontology     # mcp: { transport: http }
loom serve — 4 object type(s), 3 link type(s), 4 action(s) → 14 tool(s) over http
  get_customer  get_daily_sales_performance  get_order  get_support_ticket
  list_customer  list_daily_sales_performance  list_order  list_support_ticket
  match_support_ticket  search_customer  search_daily_sales_performance  search_order
  search_support_ticket  traverse
  read-only · mcp.writes is false, so 4 declared action(s) are not exposed
    (`loom run` still reaches them — the runtime is not what is switched off, the surface is)
  semantic search · SupportTicket.body via local/BAAI/bge-small-en-v1.5
    ...
  listening on http://127.0.0.1:8000/mcp · cleartext HTTP, no TLS — terminate it in front
  one call at a time · tool calls are serialized, so a slow query blocks the server rather than queueing beside another
```

The same fourteen tools, because **a transport is not an input to the surface** — a spec compiles to
one tool set and both transports are handed it. What differs is what a *process* is. `host`, `port`
and `path` live in `loom.yaml` rather than on the command line, for the reason `writes` does: a flag
lets one invocation contradict the file an operator reviews, and "who can reach this" is exactly the
question that file should answer. It binds to `127.0.0.1` unless told otherwise.

Two lines of that banner are the honest disclosures. It answers one call at a time, which is a
scaling claim and is therefore stated rather than left to be discovered — the DuckDB connection and
the resolver under it are built once for the process, and every scan registers under the same three
global aliases, so two concurrent reads would not merely contend. (That was written as "the same
change governance needs", and governance turned out not to need it: a policy names no caller, so one
resolver is the right count for one deployment. It belongs to the milestone that attests a principal
per call.) And it speaks cleartext: TLS belongs to whatever sits in front, which is part of why the
default bind is local.

The other half of that posture is a refusal. `mcp.writes: true` will not start on a non-loopback
bind:

```
$ loom serve examples/retail/ontology     # mcp: { transport: http, host: 0.0.0.0, writes: true }
1 problem in ontology spec:
  - loom.yaml: 'mcp.writes' is true on a non-loopback bind ('0.0.0.0') — refusing to serve a write
    surface to whoever can reach the port
    hint: bind 127.0.0.1 and put authentication in front, or set 'writes: false'. `mcp.actor` names
    a deployment, not a caller, so every write here would be recorded under one name nobody checked
```

Writes over a socket are not the same decision as writes over a pipe, and the difference is
reachability rather than transport. `mcp.actor` always named a deployment — it lives in `loom.yaml`,
so three stdio clients reading one file already record one string. What a public bind changes is who
is permitted to *be* one of those callers: over stdio, whoever can run the binary; over `0.0.0.0`,
whoever can reach the port. A per-caller identity needs a transport that actually checked one, which
means validating a bearer token rather than reading a header — until then, the bind is the bound.

```jsonc
// traverse({"objectType": "Customer", "key": "c2", "link": "orders", "limit": 2})
{
  // `one_to_many` and not the `many_to_one` the link declares: `orders` is `placedBy` followed
  // from its `to` end, and the cardinality reported is the direction's rather than the link's.
  "targetObjectType": "Order", "cardinality": "one_to_many",
  "count": 2, "limit": 2, "offset": 0, "hasMore": true,
  "objects": [
    { "orderId": "o3", "customerId": "c2", "total": "89.95", "placedAt": "2026-02-14T12:00:00+00:00" },
    { "orderId": "o4", "customerId": "c2", "total": "2100.00", "placedAt": "2026-03-02T12:00:00+00:00" }
  ]
}
```

```jsonc
// run_upgrade_tier({"parameters": {"customer": "c3", "newTier": "gold"}})
{
  "action": "upgradeTier", "objectType": "Customer", "operation": "modify",
  "status": "applied", "key": "c3",
  "before": { "customerId": "c3", "name": "Alan Turing", "tier": "bronze", "ltv": null },
  "after":  { "customerId": "c3", "name": "Alan Turing", "tier": "gold",   "ltv": null },
  "concurrency": "enforced — the write asserts the snapshot the read saw",
  "attempts": 1, "editId": "5f2c…", "failures": []
}
```

One tool per action rather than one `run(action, params)`, and the reason is the schema rather than
the name: `upgradeTier` takes an objectRef and a two-value enum, `recordOrder` takes a string, an
objectRef and a `decimal(12,2)`. A single generic tool would have to type `params` as a free-form
object — the one place in the whole surface where an agent gets an untyped bag. The declared
parameters sit under `parameters` so that Loom's own arguments (`dryRun`, and `limit`/`offset` on
the read side) can never collide with a name a spec chose. Pass `dryRun: true` and the run stops
before the write and reports what it would have done — the same thing [`loom run`](./actions.md)
prints above its `y/N`, for a caller that has no prompt.

Swapping the local warehouse for a production lake is a `loom.yaml` edit — `type: iceberg-rest`
with a URI — not a spec or code change.

Five properties of that generated surface are worth naming, because they're enforced rather than
documented:

- **No raw SQL reaches the agent.** The resolver only emits plan nodes it built itself, so there is
  no code path from a tool call to arbitrary SQL. Asserted in `tests/test_mcp_registry.py`.
- **Every read is bounded and ordered.** There is no way to ask for an unbounded scan, and paging
  is stable because plans always carry an `ORDER BY` on the primary key.
- **Declared types are honored on the way in and out.** A key arriving as `"42"` for a `long`
  property is coerced before it becomes a predicate, and `decimal` values never pass through a
  float.
- **A write cannot alter a schema, and recording a write cannot reach a table.** Four ports, three
  planes: reads, a table's shape, a table's rows, and Loom's own record. The action runtime holds
  the last two, and neither has a verb for DDL — the edit-log port takes no table name at all.
  Asserted against a fake catalog that implements exactly those ports and no others, which is also
  what proves the serving-process version of it: **a server can change the rows the spec's actions
  declare and no schema at all.** Point an MCP client at a lake and it cannot migrate one.
- **A refused write is a result, not a broken call.** The protocol's error flag answers *did this
  call become a run*, never *did the run succeed* — so a failed validation rule, a conflict and a
  write failure all arrive as content an agent branches on (`status`, then `failures[].code`, then
  `retryable`). It is the only encoding that can describe a write that committed and then failed to
  log itself, which a boolean gets backwards.

---

Next: [drafting a spec from a file](./drafting-a-spec.md) — the one command that goes the other way.
