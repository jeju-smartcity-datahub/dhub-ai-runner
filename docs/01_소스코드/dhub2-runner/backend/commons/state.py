"""Runner state: step checkpoints, run history, agent sessions, dataset locks.

``State`` keeps one collection's state in a SQLite file, for a single host.
``PostgresState`` keeps it in Postgres, shared by every pod of a Kubernetes
deployment (the web app, pipeline Jobs, agent pods). ``open_state`` picks
Postgres when RUNNER_DATABASE_URL is set.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    pipeline TEXT NOT NULL,
    step TEXT NOT NULL,
    state TEXT NOT NULL,
    offsets TEXT NOT NULL DEFAULT '{}',
    start_time TEXT,
    end_time TEXT,
    description TEXT,
    stacktrace TEXT,
    PRIMARY KEY (pipeline, step)
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    pipeline TEXT NOT NULL,
    state TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    agent TEXT NOT NULL,
    session_id TEXT NOT NULL,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (agent, session_id)
);
CREATE TABLE IF NOT EXISTS stopped (
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    since TEXT NOT NULL,
    PRIMARY KEY (kind, name)
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class State:
    def __init__(self, data_dir: str | Path):
        path = Path(data_dir)
        path.mkdir(parents=True, exist_ok=True)
        self.path = path / "runner.db"
        with self._connect() as db:
            # Several processes (the web app and the runs it starts) share the file.
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    # -- checkpoints -------------------------------------------------------

    def get_checkpoint(self, pipeline: str, step: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM checkpoints WHERE pipeline = ? AND step = ?",
                (pipeline, step),
            ).fetchone()
        if row is None:
            return {"state": "new", "offsets": {}}
        return {**dict(row), "offsets": json.loads(row["offsets"])}

    def set_checkpoint(self, pipeline: str, step: str, **fields: Any) -> None:
        current = self.get_checkpoint(pipeline, step)
        current.update(fields)
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pipeline,
                    step,
                    current["state"],
                    json.dumps(current.get("offsets") or {}),
                    current.get("start_time"),
                    current.get("end_time"),
                    current.get("description"),
                    current.get("stacktrace"),
                ),
            )

    def list_checkpoints(self, pipeline: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM checkpoints WHERE pipeline = ? ORDER BY step", (pipeline,)
            ).fetchall()
        return [{**dict(r), "offsets": json.loads(r["offsets"])} for r in rows]

    def reset_checkpoints(self, pipeline: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM checkpoints WHERE pipeline = ?", (pipeline,))

    # -- runs --------------------------------------------------------------

    def queue_run(self, run_id: str, pipeline: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO runs (id, pipeline, state, start_time) VALUES (?, ?, ?, ?)",
                (run_id, pipeline, "queued", now()),
            )

    def start_run(self, run_id: str, pipeline: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO runs (id, pipeline, state, start_time) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET state = excluded.state, "
                "start_time = excluded.start_time",
                (run_id, pipeline, "running", now()),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def interrupt_runs(self) -> int:
        """Mark runs left queued or running by a stopped process as interrupted."""
        with self._connect() as db:
            cur = db.execute(
                "UPDATE runs SET state = 'interrupted', end_time = ? "
                "WHERE state IN ('queued', 'running')",
                (now(),),
            )
        return cur.rowcount

    def end_run(self, run_id: str, state: str, error: str | None = None) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE runs SET state = ?, end_time = ?, error = ? WHERE id = ?",
                (state, now(), error, run_id),
            )

    def list_runs(self, pipeline: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM runs WHERE pipeline = ? ORDER BY start_time DESC LIMIT ?",
                (pipeline, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- agent sessions ----------------------------------------------------

    def get_session(self, agent: str, session_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT data FROM sessions WHERE agent = ? AND session_id = ?",
                (agent, session_id),
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def save_session(self, agent: str, session_id: str, data: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?)",
                (agent, session_id, json.dumps(data, ensure_ascii=False, default=str), now()),
            )

    def delete_session(self, agent: str, session_id: str) -> bool:
        with self._connect() as db:
            cur = db.execute(
                "DELETE FROM sessions WHERE agent = ? AND session_id = ?", (agent, session_id)
            )
        return cur.rowcount > 0

    def list_sessions(self, agent: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT session_id, data, updated_at FROM sessions WHERE agent = ? "
                "ORDER BY updated_at DESC",
                (agent,),
            ).fetchall()
        return [_session_summary(dict(r)) for r in rows]

    # -- stopped pipelines and agents --------------------------------------

    def stopped(self, kind: str) -> dict[str, str]:
        """``{name: since}`` of the pipelines (schedule paused) or agents stopped."""
        with self._connect() as db:
            rows = db.execute("SELECT name, since FROM stopped WHERE kind = ?", (kind,)).fetchall()
        return {r["name"]: r["since"] for r in rows}

    def set_stopped(self, kind: str, name: str, stopped: bool) -> None:
        with self._connect() as db:
            if stopped:
                db.execute("INSERT OR IGNORE INTO stopped VALUES (?, ?, ?)", (kind, name, now()))
            else:
                db.execute("DELETE FROM stopped WHERE kind = ? AND name = ?", (kind, name))

    @contextmanager
    def dataset_lock(self, name: str) -> Iterator[None]:
        """An exclusive lock on one dataset, shared by every process on this host."""
        directory = self.path.parent / "locks"
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / f"{name}.lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS runner_checkpoints (
    collection TEXT NOT NULL,
    pipeline TEXT NOT NULL,
    step TEXT NOT NULL,
    state TEXT NOT NULL,
    offsets TEXT NOT NULL DEFAULT '{}',
    start_time TEXT,
    end_time TEXT,
    description TEXT,
    stacktrace TEXT,
    PRIMARY KEY (collection, pipeline, step)
);
CREATE TABLE IF NOT EXISTS runner_runs (
    collection TEXT NOT NULL,
    id TEXT NOT NULL,
    pipeline TEXT NOT NULL,
    state TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT,
    error TEXT,
    PRIMARY KEY (collection, id)
);
CREATE TABLE IF NOT EXISTS runner_sessions (
    collection TEXT NOT NULL,
    agent TEXT NOT NULL,
    session_id TEXT NOT NULL,
    data TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (collection, agent, session_id)
);
CREATE TABLE IF NOT EXISTS runner_stopped (
    collection TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    since TEXT NOT NULL,
    PRIMARY KEY (collection, kind, name)
);
"""


