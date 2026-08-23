[← Spec index](../spec-v0.md)

# 5. Expression mini-language

Deliberately tiny so it stays portable across engines and safe to evaluate. Used in
`validation[].rule` (must yield boolean), effect value positions (must yield the target property's
type), and a governance policy's `rows:` predicate and `when:` guard (§6.1, which narrows what each
may contain but not what any of it means). Written inline or inside `{{ … }}`.

- **References:** a bare identifier `paramName` resolves to a parameter; `object.propName` resolves
  to the *current* value of the target object's property (for `modify`/`delete`);
  `principal.claimName` resolves to a claim of the attested caller. One rule places all three: **a
  reference is legal where its declaration is in scope.** A parameter is declared by its action, a
  property by its object type, and a claim by `mcp.auth.claims` in a `loom.yaml` — so `principal.` is
  legal in a governance policy there and **refused in an ontology**, which is what keeps a spec
  deployment-blind.
- **Literals:** string `'...'`, number, `true`/`false`/`null`. An enum value is a string, so it is
  quoted: `'gold'`, never bare — a bare word is always a reference.
- **Operators:** comparison `== != < <= > >=`, boolean `&& || !`, arithmetic `+ - * /`,
  string `+` (concat), and `contains` (membership in a list). `contains` is the one operator no
  ontology can use today: its left operand is a list of strings, and no *property* type is a list —
  it exists for a list-valued claim, and becomes generally useful when `array` lands as a property
  type. It is a reserved word.
- **Function allow-list (only these):** `now()`, `lower(s)`, `upper(s)`, `len(s)`,
  `coalesce(a, b, …)`.
- **No** loops, lambdas, property assignment, external calls, or arbitrary code. Anything
  richer belongs in a future custom-function extension point, not the expression language.

## 5.1 `{{ … }}` is punctuation, not a second language

`key: "{{ customer }}"` and `rule: "newTier != object.tier"` are the **same grammar**. The braces
are optional and are stripped at load, so nothing downstream — evaluator, validator, engine — ever
sees one. Two things follow, and both are load-bearing:

- **An effect value may be any expression**, not only a parameter reference. That is what makes
  `placedAt: "now()"` and `tier: "upper(newTier)"` expressible. `{{ customer }}` is the degenerate
  case of the general thing, not a different thing.
- **There is no string interpolation.** `"tier-{{ newTier }}"` is a load error, not a template.
  Building a string is the expression language's own `+`.

## 5.2 What the evaluator does with values

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

A **caller's filter** is not written in this language — it is JSON, in the shape §7.1 gives it — but
it inherits these two answers rather than getting a third set. `{"eq": null}` is null-safe because
that is what `==` means here, and an ordering comparison over a null column returns no row because
§5 refuses to order a null. The one thing a filter adds is a refusal this language has no occasion
for: a **bare** `null` (§7.1), which is JSON's inability to distinguish an absent key from a null
one rather than anything about the value domain.

---

[← §4 `action`](./04-action.md) · [§6 Project config — `loom.yaml` →](./06-project-config.md)
