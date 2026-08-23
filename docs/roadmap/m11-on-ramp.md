[← Roadmap index](../ROADMAP.md)

# ✅ Done — M11: The on-ramp — a spec drafted from a file, and loads that run in order

M9 made a batch become rows. What it did not touch is everything *before* the first load: somebody
still writes the spec by hand against a file they are looking at, and a warehouse that needs three
tables filled needs three commands with nothing to say which order they go in. This milestone is
that on-ramp. It ran beside M10's then-open fourth slice rather than after it — the two share no
code, which is why the two milestones closed out of order.

Four slices, in order:

1. `loom infer` — draft an objectType from a file
2. `sequences:` — an ordered set of declared loads, run as one command
3. Restage `crm.customers` in the example so its unmanaged columns arrive the way unmanaged columns
   really do
4. Move the example's materialization onto its own declared load

## First slice — `loom infer`, and the direction of the arrow

Loom's whole posture is that the spec is the authority and the lake is checked against it. A command
that reads a schema and writes a spec points the arrow the other way, and M9 had already refused
something that looks like this: `BulkWriter` has no DDL verb, "the never-drop rule pointed at a new
plane, **refusing to infer a schema change from the shape of somebody's file**."

**That refusal is about a load inferring a migration, and this is neither.** `loom infer` opens no
catalog, holds no port, takes no ontology path, and writes no file. It reads one file's declared
schema and prints text. The argument is not that a scaffold is harmless — it is that this one is not
on the plane the refusal governs, and the *absence of a catalog argument* is what makes that
structural rather than a promise in a docstring.

- **The draft does not validate, on purpose.** `primaryKey` and `backing` come out as placeholders
  no property matches, so `loom validate` fails on them by name. This is the whole safety story, and
  it is asserted in both directions: a test loads a rendered draft and expects the failure, and a
  second test fills in what a person would and expects a working `ObjectType`. A scaffold that
  emitted something immediately servable would be a scaffold that gets committed unread, and the
  first person to discover what it guessed would be whoever queried it.

- **Parquet only, and the other two are refused by name.** Parquet *declares* its types, so reading
  one is reading. A CSV declares nothing — every type would be sniffed from a sample, and
  decimal-versus-double on a money column is the sniff that loses fractions of a cent without
  anybody noticing, which is the exact hazard `examples/retail/seed.py` already comments on. NDJSON
  is JSON: no decimal, no date. Both are listed in `--format`'s choices *so that the refusal can
  name them* rather than reading as an unimplemented flag.

- **Nullability is read, never observed.** It comes from the file's schema and not from whether this
  file happens to hold a null. Worth stating because most writers declare everything nullable, so a
  draft is usually more permissive than the domain — which is the direction to err, and still a
  thing to go through by hand. Reading rows at all was refused for the same reason: it is how a
  generator starts inferring enum values from a sample.

- **What it will not guess.** `enum` values (a file shows the values it happens to hold, not the
  domain's set — the retail example's `closed` tier is in its enum for a reason no sample would
  reveal), `unique`, links, `searchable`, `semantic:`. Every one of them is a claim about meaning,
  and this reads storage.

- **The unmapped column is the interesting output, not the mapped one.** A column whose type the
  spec has no name for is rendered where its property would have been, with the reason, followed
  once by what actually happens to it: §2 rule 7 says an unmanaged column is reported by `plan`,
  never dropped, and carried across untouched by every write. So "no type for this" is drafted as a
  working outcome rather than a gap — and the half a reader would otherwise learn from a refused
  load is stated too, since a source column no property claims *is* refused at load time.

- **A property name is a reading of the column; the column is kept verbatim.** `full_name` becomes
  `fullName` with `column: full_name` on the same line, so the guess sits next to the thing it was
  guessed from and is undone by deleting one word. Two columns that read as one property are refused
  naming both, rather than resolved by order.

- **It drafts the `ingest:` entry too, and addresses it to the other file.** An objectType is a fact
  about the ontology and a load is a fact about a deployment, so they are printed as two blocks with
  the second commented for `loom.yaml`. `mode` comes out as a placeholder for the reason `config.py`
  already gives: the three modes differ in what they *destroy*, so a default would make the safest
  reading of an under-specified config the one nobody wrote down. The `columns:` map is emitted only
  for the properties whose name differs from their source column — and a test asserts the drafted
  block parses as a real entry, rather than looking like one.

## Second slice — `sequences:`, and the sentence `apply` already had to write

Three tables filled needs three loads and something to say which order. What it must not need is a
new claim about atomicity, and the answer was already in the repo: Iceberg's unit is the table, so
`apply` "sequences tables, stops at the first failure, and reports exactly which ones landed rather
than pretending the run was atomic". A sequence of loads inherits that verbatim, because what makes
it true is one commit per table, which is the same thing one level up. The CLI prints that paragraph
above the prompt rather than keeping it in a docstring.

