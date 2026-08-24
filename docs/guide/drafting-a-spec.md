# Drafting a spec from a file

`loom infer` is the on-ramp. Every other command in this guide starts from a spec somebody wrote;
this is the one that goes the other way — it reads a parquet file's declared schema and prints a
draft objectType, plus the `ingest:` entry that would fill the table:

```
$ loom infer daily.parquet --as DailySalesPerformance --key sales_date \
    --catalog local --table sales.daily_sales_performance
objectType:
  apiName: DailySalesPerformance
  displayName: DailySalesPerformance
  primaryKey: salesDate
  backing: { catalog: local, table: sales.daily_sales_performance }
  properties:
    - { name: salesDate, type: date, column: sales_date, unique: true }
    - { name: grossSales, type: decimal, precision: 14, scale: 2, column: gross_sales }
    ...
```

It **opens no catalog and writes no file**, which is what keeps it clear of the rule it looks like
it bends: `BulkWriter` has no DDL verb because a *load* must never infer a *migration* from the
shape of somebody's file. This runs before there is a table, produces text, and stops.

And the draft **does not validate** until a person has been through it — `primaryKey` and `backing`
come out as placeholders no property matches, so `loom validate` fails on them by name. A scaffold
that emitted something immediately servable is a scaffold that gets committed unread.

Parquet only. A CSV declares no types at all, so every type would be sniffed from a sample — and
decimal-versus-double on a money column is the sniff that loses fractions of a cent silently. JSON
has no decimal and no date. Both are refused by name, with that reason.

Three things it will not guess, in any format: `enum` values (a file shows the values it happens to
hold, not the domain's set — the retail example's `closed` tier is in its enum for a reason no
sample reveals), `unique`, and **which columns to leave out**. A column whose type the spec has no
name for — an `array`, a `struct`, a tz-naive timestamp — is rendered as a comment saying it is
unmanaged rather than missing: `loom plan` reports it, nothing drops it, and every write carries it
across untouched (§2 rule 7).

---

Next: [planning and applying a schema change](./migrations.md).