class PostgresState:
    """One collection's state in Postgres tables shared by all collections."""

    def __init__(self, url: str, collection: str):
        self.url = url
        self.collection = collection
        with self._connect() as db:
            db.execute(_PG_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(self.url, row_factory=dict_row, autocommit=True) as db:
            yield db

    def _rows(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        with self._connect() as db:
            return db.execute(sql, (self.collection, *params)).fetchall()

    def _exec(self, sql: str, *params: Any) -> int:
        with self._connect() as db:
            return db.execute(sql, (self.collection, *params)).rowcount

    # -- checkpoints -------------------------------------------------------

    def get_checkpoint(self, pipeline: str, step: str) -> dict[str, Any]:
        rows = self._rows(
            "SELECT * FROM runner_checkpoints WHERE collection = %s AND pipeline = %s AND step = %s",
            pipeline,
            step,
        )
        if not rows:
            return {"state": "new", "offsets": {}}
        return _checkpoint(rows[0])

    def set_checkpoint(self, pipeline: str, step: str, **fields: Any) -> None:
        current = self.get_checkpoint(pipeline, step)
        current.update(fields)
        self._exec(
            "INSERT INTO runner_checkpoints VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (collection, pipeline, step) DO UPDATE SET state = excluded.state, "
            "offsets = excluded.offsets, start_time = excluded.start_time, "
            "end_time = excluded.end_time, description = excluded.description, "
            "stacktrace = excluded.stacktrace",
            pipeline,
            step,
            current["state"],
            json.dumps(current.get("offsets") or {}),
            current.get("start_time"),
            current.get("end_time"),
            current.get("description"),
            current.get("stacktrace"),
        )

    def list_checkpoints(self, pipeline: str) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT * FROM runner_checkpoints WHERE collection = %s AND pipeline = %s ORDER BY step",
            pipeline,
        )
        return [_checkpoint(r) for r in rows]

    def reset_checkpoints(self, pipeline: str) -> None:
        self._exec(
            "DELETE FROM runner_checkpoints WHERE collection = %s AND pipeline = %s", pipeline
        )

    # -- runs --------------------------------------------------------------

    def queue_run(self, run_id: str, pipeline: str) -> None:
        self._exec(
            "INSERT INTO runner_runs (collection, id, pipeline, state, start_time) "
            "VALUES (%s, %s, %s, 'queued', %s)",
            run_id,
            pipeline,
            now(),
        )

    def start_run(self, run_id: str, pipeline: str) -> None:
        self._exec(
            "INSERT INTO runner_runs (collection, id, pipeline, state, start_time) "
            "VALUES (%s, %s, %s, 'running', %s) ON CONFLICT (collection, id) "
            "DO UPDATE SET state = excluded.state, start_time = excluded.start_time",
            run_id,
            pipeline,
            now(),
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM runner_runs WHERE collection = %s AND id = %s", run_id)
        return _run(rows[0]) if rows else None

    def interrupt_runs(self) -> int:
        """Mark runs left queued or running by a stopped process as interrupted."""
        with self._connect() as db:
            return db.execute(
                "UPDATE runner_runs SET state = 'interrupted', end_time = %s "
                "WHERE collection = %s AND state IN ('queued', 'running')",
                (now(), self.collection),
            ).rowcount

    def end_run(self, run_id: str, state: str, error: str | None = None) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE runner_runs SET state = %s, end_time = %s, error = %s "
                "WHERE collection = %s AND id = %s",
                (state, now(), error, self.collection, run_id),
            )

    def list_runs(self, pipeline: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT * FROM runner_runs WHERE collection = %s AND pipeline = %s "
            "ORDER BY start_time DESC LIMIT %s",
            pipeline,
            limit,
        )
        return [_run(r) for r in rows]

    # -- agent sessions ----------------------------------------------------

    def get_session(self, agent: str, session_id: str) -> dict[str, Any] | None:
        rows = self._rows(
            "SELECT data FROM runner_sessions WHERE collection = %s AND agent = %s "
            "AND session_id = %s",
            agent,
            session_id,
        )
        return json.loads(rows[0]["data"]) if rows else None

    def save_session(self, agent: str, session_id: str, data: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO runner_sessions VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (collection, agent, session_id) "
            "DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
            agent,
            session_id,
            json.dumps(data, ensure_ascii=False, default=str),
            now(),
        )

    def delete_session(self, agent: str, session_id: str) -> bool:
        return (
            self._exec(
                "DELETE FROM runner_sessions WHERE collection = %s AND agent = %s "
                "AND session_id = %s",
                agent,
                session_id,
            )
            > 0
        )

    def list_sessions(self, agent: str) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT session_id, data, updated_at FROM runner_sessions "
            "WHERE collection = %s AND agent = %s ORDER BY updated_at DESC",
            agent,
        )
        return [_session_summary(r) for r in rows]

    # -- stopped pipelines and agents --------------------------------------

    def stopped(self, kind: str) -> dict[str, str]:
        rows = self._rows(
            "SELECT name, since FROM runner_stopped WHERE collection = %s AND kind = %s", kind
        )
        return {r["name"]: r["since"] for r in rows}

    def set_stopped(self, kind: str, name: str, stopped: bool) -> None:
        if stopped:
            self._exec(
                "INSERT INTO runner_stopped VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                kind,
                name,
                now(),
            )
        else:
            self._exec(
                "DELETE FROM runner_stopped WHERE collection = %s AND kind = %s AND name = %s",
                kind,
                name,
            )

    def delete_all(self) -> None:
        """Forget everything about the collection (it was deleted)."""
        for table in ("runner_checkpoints", "runner_runs", "runner_sessions", "runner_stopped"):
            self._exec(f"DELETE FROM {table} WHERE collection = %s")

    @contextmanager
    def dataset_lock(self, name: str) -> Iterator[None]:
        """An exclusive lock on one dataset, shared by every pod using this database."""
        import psycopg

        key = f"dhub2-runner:{self.collection}:{name}"
        with psycopg.connect(self.url, autocommit=True) as db:
            db.execute("SELECT pg_advisory_lock(hashtext(%s))", (key,))
            try:
                yield
            finally:
                db.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))


def _checkpoint(row: dict[str, Any]) -> dict[str, Any]:
    row = {k: v for k, v in row.items() if k != "collection"}
    return {**row, "offsets": json.loads(row["offsets"])}


def _session_summary(row: dict[str, Any]) -> dict[str, Any]:
    data = json.loads(row["data"])
    messages = data.get("messages") or []
    first = next(
        (m.get("data", {}).get("content") for m in messages if m.get("type") == "human"), None
    )
    return {
        "session_id": row["session_id"],
        "updated_at": row["updated_at"],
        "messages": len(messages),
        "title": str(first)[:80] if first else None,
        "pending": len(data.get("pending") or []),
    }


def _run(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k != "collection"}


def open_state(data_dir: str | Path, collection: str) -> State | PostgresState:
    """A collection's state: in Postgres when RUNNER_DATABASE_URL is set, else
    in ``<data>/state/<collection>/runner.db``."""
    url = os.environ.get("RUNNER_DATABASE_URL")
    if url:
        return PostgresState(url, collection)
    return State(Path(data_dir) / "state" / collection)