- **An explicit list, not the order of `ingest:`.** `ingest:` is already a YAML list, so declaration
  order exists and could have been given meaning for nothing. Refused: an entry moved during review
  would silently change what runs when, and "these three, in this order" is a different statement
  from "these are the loads this deployment declares". One deployment can hold several sequences over
  overlapping entries, and an entry in none of them is ordinary.

- **`loads:` names entries and the manifest names files.** `loom ingest` takes one data file on the
  command line because that is what varies per run; a sequence needs several, so it takes one file
  that names them, and the principle survives rather than bends. Manifest paths resolve against the
  **manifest's own directory** — a manifest describes a drop and a drop is a directory of files
  beside it, so resolving against the cwd would make one manifest mean different things depending on
  where somebody stood.

- **Both manifest mismatches are refused, and they are different mistakes.** An entry the sequence
  runs and the manifest lacks would load two of three tables and report success — the failure this
  whole slice exists against. An entry the manifest names and the sequence does not is a file
  somebody expects to land that nothing will read.

- **`sequences:` is resolved at config load, unlike everything else in that module.** `objectType`
  and a policy's subject wait for a pairing because they name things in an ontology `config.py` has
  never seen. A sequence names entries in the file being parsed, so deferring the check would make
  the one error a reader can fix without leaving the file arrive from somewhere else.

- **An eighth port, and the third time one argument has been made.** `LOAD_LOG_TABLE` exists because
  `edits`' columns are forever; **a sequence is now in exactly that position with respect to
  `loads`**. The cheap move — a `sequence_id` column beside `load_id` — is the one thing
  `LOAD_COLUMNS` already forbids in writing: that table is only ever *created*, so a column added
  today can never reach a log that already exists, and every deployment that has run `loom ingest`
  once has one. The split turns out to be right on its own terms too: a load's record answers *what
  did this file do to this table*, and a run's answers *which loads were one run, in what order, and
  where did it stop* — three properties of the run and of no load in it.

- **`loom ingest` cannot reach the sequence table.** Which is why `sequence_log_writer_for` is a
  separate exchange point rather than a second verb pair on the load log's port: a single load has no
  run to record, and a port it holds should not be able to say that several of them were one.

- **A refused *preview* does not run for real, unlike `loom ingest`'s.** That command runs a refusal
  so the log records who tried; here the individual loads already do exactly that, each recording its
  own refusal in `_loom_meta.loads`. What running anyway would add is a sequence row for an order
  that was never attempted — precisely the intention-shaped record that writing after the fact
  exists to avoid.

- **The preview gate is the run's own `dry_run`, not its status.** Found by a test rather than by
  argument: a preview that stops halfway reports `partial`, because it is describing what *would*
  happen, so gating the record on `status != previewed` put a row in the log for a run nobody
  performed. `_Load` draws the same line in the same place, by branching on `self.dry_run` before it
  decides anything.

- **`partial` is a status only a sequence can have**, and it has no counterpart in `IngestResult`
  because a single load is one commit: it lands whole or not at all, so there is no half of it to
  name.

- **It buys no referential integrity, and says so.** Nothing in `ingest/` mentions links. Ordering
  customers before orders makes the *result* coherent; it does not make Loom check that every
  order's customer arrived. Reading a schedule as a guarantee is the misreading available here, so
  the module docstring closes it explicitly.

## Third slice — the example stops pretending its unmanaged columns were always there

`seed.py` built `crm.customers` in one `pa.schema(...)`: four columns the ontology declares and two
it does not, born together. Every other part of the repo then leaned on those two to demonstrate §2
rule 7 — *a column no property maps is somebody else's data, reported by `plan`, never dropped,
carried across untouched by every write*. **A column born in the same breath as the managed ones
cannot demonstrate that.** What the rule is about is a column that arrives from a writer that is not
Loom, and the example never showed one arriving.

So the script is three stages now, and the order is the demonstration:

1. `bootstrap` — `loom apply`, from nothing but the spec.
2. `load` — `loom sequence seed`, running the declared `customers` and `orders` entries from
   `data/manifest.yaml`.
3. `arrive` — pyiceberg directly, adding `region` and `segments` and filling them.

- **Stages 1 and 2 were the gap this milestone actually found.** Nothing in the shipped example ever
  ran a declared load: `daily-sales` existed in `loom.yaml` and was exercised only by tests and by an
  operator typing the CLI by hand. The example now loads itself through Loom, which is the thing M9
  claimed and the example did not do.

- **An all-Loom bootstrap structurally cannot produce an unmanaged column**, and that is why stage 3
  is outside the framework rather than merely conventional: `apply` creates the columns the spec
  declares, and `_check_columns` refuses a source column no property claims. Both directions are
  closed, so the only way to get one is a writer that is not Loom — which is what the word means.

