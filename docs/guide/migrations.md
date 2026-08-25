# Migrations — `plan`, `apply`, `renamedFrom`, `rollback`

You never hand-write a migration. The spec is the desired state, the live catalog is the baseline,
and Loom derives the difference between them — classifying each one by what it would cost.

## Planning a schema change

`loom plan` is the write path's dry run: it derives the tables the spec wants, diffs them against
the live catalog, and classifies every difference. Run it before seeding anything and the whole
warehouse is a creation:

```
$ loom plan examples/retail/ontology
Loom plan — examples/retail/ontology

  + local.crm.customers — create table · Customer
      + id              string required
      + full_name       string required
      + tier            string required
      + lifetime_value  double optional
...
Plan: 2 to create, 0 to change · 8 safe
```

The classification is the point. Iceberg will let you make a change that costs nothing, one that
rewrites the schema but not the data, and one that quietly invalidates existing rows — and all
three look identical in a YAML diff. Against a table already holding rows, they don't:

```
$ loom plan ./ontology
  ! local.demo.widgets — 3 change(s) · Widget
      ~ score     int -> long           physical-safe
          widening promotion applied by field id 2; existing data files are not rewritten
      ! label     optional -> required  breaking
          existing rows may already hold nulls, which the new constraint would not admit
      + nickname  string optional       safe

Plan: 0 to create, 1 to change · 1 safe, 1 physical-safe, 1 breaking
```

**`physical-safe` is Iceberg's promotion set and not a wider one.** Iceberg promotes `int -> long`
and `float -> double`, and nothing else among the types a spec can name. Anything else is
`breaking` here even where it reads perfectly well today: an `int` column under a `double` property
is one `loom validate --physical` accepts — every value in it is exactly representable and every
engine widens it on the way out — and Loom still will not call changing the *stored* type free,
because Iceberg would have to rewrite the data and Loom does not rewrite data. Those two commands
answering differently about one column is the honest pair; a plan that promised the change and an
apply that met `Cannot change column type` half way through was not.

Two rules shape it. The **live catalog is the baseline** — no state file to drift out of sync, so
a table someone changed out of band shows up honestly. And **Loom never proposes a drop**: an
objectType maps a subset of a table's columns, so a column no property mentions is someone else's
data, reported as unmanaged and left alone.

## Applying it

`loom apply` executes that same plan — it prints it first, then asks — and creates the namespaces
it needs along the way, so an empty warehouse becomes a working ontology with no seed script:

```
$ loom apply examples/retail/ontology
Loom plan — examples/retail/ontology
...
Plan: 2 to create, 0 to change · 8 safe

Apply these changes? [y/N] y

  + local.crm.customers — created · namespace 'crm' created
  + local.sales.orders — created · namespace 'sales' created
Applied 2 table change(s). Recorded as version 1 in `_loom_meta` (local).
```

Run it again and it has nothing to do — the diff is re-derived from the live catalog every time,
so idempotency isn't a bookkeeping trick, it's the same mechanism that makes `plan` honest:

```
$ loom apply examples/retail/ontology
No changes — the catalog already matches the ontology.

Already applied — nothing to do. Recorded as version 1 in `_loom_meta` (local).
```

Three rules shape the executor:

- **A breaking plan is refused whole**, and nothing runs — not even the safe tables in it. The
  fix for a breaking change is a data migration (add the column nullable, backfill, then tighten),
  and there is no `--force`, because forcing it wouldn't make it safe.
- **One table, one Iceberg transaction.** That is Iceberg's unit of atomicity, so it is Loom's:
  a table's column changes and its provenance properties commit together. Across tables the run
  is sequential and stops at the first failure, and says exactly which tables landed — an honest
  partial beats a pretend-atomic one.
- **Writes go through their own port.** The resolver, the query engines and `loom serve` hold a
  read-only `Catalog` and could not execute DDL if they tried; only `apply` asks for a
  `CatalogWriter`.

Every apply appends to `_loom_meta.applied`, an ordinary Iceberg table in the lake: the spec's
source, its content hash, a version, who ran it, and what it did. It lives in the lake rather than
beside the YAML because a state file only ever describes the checkout it sits in — and it is
history, never the planner's input.

## Renaming a column

