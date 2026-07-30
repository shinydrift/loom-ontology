"""The agent layer — layer 5. The Ontology Model, introspected into MCP tools.

Split in two on purpose: `registry` turns an ontology into plain `ToolSpec` values with no MCP
SDK involved, and `server` adapts those to a transport. That's what lets the load-bearing
guarantee — that the generated surface is typed verbs and never raw SQL — be asserted by a test
that imports no SDK and opens no socket.
"""

from __future__ import annotations

from .registry import ToolSpec, build_tools, json_safe

__all__ = ["ToolSpec", "build_tools", "json_safe"]
