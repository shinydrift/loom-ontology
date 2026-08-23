# An app on top of it

The tool surface from the [quickstart](./quickstart.md) is not only for agents. This is the same
ontology with a UI in front of it, and no privileged path underneath.

`examples/retail/dashboard/` is the same ontology with a UI in front of it — a "Retail Ops"
dashboard whose entire data plane is the tool surface from the [quickstart](./quickstart.md).

```bash
python examples/retail/seed.py           # once
python examples/retail/dashboard/app.py  # → http://127.0.0.1:8080
```

No build step, no `npm install`, no CDN: one Python file, one HTML file, hand-rolled SVG charts.
It is **a second deployment of the same spec** — `dashboard/loom.yaml` serves `examples/retail/ontology`
over a socket with `writes: true`, and no spec was edited to get there.

The point is not that Loom can draw a chart. It is that the dashboard has **no privileged access**,
and the constraint is structural rather than aspirational: the browser-facing data plane is a single
passthrough route, so there is nowhere to put a filter the ontology never declared or a join the spec
never linked. A rail down the right-hand side shows every `get_` / `search_` / `traverse` / `run_`
call as it happens, with its arguments and response — which is exactly what an agent would have
issued. The date-range picker *is* a typed `{gte, lt}` filter; the customer drill-down is a
`traverse` over a declared link; the action panel is where `applied` / `previewed` / `refused` /
`failed` stops being a table in [these docs](./actions.md) and becomes a thing on screen.

Then uncomment `governance.policies` in `dashboard/loom.yaml`, restart, and reload — with **no
dashboard change** either time, because both policies apply below the tool layer:

- `hide-ltv` — the LTV tile reads *withheld by policy* and the column leaves the table. The mask is
  applied to the projection, so the column is not in the SQL; every envelope carries the `masked`
  list the UI renders.
- `current-customers-only` — the one `closed` customer leaves the table, their bar in the tier chart
  drops to zero, and the traverse to them comes back empty. `get_` on their key answers
  `found: false`: a withheld row is indistinguishable from an absent one.

One thing it cannot be is a page talking to Loom directly. `serve_http` sets `allowed_origins=[]`
("no browser is a legitimate client of this endpoint"), so `app.py` holds the MCP session and the
page talks to `app.py`. See [`examples/README.md`](../../examples/README.md).

---

That is the end of the tour. The grammar behind all of it is [`spec-v0.md`](../spec-v0.md).