Change a property's `column` and the planner has no way to know the old one and the new one are
the same column: it adds the new one and leaves the old sitting there full of data. `renamedFrom`
says they're the same column, and the migration becomes an Iceberg **rename** — the field id is
unchanged, so nothing is rewritten and nothing is stranded:

```yaml
- { name: ltv, type: double, column: ltv_usd, renamedFrom: lifetime_value, nullable: true }
```

```
$ loom plan ./ontology
  ~ local.crm.customers — 1 change(s) · Customer
      ~ ltv_usd  renamed from lifetime_value  safe
          the column keeps field id 4, so no data file is rewritten; readers outside the
          ontology that select 'lifetime_value' by name will need updating
```

The spec states the intent; **the live catalog decides what it currently means.** That same
property, unchanged, plans four ways: the rename if only the old column is there, *nothing at all*
if only the new one is, a warning and a plain add if neither is — and a refusal if both are.

That last one is a rename target that already exists: a mistake, or a migration somebody finished
by hand halfway. Loom can't merge the two columns, because merging means dropping one:

```
$ loom apply ./ontology
  ! local.crm.customers — 1 change(s) · Customer
      ! ltv_usd  renamed from lifetime_value  breaking
          'lifetime_value' and 'ltv_usd' both exist in 'crm.customers' — Loom never drops a
          column, so it cannot merge them; move the values across and drop 'renamedFrom', or
          remove 'lifetime_value' out of band

refusing to apply: the plan contains breaking changes
  nothing was applied — no table is left half-migrated
```

Two things follow from the second row of that table. Applying is **idempotent for free** — the
rename lands, and every plan after it is clean without anything being ticked off in the spec.  And
`renamedFrom` **stays in the file afterwards**; Loom will never tell you to remove it, because one
spec is deployed to more than one lake, and after a rename ships to production, staging is still on
the other side of it. "You can delete this now" would be true of one catalog and false of another
from the same file. `_loom_meta` records which version did the rename, which is the honest place
for that answer.

## Rolling back

An apply that went wrong needs an answer other than hand-editing the YAML back to what it was.
`loom rollback` restores the spec `_loom_meta` recorded, re-plans it against the live catalog, and
executes that — the same loop as `apply`, over an older spec:

```
$ loom rollback ./ontology --to 1
Loom rollback — ./ontology
Restoring the spec recorded at version 1 (from local).
Rows are untouched — `apply` only ever ran DDL, so this only reverses DDL.

  ~ local.crm.customers — 1 change(s) · Customer
      ~ ltv_usd  renamed from lifetime_value  safe
          the column keeps field id 2, so no data file is rewritten; readers outside the
          ontology that select 'lifetime_value' by name will need updating

Plan: 0 to create, 1 to change · 1 safe

Left in place — a rollback never drops, so these stay live and unmanaged:
  · local.crm.customers: region — added after version 1

Spec files:
  ~ customer.yaml — restored
```

**Only renames actually reverse, and it says so rather than pretending otherwise.** Of the four
things Loom can do to a column, a rename is the one that undoes itself: the same field id comes
back under the old name, and no data file is rewritten. An add reverses to a *drop* — and Loom
never drops — so `region` above stays live, the restored spec no longer maps it, and it is
unmanaged from here on. That is the honest report, which is why it's printed rather than left to be
found later. A table created since is left whole for the same reason.

Reversing a promotion is a narrowing and reversing a loosening is a tightening, and both are
breaking, so those rollbacks are refused whole like any other breaking plan. That isn't a hole:
once the column is a `long`, the spec that says `int` no longer describes this lake, and the way
out is forward.

Two more things follow from `_loom_meta` being history rather than state. **A rollback is an
append** — a new row carrying the restored spec's text and hash, never a deleted one — so the next
`loom apply` sees a spec that is already live and does nothing. And the **reverse rename comes out
of that history**: `renamedFrom` points forward, so the version-1 spec can't name the column it has
to be renamed back from, but version 2 recorded what it renamed and rollback inverts it (composing
the chain if there were several).

**It reverses DDL and only DDL.** `apply` never wrote a row, so `rollback` never deletes one — no
snapshot rollback, no expiry. Rows written since are not Loom's to throw away. Spec files are the
last thing it writes and only if the run wasn't refused, so a rollback you decline leaves the lake
*and* the working tree exactly as they were.

---

Next: [running an action](./actions.md) — the only thing in Loom that changes a row.
