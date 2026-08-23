# Examples

One worked example, `retail/`, in two parts.

| | What it is |
|---|---|
| [`retail/`](retail/) | The ontology itself — three object types, one link, three actions — plus a seed script that builds a real local Iceberg warehouse to run it against. This is what the root README's walkthrough uses. |
| [`retail/dashboard/`](retail/dashboard/) | An app on top of it. A dashboard whose entire data plane is Loom's MCP tool surface, and which shows you every call it makes. |

---

## 1. The ontology — `retail/`

```bash
pip install -e ".[all]"
python examples/retail/seed.py
```

That creates `examples/retail/.warehouse` — a pyiceberg SQL catalog over SQLite with table data on
local disk, so a real Iceberg lake needs nothing installed and nothing running.

`seed.py` runs in **three stages, and only the last one is outside Loom**:

1. **`bootstrap`** — `loom apply`, from nothing but the spec. Every column the ontology declares,
   created by Loom's own migration engine, and not one more.
2. **`load`** — `loom sequence seed`, which runs the declared `customers` and `orders` loads from
   `data/manifest.yaml`. Checked against the declared types, one commit each, and a row apiece in
   `_loom_meta.loads`.
3. **`arrive`** — pyiceberg directly, adding `region` and a `list<string>` Loom has no type name for
   to `crm.customers`, and filling them.

Stage 3 is the demonstration, and the **order** is what makes it one. Those two columns used to be
born in the same `pa.schema(...)` as the declared ones, which made them read like a Loom decision —
when the whole reason they exist is that they are not. §2 rule 7 says a column no property maps is
somebody else's data: reported by `plan`, never dropped, carried across untouched by every write. A
column that arrives *afterwards*, from a writer that is not Loom, is what that rule is actually
about. And an all-Loom bootstrap could not produce one if it tried: `apply` creates what the spec
declares, and a load is refused if its file carries a column no property claims.

Then:

```bash
loom validate --physical examples/retail/ontology
loom query Customer examples/retail/ontology --key c1
loom query DailySalesPerformance examples/retail/ontology --filter salesDate.gte=2026-02-01
loom run upgradeTier examples/retail/ontology --param customer=c3 --param newTier=gold
loom serve examples/retail/ontology
```

`examples/retail/loom.yaml` is the deployment: stdio, read-only, governance commented out. It is
heavily annotated — most of the file is prose explaining what each key would do and why it is off.

### What's in the spec

- **`Customer`** · `customerId` / `name` / `tier` (enum) / `ltv` (nullable) — searchable by name and tier
- **`Order`** · `orderId` / `customerId` / `total` (decimal) / `placedAt`
- **`DailySalesPerformance`** · a precomputed daily rollup with refresh and source provenance, keyed by date
- **`placedBy`** · `Order → Customer`, many-to-one, with the reverse hop named `orders`
- **`upgradeTier`** (modify, with a validation rule) · **`recordOrder`** (create) · **`forgetCustomer`** (delete)

---

## 2. The app — `retail/dashboard/`

```bash
python examples/retail/seed.py          # once, if you haven't
python examples/retail/dashboard/app.py # then open http://127.0.0.1:8080
```

No build step, no `npm install`, no CDN. One Python file, one HTML file, hand-rolled SVG charts.

### What it is for

The point is not that Loom can draw a chart. It is that **the dashboard has no privileged access.**
Every number on the page arrives through `get_` / `search_` / `list_` / `traverse` / `run_` — the
same thirteen tools an agent gets — and the rail down the right-hand side shows you each call as it
happens, with its arguments and its response.

That constraint is enforced by shape rather than by discipline. The browser-facing data plane is a
single route:

```
POST /api/call  {"name": "search_customer", "arguments": {...}}   →   session.call_tool(...)
```

There is no per-panel endpoint, so there is nowhere to add a filter the ontology never declared, a
join the spec never linked, or a column a policy withholds.

### Why there is a Python process in the middle

Because a browser is not allowed to be an MCP client here, and that is Loom's decision rather than a
limitation. `serve_http` leaves DNS-rebinding protection on and sets `allowed_origins=[]`:

> no browser is a legitimate client of this endpoint, so any request that carries an `Origin` at all
> is one to refuse

So `app.py` holds the MCP session and the page talks to `app.py`. Which is the honest topology
anyway — an agent runtime holding a session with a UI in front of it is exactly what this models.

```
browser ──fetch──▶ app.py ──MCP over HTTP──▶ loom ──▶ DuckDB ──▶ Iceberg
                (one route)                (the only door)
```

By default `app.py` starts the Loom server in its own process. `--mcp-url` attaches it to one that
is already listening instead — any Loom HTTP server over this ontology will do, including another
`app.py`:

```bash
python examples/retail/dashboard/app.py                                        # serves MCP on :8765
python examples/retail/dashboard/app.py --mcp-url http://127.0.0.1:8765/mcp \
                                        --port 8081                            # client only
```

Two dashboards, one server, and the second cannot tell that it did not start it — which is the test
that it is a client rather than a wrapper. (For a `loom serve` instead, set `transport: http` in
`retail/loom.yaml`; `find_config` looks beside the ontology, so that is the file it will read.)

### It is a second deployment, not a second ontology

`retail/dashboard/loom.yaml` serves the identical `retail/ontology` over a socket with
`mcp.writes: true`. No spec was edited to get there. It is deliberately *not* discovered by
`find_config` — `app.py` names it explicitly — so `loom serve examples/retail/ontology` keeps
meaning exactly what the walkthrough above says it means.

### What each panel is showing you

| Panel | Tools | The thing worth noticing |
|---|---|---|
| KPI tiles | `search_daily_sales_performance` | figures, not charts — four numbers don't need four bars |
| Gross sales | `search_daily_sales_performance` | the date-range control **is** the filter: it becomes `{"salesDate": {"gte": …, "lt": …}}`, printed under the chart. A typed object filter, available because the spec marks `salesDate` searchable |
| Customers by tier | `list_customer`, `search_customer` | clicking a bar issues a real filtered search rather than filtering in the browser |
| Customer detail | `get_customer`, `traverse` | the orders arrive by following a *declared link*, not a join you wrote |
| Actions | `run_upgrade_tier`, `run_record_order`, `run_forget_customer` | the forms are generated from each tool's own input schema — the `newTier` dropdown holds `silver` and `gold` because the spec says so |
| Tool calls | — | the arguments and responses themselves |

### The four-way outcome

The action panel exists mostly to make this visible, because it is the part an agent has to branch
on and the part a boolean cannot carry:

| Status | What it means |
|---|---|
| `applied` | the row changed, and `_loom_meta.edits` has the record — the `editId` is on screen |
| `previewed` | `dryRun: true` — read and validated, then stopped. An inspection verb, not an approval: nothing is held, and a run after a preview reads again |
| `refused` | a validation rule the spec declares said no. **This is the precondition working.** Try upgrading a customer who is already gold |
| `failed` | the runtime decided to write and the write did not land; `failures[].retryable` says whether to try again |

None of these is `isError`. That flag answers *did this call become a run*, never *did it succeed* —
so a refusal comes back `isError: false` with a body saying why, and the caller has to read the body.
The dashboard does, and shows you both.

### The stale-aggregate banner

`run_record_order` writes to `sales.orders`. The chart reads `sales.daily_sales_performance`, which
is an **ingestion-time** aggregate — so record an order and the chart does not move.

The dashboard says so rather than hiding it, and the recompute button is deliberately *not* a tool:
it posts to `/api/refresh`, appears in the rail in a different colour labelled **not a tool**, and
runs `sales_performance.py` with pyiceberg. That is the tradeoff a precomputed rollup *is*, and a
tool-shaped button would be the dashboard telling a more comfortable story than the lake supports.

### Turning governance on — the two-line edit

This is the part worth doing by hand. Uncomment the policy block at the bottom of
`retail/dashboard/loom.yaml`:

```yaml
governance:
  policies:
    - name: hide-ltv
      objectType: Customer
      mask: [ltv]
```

Restart `app.py` and reload. The LTV tile now reads **withheld by policy**, the column leaves the
customers table, and `search_customer`'s own description says `Withheld by governance policy: ltv`.

**No dashboard code changed.** Every tool envelope carries a `masked` list and the UI renders it;
the property is absent from the SQL rather than dropped from the rows on the way back. `loom query`
against the same file withholds it too, because the mask is applied to the projection below both.

Add the second policy — `rows: "object.tier != 'closed'"` — and rows leave the chart, the table and
the traverse results at once, for the same reason.

The third policy in `retail/loom.yaml` (`gold-desk`, guarded by `when: principal.groups contains …`)
is the one this deployment *cannot* use. It names a caller, and naming one needs `mcp.auth`. Paste it
in and `app.py` refuses to start rather than applying the rest — a surface that cannot attest anybody
does not get to apply half a policy.

### Things it will not let you do

- **Write to a public bind.** `mcp.writes: true` is only legal on loopback; move `host` and the
  config refuses to load, because `mcp.actor` names a deployment and that is only honest while the
  callers are people who could already run the binary.
- **Migrate.** A serving process can change the rows its actions declare and no schema at all —
  nothing in this path can reach `CatalogWriter`.
- **Ask for an undeclared filter.** The tool's input schema is generated from `searchable` and the
  property types, and the resolver enforces the same list underneath it.
