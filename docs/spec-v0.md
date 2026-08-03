# Loom Ontology Spec — v0 Grammar

> The framework's public contract. Every Loom surface — Iceberg schema/migrations, the
> query resolver, the action runtime, and the MCP tool registry — is **compiled from these
> files**. If it isn't expressible here, the framework can't do it. Keep this small and honest.

---

## 0. Conventions

- **Files.** Ontology lives under `ontology/**/*.yaml`. Each file declares exactly **one**
  top-level kind: `objectType`, `linkType`, or `action`. Split freely across directories;
  the loader globs and merges.
- **Identifiers.** Everything references everything else by `apiName` (never by file path).
  - Object types: `PascalCase`, match `^[A-Z][A-Za-z0-9]*$`
  - Links / actions / properties / parameters: `camelCase`, match `^[a-z][A-Za-z0-9]*$`
  - `apiName` is **globally unique within its kind** (object namespace, link namespace,
    action namespace are separate).
- **Unknown keys are errors**, not ignored. A typo like `primryKey` fails `loom validate`
  with a "did you mean" — a spec language that silently drops fields rots.
- **Everything compiles or nothing loads.** `loom validate` runs the full ruleset below and
  either produces a consistent Ontology Model or exits non-zero. There is no partial load.

---

## 1. Canonical type system

The one table the whole framework leans on: each Loom type simultaneously knows its Iceberg
type (drives DDL + physical validation) and its JSON-Schema shape (drives the MCP tool
contract). A property/parameter `type` is one of:

| Loom type      | Iceberg type      | JSON Schema (MCP)                          | Notes |
|----------------|-------------------|--------------------------------------------|-------|
| `string`       | `string`          | `{type: string}`                           | |
| `boolean`      | `boolean`         | `{type: boolean}`                          | |
| `int`          | `int`             | `{type: integer, format: int32}`           | |
| `long`         | `long`            | `{type: integer, format: int64}`           | |
| `double`       | `double`          | `{type: number}`                           | |
| `decimal`      | `decimal(P,S)`    | `{type: string, pattern: …}`               | requires `precision`, `scale` |
| `date`         | `date`            | `{type: string, format: date}`             | |
| `timestamp`    | `timestamptz`     | `{type: string, format: date-time}`        | tz-aware; UTC on the wire |
| `enum`         | `string` + check  | `{type: string, enum: [...]}`              | requires `values` |
| `objectRef`    | (referenced PK)   | `{type: string}`                           | **parameters only**; requires `objectType` |

Deferred to a later version (call it out rather than pretend): `array<T>`, `struct`, `map`,
`geo`. Adding one = one new row here + adapter lowering; that's the extension shape.

**Type compatibility** (property `type` vs. its backing Iceberg column) follows Iceberg's own
promotion rules: `int→long`, `float→double`, `decimal(P,S)→decimal(P',S)` with `P'≥P` are
compatible; anything narrowing is a validation error.

---

## 2. `objectType`

A semantic entity bound to one Iceberg table.

```yaml
objectType:
  apiName: Customer              # required · PascalCase · unique
  displayName: Customer          # optional · default = apiName
  description: A buying account   # optional · surfaced to the LLM
  primaryKey: customerId          # required · must name a property below
  title: name                     # optional · property used as human label · default = primaryKey
  status: active                  # optional · active | deprecated | experimental · default active

  backing:                        # required
    catalog: rest_main            # required · a catalog declared in loom.yaml
    table: crm.customers          # required · Iceberg identifier "namespace.table"

  properties:                     # required · non-empty
    - name: customerId            # required · camelCase · unique within object
      type: string                # required · from §1
      column: id                  # required · physical Iceberg column
      nullable: false             # optional · default false
      unique: true                # optional · default false (advisory unless it's the PK)
      description: Stable id        # optional

    - name: name
      type: string
      column: full_name

    - name: tier
      type: enum
      values: [bronze, silver, gold]   # required iff type == enum · non-empty · unique
      column: tier

    - name: ltv
      type: double
      column: lifetime_value
      renamedFrom: legacy_ltv     # optional · the column `column` used to be — see below
      nullable: true

  searchable: [name, tier]        # optional · property names powering search_<type>
```

**Validation rules**

1. `apiName` matches the PascalCase pattern and is unique among object types.
2. `primaryKey` names an existing property; that property must be `nullable: false` **and**
   `unique: true` (the framework sets `unique: true` implicitly for the PK and errors if you
   declared it `false`).
3. `title` names an existing property.
4. Property `name`s are unique within the object; `column`s are unique within the object.
5. `enum` properties declare a non-empty, duplicate-free `values` list. `decimal` properties
   declare `precision` (≥1) and `scale` (0 ≤ scale ≤ precision).
6. `searchable` entries name existing properties whose type is `string` or `enum`.
7. **Physical check** (at `loom plan` / `loom serve`, against catalog introspection): every
   `backing.table` exists, every `column` exists on it, and every property type is compatible
   with its column's Iceberg type per §1. Missing table/column → error; incompatible type →
   error; extra columns on the table that no property maps → warning (not an error).
8. `renamedFrom` (if set) is a non-empty column name and is **not** the property's own `column`.
9. No property's `renamedFrom` may equal any property's `column` **on the same table** — including
   columns contributed by a *different* declaration bound to that table. This is what makes
   renames independent of one another: neither a chain (`a→b` and `b→c`) nor a swap is
   expressible, which is in turn what lets the planner order edits per column.
10. Two columns on one table may not declare the same `renamedFrom`. One column cannot become two.
11. Two declarations mapping the same column may not name *different* `renamedFrom` sources.
    Silence is **no opinion**, not "there was no rename", so only one of them has to say it.

### 2.1 `renamedFrom` — a moved column, not a new one

Without it, changing a property's `column` reads to the planner as "add the new one" and leaves
the old one sitting there as unmanaged data. `renamedFrom` says the two are the same column, so
`apply` issues an Iceberg **rename** — the field id is unchanged, so no data file is rewritten and
nothing is stranded.

The spec says what it wants; **the live catalog decides what that currently means.** The same
property, unchanged, plans four different ways:

| live table has | `loom plan` |
|---|---|
| the old column only | the rename · **safe** |
| the new column only | nothing — the rename already landed |
| neither | a warning, then an ordinary `add` of the new column |
| **both** | the rename · **breaking**, and `apply` refuses the whole plan |

*Both* is the only loud one. A rename target that already exists is either a mistake or a
half-finished migration, and Loom cannot merge the two columns because merging means dropping one.
It is a breaking *change* rather than a load error on purpose: the spec is fine, the lake is in a
shape this plan can't resolve — so the rest of the diff still prints, and the refusal comes from
the same whole-plan machinery as every other breaking change. Fix it by moving the values across
and dropping `renamedFrom`, or by removing the old column out of band.

