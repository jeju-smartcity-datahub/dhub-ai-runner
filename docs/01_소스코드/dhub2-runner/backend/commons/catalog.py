"""The dataset catalog: what every collection's Delta tables hold, kept in Postgres.

``Catalog.sync`` reads each dataset of a collection from its Delta log (version,
rows, size, files, partitions, columns with their file statistics, commit
history) and the pipelines that read and write it, and stores that next to
the dataset's definition (alias, description, tags, column descriptions).
Only the Delta log is read, never the data files, and a dataset whose
version and definition did not change is skipped.

What people write about a dataset (owner, tags, a readme, column notes)
lives in its own table, so a sync never overwrites it. Saved SQL queries are
kept per collection.

``describe`` answers the same about one dataset straight from its Delta
log, for a runner without Postgres.

Search matches words against a dataset's names, description, tags, owner,
readme and columns; ``tag:``, ``col:``, ``type:``, ``owner:`` and
``collection:`` narrow it to one field.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import polars as pl
import pyarrow as pa

from .collection import Collection
from .storage import Storage

log = logging.getLogger(__name__)

HISTORY = 200  # commits kept per dataset
_TEXT = 200  # longest min/max value kept

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runner_catalog_tables (
    collection TEXT NOT NULL,
    name TEXT NOT NULL,
    dataset_id TEXT,
    alias TEXT,
    description TEXT,
    collection_alias TEXT,
    doc_tags JSONB NOT NULL DEFAULT '[]',
    uri TEXT NOT NULL,
    defined BOOLEAN NOT NULL DEFAULT true,
    has_data BOOLEAN NOT NULL DEFAULT false,
    version BIGINT,
    rows BIGINT,
    size_bytes BIGINT,
    files INTEGER,
    partition_columns JSONB NOT NULL DEFAULT '[]',
    properties JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ,
    modified_at TIMESTAMPTZ,
    synced_at TIMESTAMPTZ NOT NULL,
    error TEXT,
    fingerprint TEXT,
    tags TEXT[] NOT NULL DEFAULT '{}',
    owner TEXT,
    search TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (collection, name)
);
CREATE TABLE IF NOT EXISTS runner_catalog_columns (
    collection TEXT NOT NULL,
    dataset TEXT NOT NULL,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    type TEXT,
    nullable BOOLEAN,
    description TEXT,
    declared BOOLEAN NOT NULL DEFAULT false,
    null_count BIGINT,
    min_value TEXT,
    max_value TEXT,
    PRIMARY KEY (collection, dataset, name)
);
CREATE TABLE IF NOT EXISTS runner_catalog_history (
    collection TEXT NOT NULL,
    dataset TEXT NOT NULL,
    version BIGINT NOT NULL,
    committed_at TIMESTAMPTZ,
    operation TEXT,
    parameters JSONB NOT NULL DEFAULT '{}',
    metrics JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (collection, dataset, version)
);
CREATE TABLE IF NOT EXISTS runner_catalog_lineage (
    collection TEXT NOT NULL,
    dataset TEXT NOT NULL,
    pipeline TEXT NOT NULL,
    step TEXT NOT NULL,
    direction TEXT NOT NULL,
    mode TEXT,
    PRIMARY KEY (collection, dataset, pipeline, step, direction)
);
CREATE TABLE IF NOT EXISTS runner_catalog_notes (
    collection TEXT NOT NULL,
    dataset TEXT NOT NULL,
    owner TEXT,
    tags JSONB NOT NULL DEFAULT '[]',
    readme TEXT,
    columns JSONB NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (collection, dataset)
);
CREATE TABLE IF NOT EXISTS runner_catalog_queries (
    id TEXT PRIMARY KEY,
    collection TEXT NOT NULL,
    title TEXT NOT NULL,
    sql TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS runner_catalog_queries_collection
    ON runner_catalog_queries (collection);
"""

# Substring search over the search text, when pg_trgm can be installed.
_TRGM = [
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    "CREATE INDEX IF NOT EXISTS runner_catalog_tables_search "
    "ON runner_catalog_tables USING gin (search gin_trgm_ops)",
]

SORTS = {
    "relevance": None,
    "name": "t.name",
    "rows": "t.rows",
    "size": "t.size_bytes",
    "modified": "t.modified_at",
    "version": "t.version",
    "collection": "t.collection",
}
QUALIFIERS = {
    "tag": "tag",
    "col": "column",
    "column": "column",
    "type": "type",
    "owner": "owner",
    "collection": "collection",
}
_TERM = re.compile(r'(\w+):"([^"]*)"|(\w+):(\S+)|"([^"]*)"|(\S+)')


