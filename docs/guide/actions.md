# Running an action

An action is the only thing in Loom that changes a row. `loom run` is the write path's `loom query`
— it takes an action apiName and named parameters, which is exactly the shape the generated
`run_<action>` tool takes, and calls the same runtime. If the dev command could do something the
tools can't, the ontology would have a back door:

```
$ loom run upgradeTier examples/retail/ontology --param customer=c3 --param newTier=gold
Loom run — upgradeTier on examples/retail/ontology

  modify Customer "c3"
      ~ tier  "bronze" -> "gold"

  previewed at snapshot 3071900788344075695 — nothing is held:
  the run reads again and asserts that read, so a row that moves while you
  decide is a conflict you are told about, never a silent overwrite.

Run these changes? [y/N] y
```
```jsonc
{
  "action": "upgradeTier", "objectType": "Customer", "operation": "modify",
  "status": "applied", "key": "c3",
  "before": { "customerId": "c3", "name": "Alan Turing", "tier": "bronze", "ltv": null },
  "after":  { "customerId": "c3", "name": "Alan Turing", "tier": "gold",   "ltv": null },
  "readSnapshotId": 3071900788344075695,
  "concurrency": "enforced — the write asserts the snapshot the read saw",
  "attempts": 1,
  "failures": []
}
```

Five rules shape it.

**A modify carries across the columns nobody declared.** A row-level modify is an equality-delete
plus an append committed as one transaction, which means it rewrites the *whole* row — so every
column no property maps has to be carried or it is silently nulled. Those are the same columns
`loom plan` reports as unmanaged: someone else's data. `crm.customers` in the example has two of
them, and the second has a type Loom has no name for at all:

```
# before                                       # after — one column moved, nothing else
id  tier    region  segments                    id  tier  region  segments
c3  bronze  apac    null                        c3  gold  apac    null
c1  gold    emea    [enterprise, early-adopter] c1  gold  emea    [enterprise, early-adopter]
```

`segments` is an `array<string>`, and `array<T>` is deferred in the spec's type system — the
runtime never builds a type for it, never looks at the value, and hands it straight back, because
the conversion is driven by the table's own schema rather than by anything the ontology knows.

**A refusal changes nothing, and comes back typed.** Binding, the read, the uniqueness check and
every validation rule run before the single write call. A failed rule carries the spec author's own
message, verbatim, under a code from a closed set — not an opaque string an agent has to parse:

```
$ loom run upgradeTier examples/retail/ontology --param customer=c3 --param newTier=gold
  ! validation_failed: New tier must differ from the current tier
  nothing was written.
```

Every rule is evaluated, not just up to the first failure — the same bargain `loom validate` makes
with a spec author, because an agent fixing one precondition per call is as miserable as a human
fixing one typo per run.

**Rows and schemas are different ports.** `loom apply` holds a `CatalogWriter` and has no verb for
deleting a row; the action runtime holds a `RowWriter` and has no verb for altering a schema.
Neither extends the other, so neither can do the other's job by accident — the same reasoning as
"no raw-SQL tool is ever exposed", applied twice more.

**`operation: delete` is not in tension with "Loom never drops."** Never-drop is about *inference*:
Loom refusing to read a destruction into the **silence** of a spec, which is why a column no
property mentions is left alone rather than dropped. A declared `delete` action is the opposite of
silence — someone wrote the word and named the key. The scopes differ too: never-drop governs
schema, and Loom still never drops a column or a table, in any command.

**The gap between the read and the write is closed, and "closed" is meant literally.** The snapshot
the read saw is carried into the write and asserted *inside the commit* — an Iceberg
`assert-ref-snapshot-id` requirement the catalog validates against live metadata as the table's
metadata pointer swaps. Not a re-read and a comparison in the runtime: that leaves a window between
deciding and committing, which narrows the race rather than closing it, and would have meant writing
"narrowing" here.

What is asserted is the **table's** snapshot, because Iceberg's commit protocol can assert a ref and
nothing finer. So a run conflicts with any concurrent commit, including one to a row it never touched
— coarse, and chosen: the only narrower test is comparing the row, and a row comparison can't be
carried into a commit. Two things follow. A competing write to a column the ontology never mapped
*is* a conflict, not because Loom looked at it but because a modify writes it back from a stale read
— Loom won't inspect that column and won't overwrite it blind. And the runtime absorbs the false
conflicts itself, retrying up to three times, re-reading and re-evaluating every rule against the row
actually about to be written over. `attempts` is on the result, because "applied" after three
internal re-reads is a different fact from "applied":

```jsonc
{ "status": "refused", "attempts": 3, "failures": [{
    "code": "conflict",
    "message": "Customer 'c3' could not be written: the table moved between the read and the write, after 3 attempts — tier changed under it",
    "detail": { "table": "crm.customers", "expectedSnapshotId": 3071900788344075695,
                "foundSnapshotId": 8442119003518827741, "attempts": 3,
                "changed": ["tier"], "contended": true },
    "retryable": true }] }
```

`contended` is the field that matters: an agent told only "conflict, retry" will hammer a table that
is merely busy and give up just as readily when its intent has genuinely been overtaken. And where a
competing write really does invalidate the action, the retry doesn't paper over it — the run comes
back `validation_failed` or `object_not_found`, the real reason.

Note what the prompt above does *not* say. It doesn't hold the row while you decide: the run does its
own read and asserts that one, so what you approve is the shape of the change. That's the only answer
that can also be true of `run_<action>`, which has no prompt at all.

Which is why the MCP tool's `dryRun` is an **inspection verb and not an approval step**. It produces
the same shape the block above prints — bind, read, validate, stop — and reserves exactly nothing for
the call after it: no state is carried, no row is held, and a real run reads again and asserts *that*
read. Without it, `previewed` would be a status no MCP caller could ever see and an agent's only way
to learn what an action does would be to do it. The approving, where there is any, happens where the
human is: in whatever the MCP client puts in front of its user before it lets a tool call through.

Over MCP the actor comes from `mcp.actor` in `loom.yaml` — declared by an operator, never inferred by
Loom, which is the difference that keeps `$LOOM_ACTOR`-or-OS-user off this path: that would name
whoever started `loom serve` while looking like a principal. Unset, a served run records `unknown`,
and the edit log then answers what was done, to which row, when, with which parameters and whether it
refused — everything except *who*. Neither transport has a *who* to answer with: HTTP is a socket,
not an authentication, and `mcp.actor` names a deployment either way. The gap closes with a transport
that actually checked an identity — a validated bearer token, not a header read and believed, which
would be a caller filling in its own name on its own audit record. `ActionRuntime.run` already takes
the argument per call for it, and until then the bind is what bounds the claim: a write surface is
refused on anything but a loopback address.

---

Next: [loading data in bulk](./loading-data.md) — a file's worth of rows rather than one.