A rename is classified **safe**, not physical-safe. Physical-safe is the label that means *the
stored type moved, and existing files only still read because Iceberg addresses columns by field
id*. A rename moves nothing: field id, type and nullability all survive. What it does break is
readers **outside** the ontology that select the column by name — a contract change rather than a
data-safety one, so it is stated in the plan's reason line rather than raised to a louder severity.

If a rename is combined with a promotion or a loosening on the same column, they are one
`alter_table` — one Iceberg transaction — and the rename is ordered first.

**Lifetime.** `renamedFrom` outlives its migration and Loom will never tell you to remove it. One
spec is deployed to more than one lake, and after the rename ships to production, staging and every
fresh developer warehouse are still on the other side of it; "you can delete this now" would be
true of one catalog and false of another *from the same file*. So it stays, planning as a clean
no-op wherever it is spent. When it is safe to delete is a question `_loom_meta` answers (§9): it
records the version that performed the rename.

**Chains are deliberately not expressible.** `renamedFrom` is one hop. A chain `a→b→c` only ever
arises across separate applies — within a single edit, `column: c, renamedFrom: a` is how you say
it — and the lake that would need the chain is one that skipped an intermediate apply, where the
answer is to apply the intermediate versions rather than to teach one spec every name a column has
ever had. A list would also make the *both* case combinatorial. A lake that skipped a hop lands in
the *neither* row above: warned, not silently stranded.

---

## 3. `linkType`

A relationship between two object types. FK-style links need no physical table (they compile
to a JOIN); many-to-many needs a `through` mapping table.

```yaml
linkType:
  apiName: placedBy               # required · camelCase · unique among links
  displayName: Placed by
  description: The customer who placed this order
  cardinality: many_to_one        # required · one_to_one | one_to_many | many_to_one | many_to_many
  from:
    objectType: Order             # required · existing object type
    property: customerId          # required · join property on `from`
  to:
    objectType: Customer          # required · existing object type
    property: customerId          # required · join property on `to`
  reverseName: orders             # optional · backref traversal exposed on the `to` object
  status: active

  # required ONLY for cardinality: many_to_many — otherwise must be absent
  through:
    catalog: rest_main
    table: crm.order_customer
    fromColumn: order_id
    toColumn: customer_id
    renamedFrom:                  # optional · §2.1, per side
      fromColumn: order_ref       # `order_id` used to be `order_ref`
```

**Validation rules**

1. `from.objectType` and `to.objectType` exist.
2. `from.property` and `to.property` exist on their respective object types, and their
   canonical types are comparable (equal after promotion).
3. `through` is **required iff** `cardinality == many_to_many`, and **forbidden otherwise**.
   When present, its columns are physically checked like any backing table — and planned like
   one, which is why `through.renamedFrom` exists: a mapping table is a real table, and leaving it
   out would make it the one table Loom plans but cannot rename a column on. Its keys are exactly
   `fromColumn` / `toColumn`, both optional, each following §2.1 and its rules.
4. `reverseName` (if set) collides with nothing on the `to` object — not a property name, not
   another link's `apiName`, not another link's `reverseName`.