class CatalogError(ValueError):
    pass


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Catalog:
    def __init__(self, url: str):
        self.url = url
        with self._connect() as db:
            db.execute(_SCHEMA)
            for statement in _TRGM:
                try:
                    db.execute(statement)
                except Exception as exc:  # no extension or no right to install it
                    log.info("catalog search without a trigram index: %s", exc)
                    break

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self.url, row_factory=dict_row, autocommit=True) as db:
            yield db

    # -- sync ----------------------------------------------------------------

    def sync(
        self,
        collection_name: str,
        collection: Collection,
        storage: Storage,
        alias: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Bring one collection's datasets up to date; returns the datasets
        read again (``synced``) and those no longer in the collection (``removed``)."""
        docs = datasets(collection)
        lineage = _lineage(collection)
        synced, removed = [], []
        with self._connect() as db, db.transaction():
            # One sync of a collection at a time (the refresher, runs, imports).
            db.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"catalog:{collection_name}",)
            )
            known = {
                r["name"]: r["fingerprint"]
                for r in db.execute(
                    "SELECT name, fingerprint FROM runner_catalog_tables WHERE collection = %s",
                    (collection_name,),
                ).fetchall()
            }
            for name, doc in docs.items():
                try:
                    table = storage.table(name)
                    version = table.version() if table is not None else None
                    error = None
                except Exception as exc:  # an unreadable table is listed with its error
                    table, version, error = None, None, f"{type(exc).__name__}: {exc}"
                fingerprint = _fingerprint(doc, alias, version, error)
                if not force and known.get(name) == fingerprint:
                    continue
                snap = None
                if table is not None:
                    try:
                        snap = snapshot(table)
                    except Exception as exc:  # recorded on the dataset
                        log.exception("catalog: %s/%s could not be read", collection_name, name)
                        error = f"{type(exc).__name__}: {exc}"
                self._store(
                    db, collection_name, doc, alias, storage.uri(name), snap, error, fingerprint
                )
                synced.append(name)
            for name in set(known) - set(docs):
                for table_name, column in _PER_DATASET:
                    db.execute(
                        f"DELETE FROM {table_name} WHERE collection = %s AND {column} = %s",
                        (collection_name, name),
                    )
                removed.append(name)
            db.execute(
                "DELETE FROM runner_catalog_lineage WHERE collection = %s", (collection_name,)
            )
            for row in lineage:
                if row[0] in docs:
                    db.execute(
                        "INSERT INTO runner_catalog_lineage VALUES (%s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT DO NOTHING",
                        (collection_name, *row),
                    )
            for name in synced:
                self._refresh(db, collection_name, name)
        return {"collection": collection_name, "synced": sorted(synced), "removed": sorted(removed)}

    def _store(
        self,
        db: Any,
        collection: str,
        doc: dict[str, Any],
        alias: str | None,
        uri: str,
        snap: dict[str, Any] | None,
        error: str | None,
        fingerprint: str,
    ) -> None:
        name = doc["name"]
        snap = snap or {}
        db.execute(
            """
            INSERT INTO runner_catalog_tables (
                collection, name, dataset_id, alias, description, collection_alias, doc_tags,
                uri, defined, has_data, version, rows, size_bytes, files, partition_columns,
                properties, created_at, modified_at, synced_at, error, fingerprint
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s)
            ON CONFLICT (collection, name) DO UPDATE SET
                dataset_id = excluded.dataset_id, alias = excluded.alias,
                description = excluded.description, collection_alias = excluded.collection_alias,
                doc_tags = excluded.doc_tags, uri = excluded.uri, defined = excluded.defined,
                has_data = excluded.has_data,
                version = excluded.version, rows = excluded.rows,
                size_bytes = excluded.size_bytes, files = excluded.files,
                partition_columns = excluded.partition_columns,
                properties = excluded.properties, created_at = excluded.created_at,
                modified_at = excluded.modified_at, synced_at = excluded.synced_at,
                error = excluded.error, fingerprint = excluded.fingerprint
            """,
            (
                collection,
                name,
                doc.get("id"),
                doc.get("alias"),
                doc.get("description"),
                alias,
                _json([str(t) for t in doc.get("tags") or []]),
                uri,
                not doc.get("_undefined"),
                bool(snap),
                snap.get("version"),
                snap.get("rows"),
                snap.get("size_bytes"),
                snap.get("files"),
                _json(snap.get("partition_columns") or []),
                _json(snap.get("properties") or {}),
                snap.get("created_at"),
                snap.get("modified_at"),
                now(),
                error,
                fingerprint,
            ),
        )
        db.execute(
            "DELETE FROM runner_catalog_columns WHERE collection = %s AND dataset = %s",
            (collection, name),
        )
        for position, column in enumerate(_columns(doc, snap)):
            db.execute(
                "INSERT INTO runner_catalog_columns VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    collection,
                    name,
                    position,
                    column["name"],
                    column["type"],
                    column["nullable"],
                    column["description"],
                    column["declared"],
                    column["null_count"],
                    column["min"],
                    column["max"],
                ),
            )
        db.execute(
            "DELETE FROM runner_catalog_history WHERE collection = %s AND dataset = %s",
            (collection, name),
        )
        for commit in snap.get("history") or []:
            db.execute(
                "INSERT INTO runner_catalog_history VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (
                    collection,
                    name,
                    commit["version"],
                    commit["committed_at"],
                    commit["operation"],
                    _json(commit["parameters"]),
                    _json(commit["metrics"]),
                ),
            )

    def _refresh(self, db: Any, collection: str, name: str) -> None:
        """Recompute a dataset's tags, owner and search text from its
        definition, its columns and its notes."""
        row = db.execute(
            "SELECT * FROM runner_catalog_tables WHERE collection = %s AND name = %s",
            (collection, name),
        ).fetchone()
        if row is None:
            return
        notes = _notes(db, collection, name)
        columns = db.execute(
            "SELECT name, type, description FROM runner_catalog_columns "
            "WHERE collection = %s AND dataset = %s",
            (collection, name),
        ).fetchall()
        tags = sorted({*row["doc_tags"], *notes["tags"]}, key=str.lower)
        words = [
            row["collection"],
            row["collection_alias"],
            row["name"],
            row["alias"],
            row["description"],
            notes["owner"],
            notes["readme"],
            *tags,
            *(f"{c['name']} {c['description'] or ''}" for c in columns),
            *notes["columns"].values(),
        ]
        db.execute(
            "UPDATE runner_catalog_tables SET tags = %s, owner = %s, search = %s "
            "WHERE collection = %s AND name = %s",
            (
                tags,
                notes["owner"],
                " ".join(str(w) for w in words if w).lower(),
                collection,
                name,
            ),
        )

    def forget(self, collection: str) -> None:
        """Remove everything about a collection (it was deleted)."""
        with self._connect() as db, db.transaction():
            for table_name, _ in _PER_DATASET:
                db.execute(f"DELETE FROM {table_name} WHERE collection = %s", (collection,))
            db.execute("DELETE FROM runner_catalog_notes WHERE collection = %s", (collection,))
            db.execute("DELETE FROM runner_catalog_queries WHERE collection = %s", (collection,))

    def collections(self) -> list[str]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT DISTINCT collection FROM runner_catalog_tables ORDER BY collection"
            ).fetchall()
        return [r["collection"] for r in rows]

    # -- search --------------------------------------------------------------

    def search(
        self,
        q: str | None = None,
        collection: str | None = None,
        tags: list[str] | None = None,
        owner: str | None = None,
        has_data: bool | None = None,
        sort: str = "relevance",
        descending: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Datasets matching ``q`` and the filters, a page of them, and counts
        per collection, tag and owner over everything ``q`` matches."""
        if sort not in SORTS:
            raise CatalogError(f"unknown sort {sort!r}; use one of {sorted(SORTS)}")
        terms, fields = parse_query(q or "")
        base, base_params = _query_filters(terms, fields)
        narrow, narrow_params = list(base), list(base_params)
        if collection:
            narrow.append("t.collection = %s")
            narrow_params.append(collection)
        for tag in tags or []:
            narrow.append("EXISTS (SELECT 1 FROM unnest(t.tags) g WHERE lower(g) = lower(%s))")
            narrow_params.append(tag)
        if owner:
            narrow.append("lower(t.owner) = lower(%s)")
            narrow_params.append(owner)
        if has_data is not None:
            narrow.append("t.has_data = %s")
            narrow_params.append(has_data)
        where = " AND ".join(narrow) or "true"
        base_where = " AND ".join(base) or "true"
        score, score_params = _score(terms + fields.get("column", []))
        column = SORTS[sort]
        if column is None:
            order = "score DESC, t.collection, t.name"
        else:
            desc = descending if descending is not None else sort in ("rows", "size", "modified")
            order = f"{column} {'DESC' if desc else 'ASC'} NULLS LAST, t.collection, t.name"
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        with self._connect() as db:
            rows = db.execute(
                f"SELECT t.*, {score} AS score FROM runner_catalog_tables t WHERE {where} "
                f"ORDER BY {order} LIMIT %s OFFSET %s",
                (*score_params, *narrow_params, limit, offset),
            ).fetchall()
            totals = db.execute(
                "SELECT count(*) AS datasets, coalesce(sum(rows), 0)::bigint AS rows, "
                "coalesce(sum(size_bytes), 0)::bigint AS size_bytes, "
                "count(*) FILTER (WHERE has_data) AS with_data, "
                f"count(DISTINCT collection) AS collections FROM runner_catalog_tables t "
                f"WHERE {where}",
                narrow_params,
            ).fetchone()
            facets = {
                "collections": db.execute(
                    "SELECT t.collection AS value, max(t.collection_alias) AS label, "
                    f"count(*) AS count FROM runner_catalog_tables t WHERE {base_where} "
                    "GROUP BY t.collection ORDER BY t.collection",
                    base_params,
                ).fetchall(),
                "tags": db.execute(
                    "SELECT g AS value, count(*) AS count FROM runner_catalog_tables t, "
                    f"unnest(t.tags) g WHERE {base_where} GROUP BY g ORDER BY count(*) DESC, g "
                    "LIMIT 50",
                    base_params,
                ).fetchall(),
                "owners": db.execute(
                    "SELECT t.owner AS value, count(*) AS count FROM runner_catalog_tables t "
                    f"WHERE {base_where} AND t.owner IS NOT NULL AND t.owner <> '' "
                    "GROUP BY t.owner ORDER BY count(*) DESC, t.owner LIMIT 50",
                    base_params,
                ).fetchall(),
                "has_data": db.execute(
                    "SELECT t.has_data AS value, count(*) AS count FROM runner_catalog_tables t "
                    f"WHERE {base_where} GROUP BY t.has_data",
                    base_params,
                ).fetchall(),
            }
            matched = self._matched_columns(db, rows, terms + fields.get("column", []))
            synced = db.execute("SELECT max(synced_at) AS at FROM runner_catalog_tables").fetchone()
        items = [
            {
                **_table(r),
                "score": r["score"],
                "matched_columns": matched.get((r["collection"], r["name"]), []),
            }
            for r in rows
        ]
        return {
            "query": {"terms": terms, "fields": fields},
            "total": totals["datasets"],
            "totals": totals,
            "items": items,
            "facets": facets,
            "synced_at": synced["at"],
        }

    def _matched_columns(
        self, db: Any, rows: list[dict[str, Any]], terms: list[str]
    ) -> dict[tuple[str, str], list[str]]:
        if not rows or not terms:
            return {}
        likes = " OR ".join(
            "lower(name || ' ' || coalesce(description, '')) LIKE %s" for _ in terms
        )
        found: dict[tuple[str, str], list[str]] = {}
        for r in rows:
            columns = db.execute(
                f"SELECT name FROM runner_catalog_columns WHERE collection = %s AND dataset = %s "
                f"AND ({likes}) ORDER BY position LIMIT 10",
                (r["collection"], r["name"], *(_like(t) for t in terms)),
            ).fetchall()
            if columns:
                found[(r["collection"], r["name"])] = [c["name"] for c in columns]
        return found

    # -- one dataset ---------------------------------------------------------

    def get(self, collection: str, name: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM runner_catalog_tables WHERE collection = %s AND name = %s",
                (collection, name),
            ).fetchone()
            if row is None:
                return None
            notes = _notes(db, collection, name)
            columns = db.execute(
                "SELECT * FROM runner_catalog_columns WHERE collection = %s AND dataset = %s "
                "ORDER BY position",
                (collection, name),
            ).fetchall()
            history = db.execute(
                "SELECT version, committed_at, operation, parameters, metrics "
                "FROM runner_catalog_history WHERE collection = %s AND dataset = %s "
                "ORDER BY version DESC",
                (collection, name),
            ).fetchall()
            uses = db.execute(
                "SELECT pipeline, step, direction, mode FROM runner_catalog_lineage "
                "WHERE collection = %s AND dataset = %s ORDER BY pipeline, step",
                (collection, name),
            ).fetchall()
            # Datasets the steps writing this one read, and the ones the steps
            # reading it write.
            neighbours = db.execute(
                """
                SELECT o.dataset, o.pipeline, o.step, o.direction
                FROM runner_catalog_lineage s JOIN runner_catalog_lineage o
                  ON o.collection = s.collection AND o.pipeline = s.pipeline
                 AND o.step = s.step AND o.direction <> s.direction
                WHERE s.collection = %s AND s.dataset = %s AND o.dataset <> s.dataset
                ORDER BY o.dataset, o.pipeline, o.step
                """,
                (collection, name),
            ).fetchall()
        return {
            **_table(row),
            "columns": [
                {
                    "name": c["name"],
                    "type": c["type"],
                    "nullable": c["nullable"],
                    "description": c["description"],
                    "declared": c["declared"],
                    "null_count": c["null_count"],
                    "min": c["min_value"],
                    "max": c["max_value"],
                    "note": notes["columns"].get(c["name"]),
                }
                for c in columns
            ],
            "history": history,
            "lineage": {
                "writers": [u for u in uses if u["direction"] == "write"],
                "readers": [u for u in uses if u["direction"] == "read"],
                "upstream": [
                    {k: n[k] for k in ("dataset", "pipeline", "step")}
                    for n in neighbours
                    if n["direction"] == "read"
                ],
                "downstream": [
                    {k: n[k] for k in ("dataset", "pipeline", "step")}
                    for n in neighbours
                    if n["direction"] == "write"
                ],
            },
            "notes": notes,
            "catalog": True,
        }

    def set_notes(self, collection: str, name: str, changes: dict[str, Any]) -> None:
        """Change what people wrote about a dataset: ``owner``, ``tags``,
        ``readme`` and ``columns`` (``{column: note}``); keys left out stay."""
        unknown = set(changes) - {"owner", "tags", "readme", "columns"}
        if unknown:
            raise CatalogError(f"unknown fields {sorted(unknown)}")
        with self._connect() as db, db.transaction():
            if not db.execute(
                "SELECT 1 FROM runner_catalog_tables WHERE collection = %s AND name = %s",
                (collection, name),
            ).fetchone():
                raise KeyError(name)
            notes = _notes(db, collection, name)
            if "owner" in changes:
                notes["owner"] = _text(changes["owner"], "owner", 200)
            if "readme" in changes:
                notes["readme"] = _text(changes["readme"], "readme", 100_000)
            if "tags" in changes:
                notes["tags"] = _tags(changes["tags"])
            if "columns" in changes:
                columns = changes["columns"] or {}
                if not isinstance(columns, dict):
                    raise CatalogError("columns must be an object of column notes")
                for column, note in columns.items():
                    note = _text(note, f"note of {column}", 2000)
                    if note:
                        notes["columns"][str(column)] = note
                    else:
                        notes["columns"].pop(str(column), None)
            db.execute(
                "INSERT INTO runner_catalog_notes VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (collection, dataset) DO UPDATE SET owner = excluded.owner, "
                "tags = excluded.tags, readme = excluded.readme, columns = excluded.columns, "
                "updated_at = excluded.updated_at",
                (
                    collection,
                    name,
                    notes["owner"],
                    _json(notes["tags"]),
                    notes["readme"],
                    _json(notes["columns"]),
                    now(),
                ),
            )
            self._refresh(db, collection, name)

    # -- saved queries -------------------------------------------------------

    def queries(self, collection: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            return db.execute(
                "SELECT id, title, sql, created_at, updated_at FROM runner_catalog_queries "
                "WHERE collection = %s ORDER BY updated_at DESC",
                (collection,),
            ).fetchall()

    def save_query(
        self, collection: str, title: str, sql: str, query_id: str | None = None
    ) -> dict[str, Any]:
        title = _text(title, "title", 200)
        sql = _text(sql, "sql", 100_000)
        if not title or not sql:
            raise CatalogError("a saved query needs a title and SQL")
        stamp = now()
        with self._connect() as db:
            if query_id:
                row = db.execute(
                    "UPDATE runner_catalog_queries SET title = %s, sql = %s, updated_at = %s "
                    "WHERE collection = %s AND id = %s "
                    "RETURNING id, title, sql, created_at, updated_at",
                    (title, sql, stamp, collection, query_id),
                ).fetchone()
                if row is None:
                    raise KeyError(query_id)
                return row
            return db.execute(
                "INSERT INTO runner_catalog_queries VALUES (%s, %s, %s, %s, %s, %s) "
                "RETURNING id, title, sql, created_at, updated_at",
                (uuid.uuid4().hex[:12], collection, title, sql, stamp, stamp),
            ).fetchone()

    def delete_query(self, collection: str, query_id: str) -> bool:
        with self._connect() as db:
            return (
                db.execute(
                    "DELETE FROM runner_catalog_queries WHERE collection = %s AND id = %s",
                    (collection, query_id),
                ).rowcount
                > 0
            )


def describe(
    collection_name: str,
    collection: Collection,
    storage: Storage,
    name: str,
    alias: str | None = None,
) -> dict[str, Any] | None:
    """What ``Catalog.get`` answers, read from the Delta log and the collection
    directly: for a runner without Postgres, which keeps no notes."""
    docs = datasets(collection)
    doc = docs.get((collection.find("dataset", name) or {}).get("name") or name)
    if doc is None:
        return None
    name = doc["name"]
    snap, error = None, None
    try:
        table = storage.table(name)
        snap = snapshot(table) if table is not None else None
    except Exception as exc:  # shown with the dataset
        error = f"{type(exc).__name__}: {exc}"
    found = snap or {}
    notes = {"owner": None, "tags": [], "readme": None, "columns": {}, "updated_at": None}
    uses = [r for r in _lineage(collection) if r[0] == name]
    others = [
        r
        for r in _lineage(collection)
        if r[0] != name and any(r[1:3] == u[1:3] and r[3] != u[3] for u in uses)
    ]

    def steps(direction: str) -> list[dict[str, Any]]:
        return [
            {"pipeline": p, "step": s, "direction": d, "mode": m}
            for _, p, s, d, m in sorted(uses, key=lambda r: r[1:3])
            if d == direction
        ]

    def neighbours(direction: str) -> list[dict[str, Any]]:
        return [
            {"dataset": n, "pipeline": p, "step": s}
            for n, p, s, d, _ in sorted(others)
            if d == direction
        ]

    return {
        "collection": collection_name,
        "name": name,
        "dataset_id": doc.get("id"),
        "alias": doc.get("alias"),
        "description": doc.get("description"),
        "collection_alias": alias,
        "uri": storage.uri(name),
        "defined": not doc.get("_undefined"),
        "has_data": bool(snap),
        "version": found.get("version"),
        "rows": found.get("rows"),
        "size_bytes": found.get("size_bytes"),
        "files": found.get("files"),
        "partition_columns": found.get("partition_columns") or [],
        "properties": found.get("properties") or {},
        "created_at": found.get("created_at"),
        "modified_at": found.get("modified_at"),
        "synced_at": None,
        "error": error,
        "tags": sorted({str(t) for t in doc.get("tags") or []}, key=str.lower),
        "owner": None,
        "columns": [{**c, "note": None} for c in _columns(doc, snap)],
        "history": sorted(found.get("history") or [], key=lambda h: -h["version"]),
        "lineage": {
            "writers": steps("write"),
            "readers": steps("read"),
            "upstream": neighbours("read"),
            "downstream": neighbours("write"),
        },
        "notes": notes,
        "catalog": False,
    }


def open_catalog() -> Catalog | None:
    """The catalog in Postgres when RUNNER_DATABASE_URL is set; None otherwise."""
    url = os.environ.get("RUNNER_DATABASE_URL")
    return Catalog(url) if url else None


# Tables that hold one row set per dataset, and the column naming the dataset.
_PER_DATASET = [
    ("runner_catalog_tables", "name"),
    ("runner_catalog_columns", "dataset"),
    ("runner_catalog_history", "dataset"),
    ("runner_catalog_lineage", "dataset"),
]


# -- reading a Delta table ---------------------------------------------------


def snapshot(table: Any) -> dict[str, Any]:
    """What the catalog keeps of a Delta table, from its log alone."""
    files = pl.from_arrow(pa.table(table.get_add_actions(flatten=True)))
    assert isinstance(files, pl.DataFrame)
    metadata = table.metadata()
    schema = pl.scan_delta(table).collect_schema()
    nullable = {f.name: f.nullable for f in table.schema().fields}
    if "num_records" in files.columns and files["num_records"].null_count() == 0:
        rows = int(files["num_records"].sum())
    else:  # a writer that kept no row counts
        rows = pl.scan_delta(table).select(pl.len()).collect().item()
    history = []
    for commit in table.history(limit=HISTORY):
        history.append(
            {
                "version": commit.get("version"),
                "committed_at": _from_ms(commit.get("timestamp")),
                "operation": commit.get("operation"),
                "parameters": commit.get("operationParameters") or {},
                "metrics": commit.get("operationMetrics") or {},
            }
        )
    modified = files["modification_time"].max() if "modification_time" in files.columns else None
    if history and history[0]["committed_at"]:
        modified = history[0]["committed_at"]
    elif isinstance(modified, int):
        modified = _from_ms(modified)
    return {
        "version": table.version(),
        "rows": rows,
        "size_bytes": int(files["size_bytes"].sum()) if "size_bytes" in files.columns else None,
        "files": files.height,
        "partition_columns": list(metadata.partition_columns),
        "properties": dict(metadata.configuration),
        "created_at": _from_ms(metadata.created_time),
        "modified_at": modified,
        "columns": [
            {
                "name": column,
                "type": str(dtype),
                "nullable": nullable.get(column),
                **_column_stats(files, column),
            }
            for column, dtype in schema.items()
        ],
        "history": [h for h in history if h["version"] is not None],
    }


def _column_stats(files: pl.DataFrame, column: str) -> dict[str, Any]:
    """Null count, min and max over the files' statistics; nothing when a
    file has none for the column (its writer kept no statistics)."""
    nulls, low, high = (f"null_count.{column}", f"min.{column}", f"max.{column}")
    if files.is_empty():
        return {"null_count": 0}
    if nulls not in files.columns or files[nulls].null_count():
        return {}
    stats: dict[str, Any] = {"null_count": int(files[nulls].sum())}
    for key, source, pick in (("min", low, "min"), ("max", high, "max")):
        if source in files.columns and not files[source].dtype.is_nested():
            try:
                stats[key] = _value_text(getattr(files[source], pick)())
            except Exception:  # a type without an order
                pass
    return stats


def _value_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        text = value.isoformat()
    elif isinstance(value, bytes):
        text = value.hex()
    else:
        text = str(value)
    return text if len(text) <= _TEXT else text[: _TEXT - 1] + "…"


def _from_ms(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    return dt.datetime.fromtimestamp(int(value) / 1000, dt.UTC)


# -- definitions -------------------------------------------------------------


def _columns(doc: dict[str, Any], snap: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The table's columns when it has one, else the ones the definition
    declares, with the descriptions the definition gives."""
    declared = _declared_fields(doc)
    found = (snap or {}).get("columns") or [
        {"name": f["name"], "type": _type_name(f.get("type")), "nullable": f.get("nullable")}
        for f in declared.values()
    ]
    columns = []
    for column in found:
        field = declared.get(column["name"]) or {}
        columns.append(
            {
                "name": column["name"],
                "type": column.get("type"),
                "nullable": column.get("nullable"),
                "description": field.get("description"),
                "declared": bool(field),
                "null_count": column.get("null_count"),
                "min": column.get("min"),
                "max": column.get("max"),
            }
        )
    return columns


def _declared_fields(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    fields = (doc.get("schema") or doc.get("schema_") or {}).get("fields") or []
    return {f["name"]: f for f in fields if isinstance(f, dict) and f.get("name")}


def _type_name(type_json: Any) -> str | None:
    if isinstance(type_json, dict):
        return type_json.get("name")
    return str(type_json) if type_json else None


def _fingerprint(doc: dict[str, Any], alias: str | None, version: Any, error: Any) -> str:
    raw = json.dumps([doc, alias, version, error], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


_DATA_KINDS = ("dataset", "entity", "relation", "source", "view", "variable")


def datasets(collection: Collection) -> dict[str, dict[str, Any]]:
    """A collection's datasets by name: the ones it defines, and the ones its
    pipelines name without a definition (a step may write a new table)."""
    found = {doc["name"]: doc for doc in collection.list("dataset")}
    for _, ref, _, _, _, _ in _ports(collection):
        if ref and collection.find("dataset", ref) is None:
            found.setdefault(ref, {"name": ref, "_undefined": True})
    return found


def _ports(collection: Collection) -> Iterator[tuple[str, str | None, str, str, str, str | None]]:
    """``(port, dataset ref, pipeline, step, read|write, mode)`` for every
    step port that is not a variable or another kind of data. A port with no
    target names the dataset called like the port, when there is one, as the
    pipeline runner reads it."""
    for doc in collection.list("pipeline"):
        for step in doc.get("steps") or []:
            if not isinstance(step, dict) or not step.get("name"):
                continue
            for direction, ports in (("read", step.get("inputs")), ("write", step.get("outputs"))):
                for port, data in (ports or {}).items():
                    data = data if isinstance(data, dict) else {}
                    ref = data.get("dataset")
                    if ref is None:
                        if any(data.get(k) for k in _DATA_KINDS):
                            continue
                        if collection.find("dataset", port) is None:
                            continue
                        ref = port
                    options = data.get("options") or {}
                    mode = options.get("read_mode" if direction == "read" else "write_mode")
                    yield port, ref, doc["name"], step["name"], direction, mode


def _lineage(collection: Collection) -> list[tuple[str, str, str, str, str | None]]:
    """``(dataset, pipeline, step, read|write, mode)`` for every step port
    on a dataset."""
    return [
        (
            (collection.find("dataset", ref) or {}).get("name") or ref,
            pipeline,
            step,
            direction,
            mode,
        )
        for _, ref, pipeline, step, direction, mode in _ports(collection)
        if ref
    ]


# -- search ------------------------------------------------------------------


def parse_query(q: str) -> tuple[list[str], dict[str, list[str]]]:
    """Words and ``field:value`` filters (``tag:`` ``col:`` ``type:``
    ``owner:`` ``collection:``); quotes keep spaces in a word or a value."""
    terms: list[str] = []
    fields: dict[str, list[str]] = {}
    for match in _TERM.finditer(q):
        key = (match.group(1) or match.group(3) or "").lower()
        value = match.group(2) if match.group(1) else match.group(4)
        if key in QUALIFIERS and value:
            fields.setdefault(QUALIFIERS[key], []).append(value.lower())
            continue
        word = match.group(5) if match.group(5) is not None else match.group(0)
        if word.strip():
            terms.append(word.strip().lower())
    return terms, fields


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _query_filters(terms: list[str], fields: dict[str, list[str]]) -> tuple[list[str], list]:
    where, params = [], []
    for term in terms:
        where.append("t.search LIKE %s")
        params.append(_like(term))
    for tag in fields.get("tag", []):
        where.append("EXISTS (SELECT 1 FROM unnest(t.tags) g WHERE lower(g) LIKE %s)")
        params.append(_like(tag))
    for column in fields.get("column", []):
        where.append(
            "EXISTS (SELECT 1 FROM runner_catalog_columns c WHERE c.collection = t.collection "
            "AND c.dataset = t.name AND lower(c.name) LIKE %s)"
        )
        params.append(_like(column))
    for type_name in fields.get("type", []):
        where.append(
            "EXISTS (SELECT 1 FROM runner_catalog_columns c WHERE c.collection = t.collection "
            "AND c.dataset = t.name AND lower(c.type) LIKE %s)"
        )
        params.append(_like(type_name))
    for owner in fields.get("owner", []):
        where.append("lower(coalesce(t.owner, '')) LIKE %s")
        params.append(_like(owner))
    for collection in fields.get("collection", []):
        where.append(
            "(lower(t.collection) LIKE %s OR lower(coalesce(t.collection_alias, '')) LIKE %s)"
        )
        params += [_like(collection), _like(collection)]
    return where, params


def _score(terms: list[str]) -> tuple[str, list]:
    """Where words matched, weighted: the name above the alias, tags,
    columns and the description."""
    if not terms:
        return "0", []
    parts, params = [], []
    for term in terms:
        parts.append(
            "(CASE WHEN lower(t.name) = %s THEN 100 WHEN lower(t.name) LIKE %s THEN 40 "
            "WHEN lower(coalesce(t.alias, '')) LIKE %s THEN 30 "
            "WHEN EXISTS (SELECT 1 FROM unnest(t.tags) g WHERE lower(g) LIKE %s) THEN 20 "
            "WHEN EXISTS (SELECT 1 FROM runner_catalog_columns c WHERE c.collection = "
            "t.collection AND c.dataset = t.name AND lower(c.name) LIKE %s) THEN 15 "
            "WHEN lower(coalesce(t.description, '')) LIKE %s THEN 10 ELSE 1 END)"
        )
        like = _like(term)
        params += [term, like, like, like, like, like]
    return " + ".join(parts), params


# -- values ------------------------------------------------------------------


def _notes(db: Any, collection: str, name: str) -> dict[str, Any]:
    row = db.execute(
        "SELECT owner, tags, readme, columns, updated_at FROM runner_catalog_notes "
        "WHERE collection = %s AND dataset = %s",
        (collection, name),
    ).fetchone()
    if row is None:
        return {"owner": None, "tags": [], "readme": None, "columns": {}, "updated_at": None}
    return dict(row)


def _table(row: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "collection",
        "name",
        "dataset_id",
        "alias",
        "description",
        "collection_alias",
        "uri",
        "defined",
        "has_data",
        "version",
        "rows",
        "size_bytes",
        "files",
        "partition_columns",
        "properties",
        "created_at",
        "modified_at",
        "synced_at",
        "error",
        "tags",
        "owner",
    )
    return {k: row[k] for k in keep}


def _text(value: Any, field: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CatalogError(f"{field} must be text")
    value = value.strip()
    if len(value) > limit:
        raise CatalogError(f"{field} is longer than {limit} characters")
    return value or None


def _tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
        raise CatalogError("tags must be a list of text")
    tags: dict[str, str] = {}
    for tag in value:
        tag = tag.strip()
        if len(tag) > 50:
            raise CatalogError(f"tag {tag[:20]!r}… is longer than 50 characters")
        if tag:
            tags.setdefault(tag.lower(), tag)
    return sorted(tags.values(), key=str.lower)


def _json(value: Any) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(value)
