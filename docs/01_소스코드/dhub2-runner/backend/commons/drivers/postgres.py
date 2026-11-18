"""``postgres``: dhub2's builtin Postgres driver.

Options: ``host``, ``port`` (5432), ``database``, ``username``, ``password``
(from ``password_ref``), ``sslmode`` (``prefer``). ``query`` returns a Polars
DataFrame, ``execute`` the affected row count; parameters are positional
(``%s``) as in dhub2.
"""

from __future__ import annotations

from typing import Any

import polars as pl


def connect(options: dict[str, Any], collection: str | None = None) -> Any:
    import psycopg

    missing = [k for k in ("host", "database", "username", "password") if not options.get(k)]
    if missing:
        raise ValueError(f"postgres connector: missing option(s) {missing}")
    return psycopg.connect(
        host=options["host"],
        port=int(options.get("port") or 5432),
        dbname=options["database"],
        user=options["username"],
        password=options["password"],
        sslmode=options.get("sslmode") or "prefer",
        connect_timeout=int(options.get("connect_timeout") or 10),
    )


def query(conn: Any, sql: str, params: list[Any] | None = None) -> pl.DataFrame:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        names = [column.name for column in cur.description or []]
        rows = cur.fetchall()
    conn.commit()  # end the read transaction so the connection does not idle in one
    if not rows:
        return pl.DataFrame(schema={name: pl.String for name in names})
    return pl.DataFrame(rows, schema=names, orient="row", infer_schema_length=None)


def execute(conn: Any, sql: str, params: list[Any] | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        conn.commit()
        return cur.rowcount


def close(conn: Any) -> None:
    conn.close()
