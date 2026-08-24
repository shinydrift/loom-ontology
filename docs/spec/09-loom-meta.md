[← Spec index](../spec-v0.md)

# 9. `_loom_meta` — what Loom recorded

Part of the contract because these are tables in *your* lake, not implementation details: anything
with an Iceberg client can read them, and a later Loom must keep them readable.

The namespace holds three tables, one per catalog, and none is ever named by a spec:

| table | records | created by |
|---|---|---|
| `_loom_meta.applied` | what `apply` did to **schemas** | the first `loom apply` touching that catalog |
| `_loom_meta.edits` (§9.2) | what an action did to **one row** | the first action run against that catalog |
| `_loom_meta.loads` (§9.3) | what an ingest did to **many** | the first load run against that catalog |

None is a planner input and none can be planned *against*: `plan` only ever visits the tables the
spec declares, so it proposes nothing for any of these and reports none as unmanaged.

**Three tables rather than one log with a `kind` column**, and the reason is `applied`'s schema
rather than tidiness: each of these is only ever *created*, never altered, so a column left out today
can never reach a table that already exists. Merging planes would mean every row carrying two-thirds
of a schema that does not apply to it, and every reader learning to tell record kinds apart by which
columns are null. What varies is the plane being recorded, and a plane gets a table.

`_loom_meta.applied`, created by the first `loom apply` that touches that catalog:

```
_loom_meta.applied
  version       long         required   # global to the spec, not to the catalog — see below
  applied_at    timestamptz  required
  content_hash  string                  # sha256 of the spec source, canonicalized
  spec          string                  # {relative path: file text} as JSON — what a rollback restores
  summary       string                  # JSON: the tables this run created/altered in this catalog
  status        string                  # applied | partial
  loom_version  string
  actor         string                  # $LOOM_ACTOR, else the OS user
```

`summary` is a JSON list, one entry per table this catalog holds:

```json
[{"table": "local.crm.customers", "action": "alter",
  "columns": ["ltv_usd: renamed from lifetime_value"],
  "renames": {"ltv_usd": "lifetime_value"}}]
```

`columns` is the plan's own prose, for a person reading the history. `renames` is present only when
the run renamed something, and says the same thing as data — because `rollback` has to *invert* it
(§9.1), and a rollback that parsed a display string would be one typo away from renaming the wrong
column. A **rollback** records the same list wrapped in an object naming the version it restored:

```json
{"rollback_of": 4, "tables": [ … ]}
```

**Append-only.** The current state is the row with the highest `version`; everything before it is
history. Nothing rewrites a row.

**It is not the planner's input.** The diff is always taken against the live catalog, so a table
someone changed out of band shows up honestly instead of being masked by a state file that says
otherwise. This table answers a narrower question — *has this exact spec already been applied
here, and what did that apply do?*

**`version` counts applies of the spec, not of a catalog.** A spec spanning two catalogs writes a
row to each, both carrying the same version and the same `content_hash`, and each summarizing only
its own tables. There is no central place to hold that counter, so it is derived: one past the
highest version any bound catalog holds. A catalog added to a project at version 7 starts its
history at 7.

**`content_hash` covers the spec's YAML only** — not `loom.yaml`, which is deployment config, so
the same spec hashes identically against staging and production. An edit that changes no column
still records a new version: the stored `spec` is what a rollback restores, so it has to track the
file text, not just the physical shape.

Each managed table additionally carries three Iceberg table properties, set in the same
transaction as its schema change: `loom.managed`, `loom.spec_hash`, `loom.applied_version`. They
duplicate what this table records on purpose — a table should be self-describing without the
reader knowing `_loom_meta` exists.

## 9.1 `rollback` — what this table is *for*

`loom rollback --to 4` restores the spec recorded at version 4 and re-plans it against the live
catalog. It is deliberately the ordinary loop over an older spec: no new change kind, no new write
op, the same classification and the same whole-plan refusal.

**It reverses DDL, and only DDL.** `apply` never wrote a row, so `rollback` never deletes one. It
touches no snapshot and expires nothing. Rows written since the version being restored are nobody's
to throw away.

**A version selects a spec, not a per-catalog target.** A version whose text differs from the one
before it makes every bound catalog stale, so every one records a row for it — which means a
catalog with *no* row at version 4 is a catalog whose text did not change at 4, and is therefore
already at that spec. There is one thing to restore and every catalog is re-planned against it.
Catalogs holding a row at that version must agree on its `content_hash`; if they don't, one was
written outside Loom and there is no single spec to restore, so rollback refuses.

