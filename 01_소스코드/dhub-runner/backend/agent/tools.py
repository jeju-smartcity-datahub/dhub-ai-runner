"""D.Hub for AI's builtin tools that the runner answers itself.

An agent that lists one of these tools (by name, or by D.Hub for AI's builtin id
``agent.tool.<name>``) gets the runner's version of it.

The dataset tools: D.Hub for AI answers them through its API and the datasets'
ClickHouse mirrors; the runner reads the Delta tables the collection's
pipelines write (on RustFS or a local directory). Together they give an agent
text-to-SQL over its datasets:

- ``resources_search`` / ``datasets_search``: the collection's datasets
- ``analytics_source``: a dataset's table name and columns
- ``analytics_query``: one read-only ``SELECT`` in Polars SQL; joins across
  the collection's datasets are allowed

Only the collection's own datasets (``datasets/*.json`` in the collection) can be
read.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import polars as pl
from commons.collection import Collection
from commons.storage import Storage

DEFAULT_ROWS = 100
MAX_ROWS = 1000

_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_READ = re.compile(r"^[\s(]*(select|with)\b", re.IGNORECASE)

_KIND = {
    "type": "string",
    "enum": ["dataset"],
    "description": "The source kind. The runner reads datasets only.",
}
_SEARCH_INPUTS = {
    "name_filter": {
        "type": "string",
        "description": "A case-insensitive substring of the dataset's name, alias or description.",
    },
    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
}

SPECS: dict[str, dict[str, Any]] = {
    "resources_search": {
        "description": (
            "Find the datasets of this collection by name or alias. Each item carries "
            "`kind`, `id`, `name`, `alias`, `description` and `has_data` (false until a "
            "pipeline has written the dataset). Pass an item's `id` to `analytics_source`."
        ),
        "inputs": {
            "type": "object",
            "properties": {
                "kind": _KIND,
                "kinds": {"type": "array", "items": _KIND, "minItems": 1},
                **_SEARCH_INPUTS,
            },
        },
    },
    "datasets_search": {
        "description": (
            "List the datasets of this collection, optionally filtered by name. "
            "Pass a dataset's `id` to `analytics_source`."
        ),
        "inputs": {
            "type": "object",
            "properties": {
                **_SEARCH_INPUTS,
                "query": {"type": "string", "description": "Alias of name_filter."},
            },
        },
    },
    "analytics_source": {
        "description": (
            "Resolve a dataset to the table name and columns `analytics_query` reads. "
            "Call it before writing a query and use only the `qualified_table` and "
            "`columns` it returns. A dataset no pipeline has written yet has no table "
            "and cannot be queried."
        ),
        "inputs": {
            "type": "object",
            "properties": {
                "kind": _KIND,
                "source_id": {
                    "type": "string",
                    "description": "The dataset's id or name (from `resources_search`).",
                },
            },
            "required": ["source_id"],
        },
    },
    "analytics_query": {
        "description": (
            "Run one read-only SQL query over this collection's datasets and return the "
            "rows. Call `analytics_source` first and use its `qualified_table` and "
            "`columns` only.\n\n"
            "- The dialect is Polars SQL (close to PostgreSQL). Quote identifiers with "
            "double quotes; names are case-sensitive.\n"
            "- One `SELECT` (or `WITH ... SELECT`). CTEs, subqueries, joins across the "
            "collection's datasets, `GROUP BY`, window functions are allowed.\n"
            f"- At most `max_rows` rows come back (default {DEFAULT_ROWS}, up to "
            f"{MAX_ROWS}). `truncated: true` means there were more: say so or aggregate "
            "instead of reporting a partial result as the whole.\n"
            "- An empty `rows` list means the query ran and nothing matched."
        ),
        "inputs": {
            "type": "object",
            "properties": {
                "kind": _KIND,
                "source_id": {
                    "type": "string",
                    "description": "The dataset the query mainly reads (optional).",
                },
                "query": {"type": "string", "description": "One SELECT statement."},
                "max_rows": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
            },
            "required": ["query"],
        },
    },
}


def builtin_name(ref: dict[str, Any]) -> str | None:
    """The builtin a tool reference names, by name or by D.Hub for AI's builtin id."""
    ref_id = str(ref.get("id") or "")
    for name in (ref.get("name"), ref_id.removeprefix("agent.tool.")):
        if name in SPECS:
            return name
    return None


def is_dhub2_builtin(doc: dict[str, Any], name: str) -> bool:
    """Whether a collection's tool definition is D.Hub for AI's builtin rather than the
    collection's own tool of the same name."""
    return bool(doc.get("is_builtin")) or doc.get("id") == f"agent.tool.{name}"


