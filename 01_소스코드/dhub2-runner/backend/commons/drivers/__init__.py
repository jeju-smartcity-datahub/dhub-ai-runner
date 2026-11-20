"""Builtin connector drivers.

A dhub2 connector either carries its own ``script`` or binds a builtin
``driver`` (``http``, ``postgres``, ...) with its own options. Each driver here
is a module with the same shape as a connector script: ``connect(options,
collection)`` returns a client, and every other public function takes that
client first (``get(client, path)``, ``query(client, sql, params)``, ...), so
user code calls them exactly as it does in dhub2.
"""

from __future__ import annotations

import importlib
from types import ModuleType

# driver id -> module; the ids and connector types follow dhub2's builtins.
DRIVERS = {
    "http": "http",
    "postgres": "postgres",
}

# dhub2 builtins that only make sense inside dhub2 itself.
NOT_PORTABLE = {
    "dhub2_manager_api": "it calls the dhub2 API",
    "datahub": "it reads dhub2's internal ClickHouse",
    "semantic": "the semantic layer is not supported",
    "neo4j": "ontology graphs are not supported",
    "mcp": "MCP is not supported",
    "clickhouse": "ClickHouse is not supported",
    "qdrant": "Qdrant is not supported",
}


def driver_module(driver: str) -> ModuleType:
    if driver in NOT_PORTABLE:
        raise LookupError(
            f"builtin driver {driver!r} is not available in the runner: {NOT_PORTABLE[driver]}"
        )
    if driver not in DRIVERS:
        raise LookupError(
            f"builtin driver {driver!r} is not available in the runner "
            f"(available: {', '.join(sorted(DRIVERS))})"
        )
    return importlib.import_module(f"{__name__}.{DRIVERS[driver]}")