- **`region` is unmanaged by choice and `segments` could not be otherwise.** `region` is a plain
  string and nothing stops it being a property, which is the more interesting half: the framework is
  not the obstacle. `segments` is `list<string>`, and §1 defers `array<T>`, so it is the case where
  there is no choice. Keeping both makes the example say which is which.

- **Format is declared per entry, and the example now uses two.** `customers`/`orders` are ndjson
  because a drop of hand-written seed rows should be readable in a diff; `daily-sales` stays parquet
  because a computed aggregate should carry its types rather than have them re-parsed. One file, two
  formats, neither guessed.

- **`daily-sales` is deliberately not in the `seed` sequence.** It is computed *from* the orders that
  sequence lands, so it cannot be part of the same run as its own input. Which is a useful thing for
  the example to show about what a sequence is: an order over loads, not a dependency graph over
  data.

- **A test fixture had to split, and the split is the finding.** Four suites used a `seeded` fixture
  whose premise was *a lake Loom is a guest in is exactly the one where the record matters* — and a
  seeded warehouse now arrives with all three `_loom_meta` tables in it. So `conftest.guest` is new:
  the same two tables built by pyiceberg alone. The old mechanism became test scaffolding, which is
  the right place for it, because as scaffolding it no longer says anything about what Loom
  recommends.

- **Re-running the seed is refused, and the example is better for it.** The data files are checked
  in, so their bytes are fixed, so `derive_load_id` gives the same id every time — the second run of
  `seed(fresh=False)` is one load happening twice and is told so by name. A test asserts it.

## Fourth slice — the materialization lands through the entry that was written for it

`daily-sales` had been declared in `examples/retail/loom.yaml` since M9 and **nothing shipped ever
ran it**. Both places that produce the aggregate — `seed.py` and the dashboard's `/api/refresh` —
called `refresh_daily_sales_performance`, which is the hand-rolled `txn.overwrite` the entry exists
to replace. So the declared load was exercised only by tests and by an operator typing the CLI by
hand, which is a strange thing for a milestone's headline feature to be.

- **The dashboard route was making two claims and only one of them was true.** Its docstring said
  *not a Loom tool*, correctly — `DailySalesPerformance` is an ingestion-time aggregate, and a
  tool-shaped button would be the dashboard telling a more comfortable story than the lake supports.
  But it was also, silently, *not through Loom at all*. Those were running together. Now the
  computation stops at a Parquet file and the declared entry lands it, so the route is a
  demonstration rather than an apology — and the response carries the load id, which is something a
  hand-rolled overwrite could not have said.

- **The dashboard declares that entry and only that entry.** `customers` and `orders` are how
  `seed.py` fills an empty warehouse; a running dashboard has no business holding a way to append to
  either, and an entry a deployment does not declare is one nothing in it can name. `governance.
  ingest: allowed` is set there too, because the posture is per deployment and this is a second one.

- **`refresh_daily_sales_performance` is kept, and its job changed rather than ended.** It was the
  shipped path and is now the comparison — what every lake already does and the record cannot see.
  Deleting it would make *the declared load adds a contract and a record rather than different rows*
  a claim the README asserts and nothing verifies; the acceptance test that runs both halves against
  one orders snapshot is what makes it checkable.

- **The identity rule cuts both ways, and the example now shows both.** The seed drops under `data/`
  are checked in, so their bytes are fixed and re-loading one is refused as the same load twice. The
  aggregate stamps a fresh `refreshedAt` on every recompute, so its bytes differ and a second refresh
  is a second load. Same `derive_load_id`, opposite answers, both correct — and a test for each,
  because a button that worked once and reported a duplicate forever would be the same rule
  misapplied.

- **The Parquet file is a handover, not an artifact**, so both callers write it to a temporary
  directory rather than beside the checked-in drops. The distinction is worth drawing in the example:
  `customers.ndjson` is data somebody wrote and can read in a diff; `daily.parquet` is the output of
  a computation that will run again tomorrow with different numbers in it.

## What M11 leaves owing

- **No referential check on a load whose object type has links.** A sequence orders loads; it does
  not verify that every order's customer arrived. Loom has no cross-table constraint, and adding one
  is a spec question rather than an ingest question.
- **`loom infer` reads parquet only.** CSV and NDJSON are refused by name with the reason. Adding
  either means deciding what a sniffed type is allowed to claim, which is a decision worth its own
  slice rather than a flag.
- **Nothing infers a *link*.** `loom infer` drafts one object type from one file; two files with a
  shared key are a `linkType` nobody drafts, and the shape of that guess is unexplored.

---

[← M10](./m10-semantic-search.md) · [backlog](./backlog.md)
