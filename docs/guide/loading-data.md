# Loading data in bulk — `ingest` and `sequence`

An action writes one row. These two commands write a file's worth, and several files' worth in a
declared order.

## Loading a batch

`loom ingest` writes a file's worth of rows, through an entry declared in
`loom.yaml` — an object type, a mode, a format, and nothing on the command line but which file:

```
$ loom ingest daily-sales daily.parquet examples/retail/ontology
Loom ingest — daily-sales on examples/retail/ontology

  - replace 31 row(s) into DailySalesPerformance (sales.daily_sales_performance)
      from  daily.parquet
      load  3f2a…9c1

  ! replace empties this table first — every row not in the batch is gone,
    including rows this ontology does not describe.

  previewed at snapshot 6119…207 — nothing is held:
  the load reads again and asserts that read, so a table that moves while you
  decide is a conflict you are told about, never a silent overwrite.

Load these changes? [y/N] y
```

Five things shape it, and the first is what the rest are for:

- **The lake records it.** One row in `_loom_meta.loads` — which entry, which file and its
  fingerprint, how many rows landed, how many were rejected, and the status — plus `loom.load_id`
  stamped into the write's own Iceberg commit, which is the only attribution atomic with the write.
  Until this existed, `governance.edit_log: required` could answer *what did this actor do today* for
  every single-row agent write and have nothing to say about the overwrite that moved the numbers.
- **The mode is the whole of what a load does to the rows already there.** `append` adds; `merge`
  replaces the ones it names by primary key, carrying every column the ontology does not map so it
  never nulls somebody else's data; `replace` makes the table exactly the batch. Only the two that
  read assert a snapshot — an append puts no row over another, which is what lets two pipelines load
  one table without refusing each other.
- **It never migrates.** A batch that does not fit is refused naming the column, and the fix is
  `loom plan` / `loom apply`. The port a load holds has no DDL verb at all. A column the *spec has
  no name for* — `array<T>`, `struct`, `map` — has no such fix, and that is the one place the
  on-ramp does not close on itself: [`loom infer`](./drafting-a-spec.md) drafts a type and an
  `ingest:` entry from a file, and if that file holds one of those, the drafted entry cannot load
  the file it was drafted from. Loom will not narrow the batch for you — a column no property
  claims is refused rather than dropped, precisely so a load can never quietly discard somebody's
  data — so the route is to load a file without the column and let whatever writes it keep filling
  it. The refusal says so.
- **A refusal is whole, and re-running is a refusal too.** One bad value refuses the batch, because a
  partial load leaves the lake in a state nobody declared; `--reject-to` quarantines the rows that
  failed their own checks and loads the rest. And a load's id is derived from the entry, the mode and
  the file's bytes, so a pipeline that times out and retries is told it already ran — `--load-id` is
  how you say *this file again, on purpose*.
- **It is not on the tool surface, and cannot be.** The entry lives in `loom.yaml` rather than in the
  spec, and the MCP surface is assembled from the spec — so a verb that writes an arbitrary batch is
  not something an agent can reach, structurally rather than by a rule someone remembers. It is off
  by default too: `governance.ingest` defaults to `refused`.

Loom does not connect to Kafka, crawl an object store, or open a JDBC connection. A pipeline hands it
a file; Loom decides whether that file may become rows.

## Loading several, in order

A warehouse that needs three tables filled needs three loads, and something has to say which order
they go in. `sequences:` names an order over the entries already declared; `loom sequence` runs it
from a manifest — the file that varies per run, which for a sequence is the one that names the
others:

```yaml
# loom.yaml
sequences:
  - { name: nightly, loads: [customers, orders, daily-sales] }
```
```yaml
# drop/manifest.yaml — paths resolve against this file, not the cwd
customers: customers.parquet
orders:    orders.parquet
daily-sales: daily.parquet
```
```
$ loom sequence nightly drop/manifest.yaml examples/retail/ontology
Loom sequence — nightly on examples/retail/ontology

  + customers: append 4 row(s) into Customer (crm.customers)
  + orders: append 6 row(s) into Order (sales.orders)
  - daily-sales: replace 31 row(s) into DailySalesPerformance (sales.daily_sales_performance)

  Iceberg's unit is the table, so there is no cross-table transaction to be had:
  this sequences the loads, stops at the first refusal, and reports exactly which
  ones landed rather than pretending the run was atomic.
```

That last paragraph is the whole design, and it is `loom apply`'s own sentence one level up — apply
met this first for tables and answered it the same way. **A sequence is an order, not an atom.** When
one stops, the loads before it are landed and stay landed; the result names them and names where it
stopped, because there is nothing else honest to do.

Three consequences worth knowing:

- **The order is the list, not the order of `ingest:`.** Declaration order could have been given
  meaning for free and deliberately was not — an entry moved during review would silently change
  what runs when.
- **A manifest that supplies some of the entries is refused before anything opens.** Loading two of
  three tables and reporting success is the failure this exists to prevent; a partial run is what
  `loom ingest` per entry already is.
- **It records the run, in a third table.** `_loom_meta.sequences` — which loads were one run, in
  what order, and where it stopped. A `sequence_id` column beside `load_id` would have been cheaper
  and is the one thing `_loom_meta.loads` forbids: that table is only ever *created*, so a column
  added today could never reach a log that already exists.

It checks no referential integrity, and says so: ordering customers before orders makes the *result*
coherent, but Loom has no cross-table constraint and this does not add one.

## Where governance meets a load

In one place, and it is worth knowing which.

**A deployment whose `governance.policies` do not fit the ontology refuses to load**, in the same
words `loom query`, `loom run`, `loom serve` and `loom embed` refuse to start — a mask naming a
property an action writes, a `rows:` predicate naming a claim nobody declared. The pairing is
checked before any entry runs, so bulk writes cannot be the one plane still moving under a
configuration the rest of the deployment will not stand on.

**The policies themselves are not applied to a batch.** A load has no caller: nobody is being shown
a column, and nothing is being withheld from anybody. So an entry writes every column it maps,
including one a `mask:` withholds from every read — `hide-ltv` blanks `ltv` in every tool, every
`loom query` and every action's `before`/`after`, and a `customers` load still fills it. The
asymmetry with an action, which refuses the whole deployment for writing a masked property, is the
difference between the two planes rather than a rule with a hole in it: an action is reachable by an
agent through a `run_` tool, and there is deliberately no `ingest_` tool for one to reach.

---

Next: [an app on top of it](./dashboard.md) — the same ontology with a UI in front.