**What comes back, and what doesn't.** Of the four ops the write port has, exactly one reverses
within the port:

| applied after version 4 | rolling back to 4 |
|---|---|
| `rename` | reversed — an Iceberg rename back, same field id, no file rewritten |
| `add` | left live; the restored spec no longer maps it, so it is **unmanaged** from here on |
| a created table | left in place, for the same reason one level up |
| `promote` | refused — the reverse is a narrowing, which is breaking |
| `relax` | refused — the reverse is a tightening, which is breaking |

The last two are not a hole in rollback. Once a column is a `long`, the spec that says `int` no
longer describes this lake, and the way out is forward rather than back. The middle two are the
never-drop rule holding: a rolled-back add is a live column nothing maps, and `rollback` names it
in its report rather than leaving it to be discovered.

**Renames need this table, because `renamedFrom` points forward.** The spec at version 4 says
`column: ltv_usd` and carries no key — a spec written before a rename cannot name the column that
rename has to be undone from. So `rollback` reads `summary.renames` for every version after 4,
composes the chain (`a→b` at 5 and `b→c` at 6 means the column called `a` at 4 is called `c` now),
inverts it, and plans an ordinary rename. Nothing is written back into the YAML: the restored files
are byte-identical to what was recorded.

**A rollback is an append, not an unwind.** It writes a new row at the next version carrying the
restored spec's text and hash. Its `status` is `applied` — after a rollback the lake genuinely *is*
at that spec, so anything else would make the next run's "has this spec already been applied here?"
check believe something false, and re-record a spec that is already live.

**The spec files are the last thing it writes**, and only if the run was not refused: the plan is
built against a copy, so a rollback that is declined or refused leaves the working tree exactly as
it was. Files present now but absent from the snapshot are **deleted**, and named before the
confirmation prompt — the old spec plus whatever came after it is not the spec that was recorded,
so leaving them would not be a rollback. Scope is what `spec` captured and no wider: `*.yaml` and
`*.yml` under the ontology directory, never `loom.yaml`, never a file of any other kind.

**Rollback does not touch `_loom_meta.edits`.** It reverses DDL and only DDL, and the edit log is
rows — the same reason it leaves your data alone. This is not an exception carved out for a
Loom-created table: `rollback` executes through `apply`, which holds a writer with no verb that can
remove a row from anything.

## 9.2 `edits` — what an action did

One table per catalog, created by the **first action run against that catalog** — not by `apply`,
which never creates it and does not know it exists. Making `apply` the creator would give the log a
precondition the write does not have, and Loom writes to lakes it has never migrated, which are
exactly the ones where an audit trail matters most. Per catalog rather than per backing table for the
same reason `applied` is, plus one of its own: *what did this actor do today* is a cross-table
question, and a per-table sidecar cannot answer it.

```
_loom_meta.edits
  edit_id           string       required   # also stamped into the row write's own Iceberg commit
  recorded_at       timestamptz  required
  actor             string                  # supplied by the caller; `unknown` when nobody did
  action            string                  # the action's apiName
  object_type       string
  operation         string                  # create | modify | delete
  catalog           string
  table_name        string                  # the backing table the row lives in
  object_key        string                  # the primary key, rendered
  status            string                  # applied | refused | failed
  attempts          long                    # 1, or more when a conflict was retried
  read_snapshot_id  long                    # the snapshot the write asserted
  parameters        string                  # JSON: the bound call
  before            string                  # JSON: declared properties, or empty
  after             string                  # JSON: declared properties, or empty
  failures          string                  # JSON: the run's typed failures, conflict detail and all
  loom_version      string
```

`table_name` and `object_key` rather than `table` and `key`: this table is meant to be read from any
SQL engine someone points at the lake, and both of the shorter spellings are reserved words in
dialects Loom already targets. `edit_id` and `recorded_at` are the only required columns — this table
is only ever *created*, never altered, so a column omitted today can never reach a log that already
exists, and a required one is a column a future Loom can never add at all.

**Append-only, one row per run**, oldest first by `recorded_at`. A run that retried is one row
carrying `attempts`: the attempts that lost wrote nothing, so they are not edits.

**`before`/`after` hold declared properties**, through the same projection §4.1 reports — so what the
record does not name, the run did not change. Both are empty rather than null where there is nothing
to record (a `create` has no before, a `delete` no after).

