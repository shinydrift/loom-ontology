[← Spec index](../spec-v0.md)

# 4. `action`

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

## 4.1 What running an action actually does

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

**An `objectRef` is a key, and only one of them per run is resolved.** The runtime reads the row an
effect's `key` addresses — that is where `object_not_found` comes from — and every *other* `objectRef`
parameter is bound, type-checked against the referenced type's primary key, and written as the value
it is. `recordOrder(customer: "c999")` creates an Order whose `placedBy` traverses to nothing, and a
`create` has no target row at all, so **no** `objectRef` on a create is ever resolved.

Not an oversight, and not a gap the runtime should close on its own. A reference check could not be
carried into the write's own commit the way the snapshot assertion is — the referenced row can be
deleted between the check and the commit — so it would *narrow* the window rather than close it,
which is the thing the concurrency paragraph below refuses to let the word "optimistic" cover.
Referential integrity is a lake-wide property and Loom does not own the tables; §3 compiles a link
to a JOIN and promises nothing about a key on either side of it.

Two consequences worth stating rather than discovering. The generated tool description says which
kind of `objectRef` a parameter is, because "key of a Customer" read as a promise it was not. And
`governance.policies` — whose `rows:` half is stated as *an agent cannot act on a row it cannot
see* — enforces that where the ref is the target and not where it is a referencing parameter: a
caller who cannot read a withheld Customer can still create an Order naming one. Closing that means
deciding what a reference *is*, and it is on the backlog rather than half-answered here.

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

[← §3 `linkType`](./03-link-type.md) · [§5 Expression mini-language →](./05-expressions.md)
