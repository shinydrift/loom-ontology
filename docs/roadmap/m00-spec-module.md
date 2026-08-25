[← Roadmap index](../ROADMAP.md)

# ✅ Done — M0: Spec module (compiler front-end)

- [x] Canonical type system (`types.py`) — each type → `{Iceberg, JSON Schema}`, promotion rules
- [x] Expression mini-language (`expr.py`) — tokenizer + Pratt parser → AST
- [x] Typed Ontology Model (`model.py`)
- [x] Structural loader (`loader.py`) — one-kind-per-file, unknown-key errors, shape checks
- [x] Referential/semantic validator (`validator.py`) — accumulates all errors
- [x] `loom validate` CLI · 36 tests · CI

**Probed as a client (2026-08-25): the config file's own preferred location was fatal.**
`config.find_config` looks for `loom.yaml` *inside* the ontology directory before it looks beside
it, and documents that order. `load_dir` globbed every `*.yaml` under the same directory and
required each to declare one of the three spec kinds — so taking the first supported location made
every command exit 1 with `a spec file must declare exactly one of ('objectType', 'linkType',
'action'); found none`, an error about spec grammar naming a file that is not a spec, with nothing
to connect the two. `load_dir` now skips the one at the ontology root, and only that one: a nested
`loom.yaml` is config `find_config` would never read, so it keeps erroring rather than being
silently ignored.

---

[M1 →](./m01-read-slice.md) · [backlog](./backlog.md)
