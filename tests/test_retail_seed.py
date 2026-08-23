"""The shipped example's three stages, asserted in the order they happen.

The example used to build `crm.customers` in one `pa.schema(...)` — four declared columns and two
undeclared ones, born together. Which made `region` and `segments` read like a Loom decision, when
their whole job is to demonstrate §2 rule 7: *a column no property maps is somebody else's data*.

So the interesting assertions here are about **order**. `loom apply` creates exactly what the
ontology declares and cannot create more. The declared loads fill exactly what they declare and
cannot fill more. And only then does something that is not Loom add two columns of its own — after
which every Loom write has to leave them alone, which is the rule stated as a sequence of events
rather than as a sentence in a docstring.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

from loom import build
from loom.catalog import LOAD_LOG_TABLE, SEQUENCE_LOG_TABLE, open_catalogs
from loom.config import find_config, load_config
from loom.errors import Diagnostics

pytest.importorskip("pyiceberg", reason="needs the [iceberg] extra")

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "retail"

DECLARED = ["id", "full_name", "tier", "lifetime_value"]
UNMANAGED = ["region", "segments"]


@pytest.fixture
def example(tmp_path):
    """A copy of the example and its seed module, not yet run."""
    target = tmp_path / "retail"
    shutil.copytree(EXAMPLE, target, ignore=shutil.ignore_patterns(".warehouse"))
    spec = importlib.util.spec_from_file_location("staged_seed", target / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    diag = Diagnostics()
    config = load_config(find_config(target / "ontology"), diag)
    ontology, _ = build(target / "ontology")
    diag.raise_if_errors()
    (Path(config.catalogs["local"].warehouse.removeprefix("file://"))).mkdir(parents=True, exist_ok=True)
    return target, module, ontology, config


def catalog(config):
    return open_catalogs(config)["local"]


# ---- stage 1 ---------------------------------------------------------------------


def test_apply_creates_exactly_the_columns_the_ontology_declares(example):
    """Which is why stage 3 has to exist at all: an all-Loom bootstrap structurally *cannot*
    produce a table with columns Loom does not manage."""
    _, seed, ontology, config = example
    seed.bootstrap(ontology, open_catalogs(config))

    columns = list(catalog(config).describe("crm.customers").columns)
    assert columns == DECLARED
    assert not set(UNMANAGED) & set(columns)


def test_apply_creates_every_table_the_spec_backs(example):
    _, seed, ontology, config = example
    seed.bootstrap(ontology, open_catalogs(config))

    cat = catalog(config)
    for table in ("crm.customers", "sales.orders", "sales.daily_sales_performance"):
        assert cat.table_exists(table)


# ---- stage 2 ---------------------------------------------------------------------


def test_the_rows_arrive_through_the_declared_sequence(example):
    """`table.append(rows)` became `loom sequence seed`, and the difference is the record."""
    _, seed, ontology, config = example
    seed.bootstrap(ontology, open_catalogs(config))
    seed.load(ontology, config, open_catalogs(config))

    cat = catalog(config)
    assert len(cat.scan("crm.customers").to_pylist()) == 4
    assert len(cat.scan("sales.orders").to_pylist()) == 6

    loads = cat.scan(LOAD_LOG_TABLE).to_pylist()
    assert {r["entry"] for r in loads} == {"customers", "orders"}
    assert {r["actor"] for r in loads} == {"seed.py"}
    (run,) = cat.scan(SEQUENCE_LOG_TABLE).to_pylist()
    assert run["sequence"] == "seed" and run["landed"] == 2


def test_the_loaded_rows_are_only_the_declared_properties(example):
    """A source column no property claims is refused at load time, so the seed's own files cannot
    carry the two unmanaged columns — they are still absent at the end of stage 2."""
    _, seed, ontology, config = example
    seed.bootstrap(ontology, open_catalogs(config))
    seed.load(ontology, config, open_catalogs(config))

    assert list(catalog(config).describe("crm.customers").columns) == DECLARED


def test_loading_the_same_drop_twice_is_refused(example):
    """A load's id is derived from the file's bytes, and the example's files are checked in — so
    the second run of the seed sequence is one load happening twice, and is told so."""
    _, seed, ontology, config = example
    seed.bootstrap(ontology, open_catalogs(config))
    seed.load(ontology, config, open_catalogs(config))

    with pytest.raises(RuntimeError, match="derived from"):
        seed.load(ontology, config, open_catalogs(config))


# ---- stage 3, and what it demonstrates -------------------------------------------


def test_the_unmanaged_columns_arrive_after_the_load_from_something_that_is_not_loom(example):
    _, seed, ontology, config = example
    seed.bootstrap(ontology, open_catalogs(config))
    seed.load(ontology, config, open_catalogs(config))
    assert list(catalog(config).describe("crm.customers").columns) == DECLARED

    seed.arrive(seed.open_sql_catalog(config))

    columns = list(catalog(config).describe("crm.customers").columns)
    assert columns == DECLARED + UNMANAGED
    rows = {r["id"]: r for r in catalog(config).scan("crm.customers").to_pylist()}
    assert rows["c1"]["region"] == "emea"
    assert rows["c1"]["segments"] == ["enterprise", "early-adopter"]
    assert rows["c3"]["segments"] is None


def test_plan_reports_them_as_unmanaged_and_proposes_nothing(example):
    """The rule, at the layer that owns schemas: reported, and never dropped."""
    from loom.migrate import diff_ontology

    _, seed, ontology, config = example
    seed.seed(Path(config.source).parent)

    diag = Diagnostics()
    plan = diff_ontology(ontology, open_catalogs(config), diag)
    diag.raise_if_errors()

    unmanaged = {u.table: set(u.columns) for u in plan.unmanaged}
    assert unmanaged["crm.customers"] == set(UNMANAGED)
    assert plan.is_empty


def test_an_action_carries_them_across_untouched(example):
    """And the rule at the layer that owns rows. A modify is an equality-delete plus an append, so
    a column it did not carry it would silently null — including the one whose type the spec has no
    name for, which is carried without ever being examined."""
    from loom.action import build_runtime

    target, seed, ontology, config = example
    seed.seed(target)

    before = {r["id"]: r for r in catalog(config).scan("crm.customers").to_pylist()}
    build_runtime(ontology, config).run("upgradeTier", {"customer": "c3", "newTier": "gold"})
    after = {r["id"]: r for r in catalog(config).scan("crm.customers").to_pylist()}

    assert after["c3"]["tier"] == "gold"
    assert after["c3"]["region"] == before["c3"]["region"] == "apac"
    assert after["c3"]["segments"] == before["c3"]["segments"] is None
    assert after["c1"] == before["c1"]


def test_the_whole_seed_leaves_the_spec_physically_valid(example):
    """The example's own promise, and the one a restaging is most likely to break."""
    from loom.loader import load_dir
    from loom.validator import check_physical, validate

    target, seed, _, config = example
    seed.seed(target)

    diag = Diagnostics()
    loaded = load_dir(target / "ontology", diag)
    validate(loaded, diag)
    diag.raise_if_errors()
    check_physical(loaded, open_catalogs(config), diag)
    diag.raise_if_errors()