5. **Cardinality sanity** (advisory warnings, not errors): for `many_to_one`, `to.property`
   should be unique (it's the "one" side); for `one_to_many`, `from.property` should be unique.
   Loom warns on a likely-mismodeled join rather than silently producing fan-out.

---

## 4. `action`

The kinetic layer. **v1 is single-object**: an action mutates exactly one instance of its
`targetObjectType`, producing one atomic Iceberg commit. This constraint is enforced here, at
spec-load — not discovered at runtime.

```yaml
action:
  apiName: upgradeTier            # required · camelCase · unique among actions
  displayName: Upgrade tier
  description: Raise a customer to a higher membership tier   # required · this IS the MCP tool description
  targetObjectType: Customer      # required · existing object type
  operation: modify               # required · create | modify | delete
  status: active

  parameters:
    - name: customer              # required · camelCase · unique within action
      type: objectRef             # from §1
      objectType: Customer        # required iff type == objectRef
      required: true              # optional · default true
      description: Customer to upgrade
    - name: newTier
      type: enum
      values: [silver, gold]      # required iff enum
      required: true

  validation:                     # optional · preconditions, checked before the write
    - rule: "newTier != object.tier"     # `object.<prop>` is the row as it is *now* — see §5
      message: New tier must differ from the current tier

  effects:                        # required · exactly one entry (single-object)
    - modifyObject:
        key: "{{ customer }}"     # expression → PK of the target object
        set:
          tier: "{{ newTier }}"   # propertyName: valueExpr
```

**Effect grammar by `operation`** (exactly one effect entry, and its kind must match
`operation`):

```yaml
# operation: create
effects:
  - createObject:
      set: { <propertyName>: <expr>, ... }   # must cover primaryKey + every non-nullable, non-defaulted property

# operation: modify
effects:
  - modifyObject:
      key: <expr>                             # → target PK
      set: { <propertyName>: <expr>, ... }

# operation: delete
effects:
  - deleteObject:
      key: <expr>                             # → target PK
```

**Validation rules** (the boundary-keeping ones matter most)

1. `targetObjectType` exists. `operation` matches the single effect kind
   (`create→createObject`, `modify→modifyObject`, `delete→deleteObject`).
2. **Single-object boundary:** exactly one effect entry, and it targets `targetObjectType`
   only. Any effect that references a second object type is **rejected at load** — this is the
   v1 scope wall, made a hard error, not a convention.
3. Every `set` key names a real property on `targetObjectType`; each value `<expr>`
   type-checks to that property's type.
4. `create`: `set` must cover the `primaryKey` and every property that is `nullable: false`
   without a `default`. `modify`/`delete`: `key` `<expr>` resolves to the PK's type.
5. Parameters: `objectRef` params name an existing `objectType`; `enum` params declare
   `values`; a `default` (if present) type-checks and implies `required: false`.
6. `validation[].rule` and every `<expr>` reference **only** declared parameters and
   properties of the target object (see §5) — no free variables. `object.<prop>` is in scope for
   `modify`/`delete` only: a `create` has no prior object.
7. **Concurrency is implicit:** every operation is read-then-write under optimistic concurrency —
   the write asserts the snapshot the read saw, inside the same commit. No YAML expresses it; the
   runtime always does it, for `create` as well as `modify`/`delete` (§4.1 says why all three).

### 4.1 What running an action actually does

The grammar above is half the contract. The other half is the runtime, because an action is the
only thing in Loom that changes a row, and what it does to the columns the spec *doesn't* mention
is as much a promise as what it does to the ones it does.

**One row, four steps, one commit, one record.** Bind the parameters → read the target row →
evaluate every validation rule → one write → one row in the edit log. Everything that can refuse
happens in the first three, so **a run that refuses changes nothing it was asked to change** — the
same promise `apply` makes about a breaking plan.

That sentence is deliberately narrower than "changes nothing", which is what it said before the edit
log existed. A refused run writes no data — no row, no column, no table — and is *recorded*. See
"The edit log" below for why the wording moved rather than the behaviour being quietly excused.

**A `modify` is an equality-delete plus an append, and therefore a full-row rewrite.** This is why
the read that precedes the write is a *whole physical row*, not the ontology's projection of one:
every column no property maps has to be carried across, or the write silently nulls it. Those are
the same columns `plan` reports as unmanaged and leaves alone (§2 rule 7) — the never-drop rule one
level down, where the data is rather than the schema.

A column whose **type** the ontology has no name for — an `array`, a `struct`, a `map`, anything
§1 defers — is carried the same way: untouched and unexamined. The runtime builds no type for it
and never inspects the value; the conversion is driven by the table's own schema. Only the columns
an effect `set`s pass through the type system.

**A `delete` is one row, and it does not contradict "Loom never drops."** Never-drop is about
*inference*: Loom refusing to read a destruction into the **silence** of a spec, because a column
nothing declares is someone else's data rather than a deleted property. `operation: delete` is the
opposite of silence — a person wrote the word, named the object type, and the key arrives as a
declared parameter. The scopes differ too. Never-drop governs **schema**: Loom never drops a column
or a table, in any command. This removes **one row**, addressed by primary key.

**The key is checked for uniqueness before the write.** The primary key is single-property in v0
and Loom does not own the table, so nothing physically guarantees it is unique — and an
equality-delete on a key matching two rows would remove both and append one. A key matching more
than one row is refused (`ambiguous_key`), naming the table. Loom cannot repair it: the two rows
are still there and the fix is out of band.

**Failures are typed, and all of them are reported.** Nothing a caller, an author or the data can
cause is an exception. A run comes back with a status (`applied` · `previewed` · `refused` ·
`failed`) and a list of failures, each carrying a code from a closed set — `missing_parameter`,
`unknown_parameter`, `type_error`, `validation_failed`, `expression_error`, `object_not_found`,
`object_exists`, `ambiguous_key`, `write_failed`, `conflict`, `log_failed`. A failed rule carries the spec's own
`message`, verbatim. Every rule is evaluated rather than stopping at the first failure, for the
same reason `loom validate` reports every problem at once.

**Concurrency, and what it is a guarantee about.** Rule 7 is now true. The runtime records the
snapshot each read saw and hands it to the write, which asserts it **inside the commit** — for
Iceberg, an `assert-ref-snapshot-id` requirement the catalog validates against live metadata as the
table's metadata pointer swaps. A run that loses is declined before it commits, so it changes
nothing, exactly as every other refusal does; it comes back `conflict`, the one retryable code.

The distinction that word is doing work for: this is not a re-read and a comparison. A runtime that
compares and then writes has a window between deciding and committing — it *narrows* the race rather
than closing it, and "optimistic concurrency" is a phrase that promises closed. The check is carried,
not performed.

**What counts as the row moving: the whole table.** The check asserts the table's snapshot, so a
commit anywhere in the table conflicts with a run that had nothing to do with it. That coarseness is
chosen. Iceberg's commit protocol can assert a ref's snapshot and nothing finer, so the only narrower
test is comparing the row itself — and a row comparison cannot be carried into a commit, which would
trade the guarantee for the precision. Coarse-and-closed beats narrow-and-open.

Two consequences follow, and both are deliberate. **A competing write to a column no property maps is
a conflict**, which is the answer to the question the carry-across rule above leaves open — not
because the runtime inspected the column (it inspects none), but because a `modify` writes that
column back from a read taken before the competing commit, so committing anyway would restore a stale
value over somebody else's newer one. Loom will not read that column and will not overwrite it blind.
And **false conflicts exist by construction**: the snapshot is read *before* the rows, so the recorded
id is at-or-before the data, and the check reports conflicts that weren't ones but can never miss one
that was. The other order silently blesses a lost update.

**A conflict is retried inside the run, up to three times, and the result says how many.** That is
what makes the coarse check usable: something has to absorb the conflicts it invents, and pushing
that onto every caller means every caller writing the same retry loop. Each attempt re-reads and
re-evaluates every rule and every effect expression against the row actually about to be written
over — never a replay, which would write values computed against a row that no longer exists. A retry
can therefore succeed against a row the caller never saw; what makes that sound is that
`validation` rules *are* the caller's statement of which states it will act on, and they are checked
against the newer row. Where a competing write genuinely invalidates the action, the retry reports
the real reason — `validation_failed`, `object_not_found` — rather than a conflict inviting an agent
to retry something that cannot succeed. `attempts` is on the result because "applied" after three
internal re-reads is a different fact from "applied".

**All three operations are checked, each for its own reason.** `modify`, for the carry-across above.
`create`, because its read is the primary-key existence check and two concurrent creates both pass
it, then both append — manufacturing exactly the duplicate row the runtime refuses as `ambiguous_key`
ever after and can never repair; checked, only one can commit against the snapshot both read. (This
guarantees nothing about a writer that isn't Loom, which is why `ambiguous_key` stays.) `delete`,
because it is the only irreversible one: a conflicting modify can be re-applied and a conflicting
create refuses cleanly, but a delete that lost a race is gone, and the competing write may have been
a `modify` rather than another delete — in which case the row is not "already gone", it changed. When
the competing write really was a delete, the retry re-reads, finds nothing, and returns
`object_not_found`, which is that outcome stated accurately.

**What `conflict` carries.** Not just "retry": an agent told only that will hammer a table that is
merely busy and give up just as readily when its intent has genuinely been overtaken. `detail` holds
the table, `expectedSnapshotId` and `foundSnapshotId` (the latter advisory — read after the refusal,
so on a hot table it may already be past the commit that won), `attempts`, `changed` — the **declared
properties** that moved, diffed through the same projection `before`/`after` use, so unmapped columns
are compared no more than they are reported — and `contended`, whether any of those are properties
this action reads in a rule or writes in an effect. A busy table and a contested row are different
situations, and the message says which.

**The confirmation prompt is outside the window.** `loom run` previews, asks, then runs — and the
run does its own read, which is the one it asserts. A human's thinking time is therefore not inside
the transaction, and what the prompt asks a person to approve is the *shape* of the change. That is
also the only answer that can be true of both callers: `run_<action>` has no prompt at all, so a
design in which the checked snapshot came from a preview is one the MCP caller could never join.

That last sentence is still exactly true, and `run_<action>`'s `dryRun` (§7) does not soften it.
A preview over MCP produces the same *shape* the CLI prints above its `y/N`, and reserves the same
nothing: no state links it to a later call, no row is held, and a run after it reads again and
asserts that read. What the CLI has and the tool does not is somebody to ask — and that is where it
stays. Approval of an agent's tool call belongs to whatever the client puts in front of its user;
Loom's part is to make the shape knowable before the write, which is what a preview is. Without one,
`previewed` would be a status no MCP caller could ever observe.

**The edit log.** Every run that named a row appends one record to `_loom_meta.edits` (§9.2) — the
data-plane counterpart to what `_loom_meta.applied` records about schemas.

*Refusals are recorded, and that is why the promise above is worded the way it is.* An audit trail
holding only successes cannot answer *who tried to delete this customer*, which is close to the only
question audit trails exist for — and since a conflict is a refusal, a contended row would otherwise
leave no trace of the attempts it swallowed. So a refusal writes no data and does leave a record that
it was attempted. `apply` still refuses before it holds a writer and records nothing at all: a
stronger instance of the same rule, not an exception to it. The asymmetry is deliberate — an `apply`
refusal is local, printed, and reproducible from a file still on disk; a run refusal is remote, seen
by nobody, and unreproducible, because the row it was refused against has already moved on.

*A run is recorded once it named a row.* A call that could not be bound — a missing parameter, a
value outside a declared enum — never resolved a key, so its record would carry none and answer no
audit question. That is a *request* log and belongs at the serve boundary. Previews are never
recorded: a preview writes nothing, and `loom run` previews before every real run. A `failed` write
**is** recorded; it is the one status where nobody knows whether the row changed.

*The record holds declared properties only* — the same projection `before` and `after` use, and the
same rule, extended to a new reader rather than excepted for one. The physical row was the
alternative and it is worse than the leak that rule prevents: an unabridged second copy of the data,
in a table nothing governs, retained forever, and the copy that *outlives* the row — which would make
a `delete` action erase a customer into a permanent record of them. The objection that this is an
incomplete account of a full-row rewrite is answered by the carry-across guarantee plus the snapshot
check: every unmapped column was written back unchanged and nothing moved under the run, so **what
the record does not name, the run did not change.** The bound parameters are recorded too, because a
refused modify has no `after` and would otherwise record that somebody tried without recording what.

*The actor is supplied by the caller, never invented.* `$LOOM_ACTOR`-or-OS-user is honest for a
command a person runs and a lie for a served tool, where it would name whoever started `loom serve`
and stamp every caller in the deployment with one string. `loom run` passes it explicitly;
`run_<action>` passes `mcp.actor` (§6); when nobody supplies one the record says `unknown`, which is
worth more than a confident wrong answer.

The distinction that key rests on is *declared* versus *inferred*, not process versus caller. An
operator writing `actor: agent:support-bot` is stating something true about a deployment. Loom
falling back to the OS user would be inferring the same shape of answer without anyone having
checked it. A client-supplied actor was the third option and is worse than `unknown`: an audit
record whose subject fills in its own name is self-attestation, and MCP has no identity for it to
attest with.

This used to carry a second clause — *and over stdio it is exactly true, because one client spawns
one process and the session has one principal.* That was **already doing less work than it looked
like**, and the HTTP transport is where it gets corrected rather than extended. `mcp.actor` lives in
`loom.yaml`, which configures a *deployment*, so three stdio clients reading one file already record
one string for three callers. One name for many callers is not something a socket introduced, and
declared-versus-inferred — the part that was load-bearing — is untouched by either transport.

What a socket does change is **reachability**: not how many callers share the name, but who is
permitted to be one of them. §6 draws the limit there, on the bind address rather than on the
transport, and refuses `writes: true` on a non-loopback bind.

So it is worth saying plainly what the log is worth over an unauthenticated transport. With no
`mcp.actor` set, every served write records `unknown`, and the record still answers *what* was done,
to *which row*, *when*, with *which parameters*, and *whether it refused* — it does not answer
*who*. That is a gap in the transports Loom currently speaks, neither of which authenticates
anybody, rather than a gap in the log; the config key is the honest way to close it for a
deployment, and an authenticated transport is the way to close it per call. What "authenticated"
has to mean there is not a header Loom reads — see the open edge on per-caller identity.

*The write carries its own identity, and the log is written after it.* Iceberg has no transaction
spanning two tables, so the row write and the log append are two commits and a crash can land between
them. What survives that is the row write's **snapshot summary**, which carries `loom.edit_id`,
`loom.action` and `loom.actor` inside the very commit that changed the data — the only attribution
here that cannot be separated from the edit. A lost log row is therefore a stamped snapshot with no
matching record: a gap a reader can find, rather than silence. It is also what makes `failed`
answerable — if a snapshot carries the id, the write landed. The guarantee is asymmetric and worth
stating: a lost record of a *refusal* is not detectable, because a refusal leaves nothing to stamp.
A failed append never fails the action, which has already committed; it comes back as a
non-retryable `log_failed` beside the real status.

*A retried run is one record.* The attempts that lost wrote nothing, so they are not edits; they are
one edit that took several tries, and `attempts` says so. The states they lost to are not this run's
to describe — a competing writer coming through Loom has its own record in the same table.

---

## 5. Expression mini-language

Deliberately tiny so it stays portable across engines and safe to evaluate. Used in
`validation[].rule` (must yield boolean), effect value positions (must yield the target property's
type), and a governance policy's `rows:` predicate (§6.1, which narrows what it may contain but not
what any of it means). Written inline or inside `{{ … }}`.

- **References:** a bare identifier `paramName` resolves to a parameter; `object.propName`
  resolves to the *current* value of the target object's property (for `modify`/`delete`).
- **Literals:** string `'...'`, number, `true`/`false`/`null`. An enum value is a string, so it is
  quoted: `'gold'`, never bare — a bare word is always a reference.
- **Operators:** comparison `== != < <= > >=`, boolean `&& || !`, arithmetic `+ - * /`,
  string `+` (concat).
- **Function allow-list (only these):** `now()`, `lower(s)`, `upper(s)`, `len(s)`,
  `coalesce(a, b, …)`.
- **No** loops, lambdas, property assignment, external calls, or arbitrary code. Anything
  richer belongs in a future custom-function extension point, not the expression language.

### 5.1 `{{ … }}` is punctuation, not a second language

`key: "{{ customer }}"` and `rule: "newTier != object.tier"` are the **same grammar**. The braces
are optional and are stripped at load, so nothing downstream — evaluator, validator, engine — ever
sees one. Two things follow, and both are load-bearing:

- **An effect value may be any expression**, not only a parameter reference. That is what makes
  `placedAt: "now()"` and `tier: "upper(newTier)"` expressible. `{{ customer }}` is the degenerate
  case of the general thing, not a different thing.
- **There is no string interpolation.** `"tier-{{ newTier }}"` is a load error, not a template.
  Building a string is the expression language's own `+`.

### 5.2 What the evaluator does with values

Type-checking happens offline (§4 rule 3); this is what the values themselves do at run time.

- **The value domain is the read path's.** `decimal` is a decimal all the way through and never
  passes through binary floating point — mixing a decimal and a float in arithmetic is an error
  rather than a silent choice of precision. `timestamp` is tz-aware. A number destined for an
  `int`/`long` property must be integral; it is never truncated to fit.
- **Null is a value, not an unknown.** `null != 'gold'` is **true** and `null == null` is **true**.
  This is deliberately not SQL's three-valued logic: an "unknown" precondition would leave the
  runtime no safe option but to refuse, making `null` a hazard in every rule written about a
  nullable property. A precondition is meant to be a decision.

  This used to be argued from "the language never reaches SQL", and §6.1's row predicates make that
  half false — one *is* compiled into a query. The rule survives the correction unchanged, and is
  stronger for being argued from what it is for: equality means the same thing on both planes
  because the lowering says `IS NOT DISTINCT FROM`, not because nothing was listening. What a
  predicate does differently is not the meaning of an operator but the **disposition of "cannot
  decide"**: a rule reports `expression_error` to the caller who can fix it, and a policy, having
  nobody to tell, does not admit the row.
- **But null cannot be ordered or computed with.** `<`, `<=`, `>`, `>=`, arithmetic, `!` and the
  boolean operators all fail on null rather than inventing an answer. `&&` and `||` short-circuit,
  which is what makes that workable — `object.ltv != null && object.ltv > 100` is the idiom, and
  `coalesce` is in the allow-list for the same reason. (In a `rows:` predicate they do **not**
  short-circuit: nothing there raises, so the only thing short-circuiting could still do is make
  the answer depend on the order the operands were written in — which is exactly what SQL does not
  do, and therefore what the two planes could disagree about.)
- **A rule that cannot be evaluated is not a rule that returned false.** It is its own failure
  code (`expression_error`), because an agent should not retry the two the same way.

---

## 6. Project config — `loom.yaml`

Not part of the ontology, but the grammar the ontology's `catalog:` / engine references
resolve against.

```yaml
version: 0
catalogs:
  rest_main:
    type: iceberg-rest            # v1 catalog type
    uri: https://catalog.internal/api
    warehouse: s3://lake/warehouse
    auth: { type: oauth2, ... }   # opaque to the spec; passed to the catalog client
engine:
  type: duckdb                    # duckdb | trino | spark  (read path; §7 of the design)
  options: {}                     # engine-specific
mcp:
  name: loom
  transport: stdio                # stdio | http
  writes: false                   # expose run_<action> tools at all · default false
  actor: null                     # who a served run is recorded as · declared, never inferred
  host: 127.0.0.1                 # http only · the bind, and the posture
  port: 8000                      # http only
  path: /mcp                      # http only
  allowed_hosts: []               # http only · Host allow-list; derived on a loopback bind
governance:                       # optional · what this deployment withholds, and what it demands
  edit_log: optional              # optional | required · refuse to run if it cannot record a write
  policies:
    - name: hide-ltv              # required, unique — a refusal names it
      objectType: Customer        # the objectType it governs
      mask: [ltv]                 # properties withheld from every read of that type
      rows: "object.deletedAt == null"   # and the rows it will show · §5, narrowed
```

### 6.1 `governance`

**No policy names a principal, and that is the design rather than a stage of it.** The obvious
shape — *this caller sees these rows* — was rejected because `loom query` and `loom run` have no
transport, so nothing can ever attest an identity to them, and a spawned stdio server carries no
bearer token either. A grammar expressible only against an authenticated caller would make the
*direct* half of §7's second invariant ungovernable by construction, and would leave governance
existing only over HTTP — which is precisely the transport-dependent surface §7 says the tool set
is not. So a policy is **deployment-scoped**: one `loom.yaml` filters one way, for every caller of
it, and two audiences are served by two deployments. `mcp.actor` gains no second reader; it stays a
string about a deployment that reaches the edit log and nothing else, so nothing here is shaped
around a value that a per-call principal will replace.

`when:` is the clause an attested caller turns on, and it is the **only** key still named and
refused, because a config that is silently ignored reads, to whoever wrote it, exactly like one that
was obeyed. `audit:` was the other one and is gone from the grammar rather than still sitting in
it — see "`edit_log` is a posture, not a policy" below.

**Two rules decide the rest.**

*The schema is public; the data is not.* A **mask announces itself** — in `masked` on every read
tool's result, and in the tool description — because the property names are already in the spec and
in the JSON Schema, so saying "withheld" tells a caller nothing new. A **row predicate does not**,
because the rows *are* the data: a withheld row is simply **absent**, `get_` answers `found: false`
in the same words it uses for a key that never existed, and no tool description gains a sentence.
The same rule settles two questions that look unrelated. **Filtering on a masked property is
refused, not answered emptily**, since an empty result is an oracle (a substring filter on a
withheld column binary-searches its value) while a refusal only repeats what the mask already said;
a masked property also leaves the `filter` schema, for the reason `loom serve` refuses to start
rather than advertise a tool that fails on every call. And **a row predicate that cannot be
evaluated over a row cannot report it** — per row there is no channel, and per call, "this row
exists but I could not decide about it" is the existence oracle again. So it does not admit, which
is the whole of the null question below.

*Policies subtract, never add.* None can grant, and none can widen what the config already permits —
which is why `writes` above stays a switch of its own rather than being subsumed. Masks union, so
declaration order cannot matter.

**Four things a mask cannot withhold**, each refused where the spec and the deployment are paired
(`build_resolver` and `build_runtime`, the same place §6's engine negotiation happens, so
`loom query`, `loom run` and `loom serve` refuse identically):

- a property or objectType the spec does not declare — a policy protecting a misspelling protects
  nothing and looks exactly like one that works;
- a **primary key**: every surface addresses a row by it, so withholding it withholds the object
  rather than a property. Not declaring the object type is the honest spelling of that intention;
- a property a **link** joins on, whose value is the link's whole meaning;
- a property an **action** reads in a rule or writes in an effect. This one is about a *combination*
  — the spec is fine and the policy is fine — and it is what keeps §9.2's guarantee true: a rule
  reading a withheld property is an oracle the caller drives, and an effect writing one changes data
  the deployment says the caller may not see.

**None of the last three carries over to `rows:`**, and that is a consequence rather than an
oversight: each of them is a surface still trying to *use* a value it may not read. A predicate uses
the value and shows nobody, so it may filter on a primary key, on a link's join property, or on a
property an action reads. Only the first refusal survives — a predicate naming a property the spec
does not declare is the same invisible typo a mask is.

**Where it is enforced: the projection, on both planes.** A masked property is never *selected*, so
it is not in the result set for any layer above to forget to drop, and `_loom_meta.edits` inherits
the mask because `before`/`after` are built from the same projection. The physical row is untouched:
a masked column is **carried across** a `modify` exactly as an unmapped one is (§4.1) — withheld
from the account of the write, never from the write, or the policy would destroy the data it exists
to protect.

**`rows:` is §5, narrowed to what two evaluators can be made to agree on.** It is the same
expression language a validation rule is written in — one grammar, one parser, `object.<prop>` for
a property, and no parameters because a policy has none. What it may *contain* is smaller:
comparisons between the row's own properties and literals, composed with `&&`, `||` and `!`, and
nothing else. Arithmetic, `lower()`, `len()` and `coalesce()` are **refused at load, naming the
node**, on one rule — *a predicate is lowerable when Loom, not the engine, decides what every
operator means.* `now()` is refused with them, for the one reason that is not about engines: it
puts a clock inside a filter, and *which instant, the read's or the run's* is a decision worth
writing down rather than inheriting. The subset may only ever widen, which accepts predicates that
used to be refused and cannot change one already written.

It has to mean the same thing twice, because the two planes read differently: the read path
**compiles it into the query** (it must filter before `LIMIT`/`OFFSET`, or `hasMore` lies), and the
write path **evaluates it in process over one row**, because the action runtime reads through the
catalog rather than the resolver and an agent that cannot see a row must not be able to act on it.

*Null: three answers, one admission rule.* A predicate is true, false, or **undecided**, and a row
is admitted **only on true**. `==` and `!=` never return undecided — §5's "null is a value" is kept
exactly, which is what makes `object.deletedAt == null` expressible, and it is carried into SQL as
`IS NOT DISTINCT FROM` rather than `=`. Ordering a null is undecided rather than an error, and
`!`, `&&`, `||` propagate it by the rules SQL's own `NOT`/`AND`/`OR` already follow — so the two
lowerings agree by construction, and negation stays fail-closed. The alternative that looks
simpler — make every leaf definitely true or false on both planes — fails open: `!(object.ltv >
100)` becomes `!false` for a null `ltv` and *admits* the row a policy was written to exclude.

*Applied to every governed table a read touches*, so a link is not the way around a filter: you
cannot traverse *from* a customer this deployment does not show you, and you cannot land on one.
And *the write path gates the row it read, never the row it writes* — a `modify` may move a row out
of the predicate, which is what a soft delete is. One consequence is deliberate and worth naming: a
`create` still reports `object_exists` for a key held by a row the policy excludes, because that
check has to be physical or two creates manufacture a duplicate primary key nothing can repair. On
that one path a predicate hides rows and not keys, and what it discloses is confined to *something
exists under the key you supplied*.

**`edit_log` is a posture, not a policy — which is why it sits beside `policies:` rather than in
it.** `audit:` was reserved inside the policy grammar for two slices, and it named two clauses that
turned out to belong in different places, so the key left rather than landing.

*"No log, no write"* subtracts an ability, which is the shape a policy has — but it names no
objectType, because unloggability is a fact about a **catalog**: the log is one table per catalog
(§9.2), reached through a port a catalog either implements or does not. A per-type spelling would
let a config say "Customer edits must be logged, Order edits need not" about a single fact
concerning a single catalog. It is also a switch an operator reads, which is exactly why `writes`
below is not a policy either.

**What it promises is about a deployment, never about a run**, and the name says so. There is no
transaction spanning a row's table and `_loom_meta.edits`, so *every applied run is logged* is not
available at any price. `edit_log: required` says the one thing that is true — **a deployment that
cannot record what it writes does not start** — and it is checked where the spec and the deployment
are paired, in `build_runtime`. It is the one governance key that binds a single plane: the read
path writes no rows, so it produces no records, so `build_resolver` has nothing to check.

Two kinds of unloggability, both knowable before any row is written, which is what makes this a
startup question:

- **structural** — the catalog implements no edit-log port, so every run against it writes its row
  and reports `log_failed` afterwards, for as long as the deployment lives;
- **physical** — the `_loom_meta` namespace or the table cannot be created. Provable only by doing
  it, so the check **creates the table** rather than probing for one. `table_exists` asks the wrong
  question (`false` is the ordinary state of a catalog whose first append has not happened), and
  creating a table records nothing that might not have happened — an empty log is a permission, not
  an intention, so this does not reopen §9.2's ordering.

A **per-write probe** was rejected, and not only because it narrows the window rather than closing
it: it is nearly blind. The log lives in the *same catalog* as the row it describes, so a catalog
nobody can reach already fails the row write, with nothing written and nothing to record. The
failures worth catching are specific to the log table, and the only probe that sees those is an
append — which is log-then-write, a table of intentions that may never have happened. So the whole
of this posture is spent at startup, and no round trip is added to the path of every action.

**Nothing after the write changes under either posture.** An append that fails once the row has
committed still comes back as `log_failed` beside an unchanged status, because *the row committed,
so `failed` would tell a caller to retry a delete that already happened* is not an argument a
config weakens. What survives that window is what always survived it: the commit carries
`loom.edit_id`, so a lost record is a stamped snapshot with no matching row — a gap a reader can
find.

**Default `optional`**, for the reason `writes` is off by default: an upgrade and a catalog with no
edit-log port are two things that happen for unrelated reasons, and neither is a deployment asking
to stop working.

*Retention is not here, and no key is coming for it* — see §9.2 and "Open edges".

**`engine.type` is negotiated against the ontology, not just resolved.** An adapter reports what it
can do, and a spec implies what will be asked of it: declaring a link means a traverse joins two
backing tables, declaring a *string* property `searchable` means a filter is a case-insensitive
substring match (an enum is a closed set and matches exactly, so it implies nothing), and the page
arguments §7 puts on every read tool mean a second page is an `OFFSET` — that last one required by
the surface rather than by anything a spec says. An engine that cannot do one of them is **refused
where the two are wired together**, so `loom query` and `loom serve` refuse identically, and the
message names the declaration to go and look at. The surface is never narrowed to fit: §7's tool set
is a function of the spec, and matching exactly where this section promised substring would return
rows rather than fail, which is the one failure an agent cannot see.

The four address keys mean nothing without an address, so setting any of them under
`transport: stdio` is an **error** rather than something quietly dropped — the same rule
`governance.policies` follows, for the same reason: a config that is silently ignored reads, to
whoever wrote it, exactly like one that was obeyed.

**`writes` is off by default, and that is a decision.** Until the action runtime became a tool set,
`loom serve` could not change anything, and deployments were pointed at real lakes on that basis.
Defaulting it on would mean an upgrade plus a spec that declares an action — two things that happen
for unrelated reasons — silently making a production lake mutable by any MCP client. So a deployment
says so, in the file a deployment is configured by. It is deliberately not a CLI flag (a flag lets an
invocation contradict the file an operator reviews) and deliberately not a governance policy: it
names no principal and filters no row. It is a switch on a whole surface, which §6 owns and §7
generates against — though M5 may end up subsuming it.

**`actor` is what a served run is recorded as** in `_loom_meta.edits`. Unset, runs record `unknown`.
See §4.1's "the actor is supplied by the caller, never invented" for why this is a config key rather
than an inference or a tool parameter.

**The transport has an address now, and the address is the posture.** `stdio` has none: a client
spawns the process and owns it. `http` is reachable by whoever can reach the port, and that single
difference decides everything else in this block.

*All of it is config, including the port.* The argument that put `writes` here — a flag lets one
invocation contradict the file an operator reviews — is weakest for a port number, which is not a
posture at all. It goes here anyway, because a file that describes half an address does not describe
the server, and reviewing the deployment would mean reading the unit file too. The host is the
strongest case rather than the weakest: it *is* the posture, and it is precisely the question this
file should answer.

*`host` defaults to `127.0.0.1`*, for the reason `loom apply` refuses to run unattended: do not put
somebody's lake on a network because nobody said to. `loom serve` speaks cleartext — there is no TLS
key, and terminating TLS belongs to whatever sits in front — which is a second reason the default
bind is local.

*`writes: true` is refused on a non-loopback bind*, at startup, the way `cmd_serve` already refuses
rather than advertising tools that will fail. **Writes over a socket are not the same decision as
writes over a pipe**, and the difference is reachability rather than transport. It is worth being
exact about why, because the obvious reason is wrong: `actor` lives in *this file*, so it always
named a deployment rather than a session — three stdio clients reading one `loom.yaml` already
record one string for three callers. Many callers under one name is not what a socket introduces.
What a non-loopback bind changes is who is permitted to *be* one of those callers: over stdio,
whoever can run the binary and read this file; over `0.0.0.0`, whoever can reach the port. That is
where `actor:` stops being a statement anybody checked. The check is honest about its own limit —
it constrains what Loom **binds**, not what **reaches** it, and a proxy in front of a loopback bind
is outside anything this file can see.

*`allowed_hosts`* is the `Host` allow-list behind DNS-rebinding protection, which is always on. It
is optional exactly where it can be derived: a loopback bind knows the three names it answers to. A
non-loopback bind does not know the name the world reaches it by, so it is required there rather
than guessed.

---

## 7. What the grammar compiles to (deterministic)

So the contract is complete on both ends, here's the fixed mapping from spec → generated MCP
surface. Nothing here is hand-authored.

| Spec element                         | Generated MCP tool(s)                                   | Input schema source |
|--------------------------------------|---------------------------------------------------------|---------------------|
| `objectType Customer`                | `get_customer(key)`                                     | PK type |
|                                      | `search_customer(filter, page)`                         | `searchable` props + property types |
|                                      | `list_customer(page)`                                   | pagination |
| `linkType placedBy` (+ `reverseName`)| contributes `order` / `customer` directions to `traverse(object, link, direction)` | link mapping |
| `action upgradeTier`                 | `run_upgrade_tier(parameters, dryRun)`                  | `parameters` → JSON Schema; `description` → tool description |

Tool names are the api name in `snake_case`, for every row of that table. This one used to read
`run_upgradeTier`, which no other row's spelling would have produced — a slip, corrected, in the
same class as the two §4/§5 bugs the action runtime turned up.

**One tool per action, not one `run(action, params)`.** `traverse` is generic and an action is not,
and the rule that decides both is about the *schema* rather than the name: a generic tool is right
exactly when the varying element does not change the input schema. Every link takes the same
`(objectType, key, link, page)`; every action takes something different — an objectRef and a
two-value enum here, a string and a `decimal(12,2)` there. Collapsing them means typing `params` as
a free-form object, which would be the one place in this table where an agent is handed an untyped
bag and "declared types are honored on the way in" stops being structural. The cost is stated rather
than hidden: a spec with forty actions generates forty tools.

**Two argument namespaces, which never mix.** Names drawn from the spec's vocabulary go inside a
nested object — `search_`'s declared property filters under `filter`, `run_`'s declared parameters
under `parameters`. Names Loom chose stay at the top: `key`, `limit`, `offset`, `objectType`,
`link`, `dryRun`. That is what makes `dryRun` addable at all, because an ontology may declare a
parameter called `dryRun` and it can no more be shadowed than a property called `limit` can.

**`dryRun` is an inspection verb, not an approval step.** It runs bind → read → validate and stops
before the write, returning `previewed` — the same thing `loom run` prints above its `y/N`, and the
only way an agent can learn what an action would do other than by doing it. It confers nothing on
the run after it: no state is carried, no row is held, and the next run does its own read and
asserts that one (§4.1). A design where a preview reserved anything for a later call is the one
§4.1 rejected when it put the prompt outside the window.

**Which actions become tools, and whether any do.** All of them, whatever their `status` — a
non-`active` element is labelled in its description (`DEPRECATED — …`) rather than hidden, because
hiding it would leave `loom run` able to run something the tool surface denies, and the runtime is
deliberately one entry point for both. Whether the write half is generated at all is a *deployment*
decision, not a spec one: `mcp.writes` (§6) is off by default, so declaring an action does not by
itself make a lake mutable by an MCP client.

**What a run returns.** `ActionResult` (§4.1) serialized — status, key, before/after, snapshot,
attempts, `editId`, and typed `failures[]`. Never a protocol-level error: the transport's error flag
answers *did this call become a run*, not *did the run succeed*, so a refused precondition, a
conflict and a write failure all arrive as content an agent branches on. That is also the only
encoding that can describe `applied` with a `log_failed` beside it, which a boolean gets backwards.

**Nothing in this table differs by transport, and that is stated rather than implied.** The surface
is a function of the spec; `mcp.transport` is not one of its inputs. Both transports are handed the
same assembled server, so there is no seam here for one of them to widen — a tool that exists over
HTTP exists over stdio, with the same name, the same input schema and the same description.

The corollary matters more than the claim, because **a transport with real status codes invites
re-litigating `isError`**. It does not get to. An HTTP status answers *did this exchange happen*;
`isError` answers *did this call become a run*. They are questions at different layers and never two
votes on one thing, so **an HTTP status never disagrees with `isError`**: every tool outcome —
`applied`, `previewed`, `refused`, `failed`, an unknown link, an unknown tool — is a `200` carrying
content. A non-`200` is only ever about the exchange (a rejected `Host`, a rejected `Origin`, a
malformed body, an unknown session), and no tool result can produce one. The alternative maps a
refusal onto a 4xx, where an agent's transport raises before its own branch on `status` ever runs,
and a validation rule doing its job arrives looking like a broken client.

Two invariants the compiler guarantees: **the LLM never receives raw SQL** (only these
verbs), and **governance policies filter both the direct API and the MCP tools identically**
(enforced in the resolver, below the MCP layer — and on the write plane in the action runtime's own
projection, since `dryRun` would otherwise read out of `before` what a policy withheld from a read).

The first invariant is a claim about the **tool set**, and §6.1's policies are the one thing that
changes what a tool *returns* without changing what it is. The line between those is worth stating
where both are: the set of tools, their names and their argument namespaces are a function of the
spec; a policy can **subtract** from what one advertises and returns — a withheld property leaves
the projection, the `filter` schema and the result — and can never add to any of them. That is the
same direction the engine may not push in: §6 refuses a spec an engine cannot serve rather than
narrowing the surface to fit, because an engine is an implementation detail, while a policy is the
deployment's declared intent about its own data.

---

## 8. Worked example — a complete, valid mini-ontology

```yaml
# ontology/customer.yaml
objectType:
  apiName: Customer
  primaryKey: customerId
  title: name
  backing: { catalog: rest_main, table: crm.customers }
  properties:
    - { name: customerId, type: string, column: id, unique: true }
    - { name: name,       type: string, column: full_name }
    - { name: tier,       type: enum, values: [bronze, silver, gold], column: tier }
    - { name: ltv,        type: double, column: lifetime_value, nullable: true }
  searchable: [name, tier]
```
```yaml
# ontology/order.yaml
objectType:
  apiName: Order
  primaryKey: orderId
  title: orderId
  backing: { catalog: rest_main, table: sales.orders }
  properties:
    - { name: orderId,    type: string, column: id, unique: true }
    - { name: customerId, type: string, column: customer_id }
    - { name: total,      type: decimal, precision: 12, scale: 2, column: total_amount }
    - { name: placedAt,   type: timestamp, column: created_at }
  searchable: [orderId]
```
```yaml
# ontology/links/placed-by.yaml
linkType:
  apiName: placedBy
  cardinality: many_to_one
  from: { objectType: Order,    property: customerId }
  to:   { objectType: Customer, property: customerId }
  reverseName: orders
```
```yaml
# ontology/actions/upgrade-tier.yaml
action:
  apiName: upgradeTier
  description: Raise a customer to a higher membership tier
  targetObjectType: Customer
  operation: modify
  parameters:
    - { name: customer, type: objectRef, objectType: Customer }
    - { name: newTier,  type: enum, values: [silver, gold] }
  validation:
    - { rule: "newTier != object.tier", message: New tier must differ from current tier }
  effects:
    - modifyObject:
        key: "{{ customer }}"
        set: { tier: "{{ newTier }}" }
```

This validates clean, migrates two Iceberg tables, resolves `Customer.orders` as a reverse
JOIN, and exposes `get_customer`, `search_customer`, `list_customer`, `get_order`,
`search_order`, `list_order`, `traverse` and — where the deployment sets `mcp.writes` —
`run_upgrade_tier` over MCP, all from ~40 lines of YAML.

---

## 9. `_loom_meta` — what Loom recorded

Part of the contract because these are tables in *your* lake, not implementation details: anything
with an Iceberg client can read them, and a later Loom must keep them readable.

The namespace holds two tables, one per catalog, and neither is ever named by a spec:

| table | records | created by |
|---|---|---|
| `_loom_meta.applied` | what `apply` did to **schemas** | the first `loom apply` touching that catalog |
| `_loom_meta.edits` (§9.2) | what an action did to **rows** | the first action run against that catalog |

Neither is a planner input and neither can be planned *against*: `plan` only ever visits the tables
the spec declares, so it proposes nothing for either of these and reports neither as unmanaged.

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

### 9.1 `rollback` — what this table is *for*

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

### 9.2 `edits` — what an action did

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

---

## Open edges (v0 → v1)

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
- **A per-caller identity over MCP** — still open, and the HTTP transport narrowed it rather than
  closing it. `run_<action>` records `mcp.actor`, which names a *deployment*; neither transport Loom
  speaks authenticates anybody, so there is no caller to name instead.

  What was learned in narrowing it is that there are **three** kinds of answer here and only one is
  worth having, which the earlier wording did not distinguish. An actor may be *declared* by an
  operator (`mcp.actor` — true about a deployment, silent about a caller); *inferred* by Loom
  (`default_actor()`'s OS user — rejected, because it looks like a principal and is not one); or
  **attested** by a transport that checked it — not declared and not inferred, and the only one of
  the three worth more than `unknown`. MCP's authorization is an OAuth 2.1 resource-server profile,
  so attested means a bearer token validated on issuer, audience, expiry and signature against an
  authorization server. Anything short of that — reading a header, trusting a claim — is the
  client-supplied actor rejected above, wearing a hat.

  What is still missing is therefore specific: the validation, and the config to describe an
  authorization server. Nothing else has to move. `ActionRuntime.run` already takes the argument per
  call, the serve boundary already fills it in, and §6 already refuses the case where the gap would
  do damage — writes on a bind reachable by strangers. A loopback HTTP server may write today
  because its callers are the same set stdio's were; a public one may not, until this closes.

  Governance (§6.1) turned out **not** to be the other half of this, and the correction is worth
  keeping because it was written the other way round here first. The prediction was that policies
  filter by principal, so the identity would have to reach the resolver. What actually landed
  filters by *deployment*, for a reason that is structural rather than a matter of ordering:
  `loom query` and `loom run` have no transport at all, so an identity can never be attested to
  them, and building governance on one would have left the direct half of §7's second invariant
  ungovernable. So the resolver still receives no identity, deliberately and now permanently for
  everything §6.1 can express — and `when:` is reserved for exactly the policies that this edge
  unblocks, refused until then rather than approximated against `mcp.actor`.

  What that leaves for this edge to carry is narrower than it looks: the validation, the config for
  an authorization server, and — because a principal would then vary per call — the two things that
  are one-per-process today. `build_server` builds one `Resolver` and one `ActionRuntime`, and
  `DuckDBEngine` holds one connection registering every scan under the global aliases `t0`/`t1`/`m0`
  (which is also what serializes a served process, §7). Both are the same milestone's work, and
  neither was M5's.
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