**`read_snapshot_id` identifies the commit.** The write asserted it *inside* the commit, so on that
table's ref exactly one snapshot has it as a parent. That snapshot — and, for a `modify`, the append
that follows its equality-delete — carries `loom.edit_id`, `loom.action` and `loom.actor` in its
Iceberg **snapshot summary**. The duplication is the one §9 already makes with table properties, one
plane down: a table's history should say who changed it without the reader knowing a log table
exists. It is also the only record of an edit that is atomic with the edit, which is what makes a lost
log row a gap somebody can find rather than silence.

**Nothing Loom does removes a row from this table**, and that is an invariant rather than a missing
feature. The sentence above is what write-then-log buys, and it only holds while a reader can
conclude one thing from a stamp with no matching row: *the record was lost.* Expire records and it
means two things — lost, or expired — and the reader holding the stamp cannot tell which, which
spends the single property the ordering was chosen for. So `EditLogWriter` has no delete verb and is
not getting one, and a retention window is not a policy key (§6.1) but a command Loom has not built.

What that leaves owing is real and is narrower than "retention": declared properties are still
somebody's data, and this table outlives the row it describes, so a `delete` action erases a
customer and leaves the ontology's account of them behind. **Erasure does not require deletion.**
The answer that keeps the invariant is a **redaction in place** — the row kept, `edit_id`,
`recorded_at`, `action`, `operation` and `status` kept, and `parameters`/`before`/`after`/
`object_key` emptied — so the skeleton stays citeable, the stamp still finds a row, and the personal
data is gone. That is a rewrite rather than an append, by a holder that is not the action runtime.
See "Open edges".

## 9.3 `loads` — what an ingest did

One table per catalog, created by the **first load run against that catalog** — `edits`' rule, for
`edits`' reason. Under `governance.edit_log: required` it is created at startup instead, beside the
edit log, because that posture is a demand about writes and a bulk load is a write.

```
_loom_meta.loads
  load_id             string       required   # also stamped into the write's own Iceberg commit
  recorded_at         timestamptz  required
  entry               string                  # the `ingest:` entry that ran
  actor               string                  # `unknown` when nobody was named
  principal           string                  # always null today — ingest attests nobody
  object_type         string
  mode                string                  # append | merge | replace
  catalog             string
  table_name          string
  source              string                  # the path that was read
  source_fingerprint  string                  # sha256 of its bytes
  status              string                  # applied | refused | failed
  rows_read           long
  rows_written        long
  rows_rejected       long
  read_snapshot_id    long                    # null for an append, which reads nothing
  failures            string                  # JSON
  loom_version        string
```

**One row per load, not per row loaded.** The alternative is unaffordable — a million-row load would
write a million records — but it is also wrong in the way one record per *attempt* would have been
wrong for an action: a load is one decision and one commit. What varies per row is whether it was
written or rejected, and that is three integers.

**There is no `before` and no `after`, and that is a difference rather than an omission.** An
action's record carries them because a caller supplied a handful of values and *what did this change*
has an answer that fits in a row. A load's answer is the batch, and copying it here would make this
table an unabridged second copy of somebody's nightly drop — §9.2's leak at a scale where it is also
a storage bill. `source` and `source_fingerprint` replace them: not the data, but enough that an
auditor holding the file can prove it is the one that landed.

**A refusal is recorded once the load named itself.** `edits`' gate is *the run named a row*; this
one is *the load acquired an id*, which happens as soon as the file has been read and fingerprinted.
Everything after that — a bad column, a value that would not coerce, a lost race — writes a row with
`rows_written: 0`, because *who tried to replace this table* is as much an audit question as *who
tried to delete this customer*. The two refusals above the gate do not: a file that cannot be read
produces no id, so the record would cite nothing, and a deployment whose `governance.ingest` is
`refused` is declining to be the kind of thing that loads at all — recording it would mean creating
this table in exactly the deployments that set the posture to avoid having one.

**A `--dry-run` records nothing at all, including a refusal**, matching what `preview()` does on the
action plane. Asking whether a load would work must not write to the lake, and it must not create
this table in a catalog whose operator was asking a question. That interacts with one thing worth
naming, because `loom ingest` previews before every real load: a refusal discovered at preview would
otherwise never be recorded, so unless `--dry-run` was asked for, the command runs the refusing load
for real. Nothing is written either way — a refusal changes nothing — and the record then belongs to
an attempt somebody actually made.

**Nothing removes a row here either**, for §9.2's reason exactly, and the erasure question does not
arise the same way: this table holds counts and a fingerprint, never a customer.

---

[← §8 Worked example](./08-worked-example.md) · [Open edges (v0 → v1) →](./open-edges.md)
