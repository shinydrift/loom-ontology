"""Engine adapters. One module per compute engine; selected by `engine.type` in loom.yaml."""

from __future__ import annotations

from ...config import EngineConfig
from ..engine import Engine, EngineError


def open_engine(config: EngineConfig, catalogs) -> Engine:
    """Construct the engine named in the project config, bound to the open catalogs."""
    if config.type == "duckdb":
        from .duckdb import DuckDBEngine

        return DuckDBEngine(catalogs=catalogs, options=config.options)
    raise EngineError(f"no engine implementation for type '{config.type}'")