class Tools:
    """The builtin tools of one collection."""

    def __init__(self, collection: Collection, storage: Storage):
        self.collection = collection
        self.storage = storage

    def function(self, name: str) -> Callable[..., Any]:
        return {
            "resources_search": self.resources_search,
            "datasets_search": self.datasets_search,
            "analytics_source": self.analytics_source,
            "analytics_query": self.analytics_query,
        }[name]

    # -- search --------------------------------------------------------------

    def resources_search(self, **kwargs: Any) -> dict[str, Any]:
        kinds = kwargs.get("kinds") or [kwargs.get("kind") or "dataset"]
        other = [k for k in kinds if k != "dataset"]
        if other:
            raise ValueError(f"the runner searches datasets only, not {other}")
        items = self._search(kwargs.get("name_filter"), kwargs.get("limit"))
        return {"items": items, "counts": {"dataset": len(items)}}

    def datasets_search(self, **kwargs: Any) -> dict[str, Any]:
        items = self._search(kwargs.get("name_filter") or kwargs.get("query"), kwargs.get("limit"))
        return {"datasets": items, "total_count": len(items)}

    def _search(self, name_filter: str | None, limit: Any) -> list[dict[str, Any]]:
        needle = (name_filter or "").strip().lower()
        found = []
        for doc in self.collection.list("dataset"):
            # The description too, so a search in the user's words finds it.
            texts = [doc["name"], doc.get("alias") or "", doc.get("description") or ""]
            if needle and not any(needle in t.lower() for t in texts):
                continue
            found.append(
                {
                    "kind": "dataset",
                    "id": doc.get("id") or doc["name"],
                    "name": doc["name"],
                    "alias": doc.get("alias"),
                    "description": doc.get("description"),
                    "has_data": self.storage.table(doc["name"]) is not None,
                }
            )
        return found[: max(1, min(200, int(limit or 50)))]

    # -- source and query ----------------------------------------------------

    def analytics_source(self, **kwargs: Any) -> dict[str, Any]:
        _check_kind(kwargs.get("kind"))
        doc = self._dataset(kwargs.get("source_id"))
        name = doc["name"]
        scan = self._scan(name)
        if scan is None:
            raise ValueError(f"dataset {name!r} has no data yet; no pipeline has written it")
        described = {
            f.get("name"): f.get("description")
            for f in (doc.get("schema") or doc.get("schema_") or {}).get("fields") or []
        }
        columns = []
        for column, dtype in scan.collect_schema().items():
            entry = {"name": column, "type": str(dtype)}
            if described.get(column):
                entry["description"] = described[column]
            columns.append(entry)
        return {
            "kind": "dataset",
            "id": doc.get("id") or name,
            "name": name,
            "alias": doc.get("alias"),
            "description": doc.get("description"),
            "table": name,
            "qualified_table": _quote(name),
            "columns": columns,
        }

    def analytics_query(self, **kwargs: Any) -> dict[str, Any]:
        _check_kind(kwargs.get("kind"))
        query = str(kwargs.get("query") or "").strip()
        if not query:
            raise ValueError("'query' is required")
        statement = _COMMENTS.sub(" ", query).strip().rstrip(";").strip()
        if not _READ.match(statement):
            raise ValueError("only one SELECT (or WITH ... SELECT) statement is allowed")
        max_rows = kwargs.get("max_rows")
        try:
            limit = max(1, min(MAX_ROWS, int(max_rows))) if max_rows is not None else DEFAULT_ROWS
        except (TypeError, ValueError) as exc:
            raise ValueError("'max_rows' must be an integer") from exc
        source = self._dataset(kwargs["source_id"]) if kwargs.get("source_id") else None

        # A fresh context per query holds lazy scans of the tables the query
        # names; nothing it runs writes to the Delta tables.
        context = pl.SQLContext()
        tables, empty = [], []
        for doc in self.collection.list("dataset"):
            name = doc["name"]
            if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", statement, re.IGNORECASE):
                scan = self._scan(name)
                if scan is None:
                    empty.append(name)
                else:
                    context.register(name, scan)
                    tables.append(name)
        try:
            frame = context.execute(statement).limit(limit + 1).collect()
        except pl.exceptions.PolarsError as exc:
            # The first paragraph says what is wrong; the rest is Polars' plan.
            message = str(exc).split("\n\n", 1)[0]
            if empty:
                message += f"\n(no pipeline has written {empty} yet, so they have no data)"
            readable = [
                d["name"] for d in self.collection.list("dataset") if self.storage.table(d["name"])
            ]
            raise ValueError(f"{message}\n(tables with data: {readable})") from exc
        result: dict[str, Any] = {
            "rows": frame.head(limit).to_dicts(),
            "row_count": min(frame.height, limit),
            "truncated": frame.height > limit,
            "tables": tables,
            "query": query,
        }
        if source is not None:
            result["source"] = {"kind": "dataset", "id": source.get("id") or source["name"]}
        return result

    def _dataset(self, source_id: Any) -> dict[str, Any]:
        key = str(source_id or "").strip()
        if not key:
            raise ValueError("'source_id' is required")
        doc = self.collection.find("dataset", key) or self.collection.find(
            "dataset", key.removeprefix("dataset.")
        )
        if doc is None:
            names = [d["name"] for d in self.collection.list("dataset")]
            raise ValueError(f"dataset {key!r} not found; datasets: {names}")
        return doc

    def _scan(self, name: str) -> pl.LazyFrame | None:
        if self.storage.table(name) is None:
            return None
        return pl.scan_delta(self.storage.uri(name), storage_options=self.storage.options)


def _check_kind(kind: Any) -> None:
    if kind not in (None, "", "dataset"):
        raise ValueError(f"the runner reads datasets only, not {kind!r}")


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'
