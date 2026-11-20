"""Delta Lake tables, one per dataset.

A dataset lives at ``<root>/<name>``, where the root is a local directory
or an S3 location such as ``s3://dhub-runner/tables`` on RustFS. Tables are
created with the change data feed on, so ``cdc`` inputs can read what changed
since the last run.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl
import pyarrow as pa
from deltalake import DeltaTable, write_deltalake
from deltalake.exceptions import TableNotFoundError

from . import s3
from .schema import conform

CHANGE_COLUMNS = ["_change_type", "_commit_version", "_commit_timestamp"]


class StorageError(Exception):
    pass


class Storage:
    def __init__(self, root: str | Path):
        self.root = str(root).rstrip("/")
        self.options = s3.storage_options() if s3.is_s3(self.root) else None
        if self.options:
            s3.ensure_bucket(s3.split(self.root)[0])

    def uri(self, name: str) -> str:
        return f"{self.root}/{name}"

    def table(self, name: str) -> DeltaTable | None:
        try:
            return DeltaTable(self.uri(name), storage_options=self.options)
        except TableNotFoundError:
            return None

    def read(self, name: str, version: int | None = None) -> pl.DataFrame:
        """The table at ``version`` (latest when None); empty when it does not exist."""
        if self.table(name) is None:
            return pl.DataFrame()
        return pl.read_delta(self.uri(name), version=version, storage_options=self.options)

    def scan(self, name: str, version: int | None = None) -> pl.LazyFrame | None:
        """A lazy scan of the table at ``version`` (latest when None); None when
        it does not exist."""
        if self.table(name) is None:
            return None
        return pl.scan_delta(self.uri(name), version=version, storage_options=self.options)

    def describe(self, name: str) -> dict | None:
        """Version, row count and columns of a dataset; None when it has no table."""
        table = self.table(name)
        if table is None:
            return None
        scan = pl.scan_delta(self.uri(name), storage_options=self.options)
        return {
            "version": table.version(),
            "rows": scan.select(pl.len()).collect().item(),
            "columns": [{"name": n, "type": str(t)} for n, t in scan.collect_schema().items()],
        }

    def read_changes(
        self, name: str, offset: int, changes: str = "inserts"
    ) -> tuple[pl.DataFrame, int]:
        """Rows changed since ``offset`` and the next offset.

        The first read (offset 0) is the whole table. ``changes="inserts"``
        returns inserted rows only; ``"all"`` keeps every change row with its
        ``_change_type`` / ``_commit_version`` / ``_commit_timestamp``."""
        table = self.table(name)
        if table is None:
            return pl.DataFrame(), 0
        current = table.version()
        if offset == 0:
            frame = pl.read_delta(self.uri(name), version=current, storage_options=self.options)
            if changes == "all":
                frame = frame.with_columns(
                    pl.lit("insert").alias("_change_type"),
                    pl.lit(current, pl.Int64).alias("_commit_version"),
                    pl.lit(None, pl.Datetime("ms", "UTC")).alias("_commit_timestamp"),
                )
            return frame, current + 1
        if offset > current:
            empty = pl.scan_delta(self.uri(name), storage_options=self.options).limit(0).collect()
            return empty, offset
        reader = table.load_cdf(starting_version=offset, ending_version=current)
        frame = pl.from_arrow(pa.table(reader))
        assert isinstance(frame, pl.DataFrame)
        if changes == "all":
            frame = frame.sort("_commit_version", maintain_order=True)
        else:
            frame = frame.filter(pl.col("_change_type") == "insert").drop(CHANGE_COLUMNS)
        return frame, current + 1

    def seed(self, name: str, path: Path, declared: dict | None = None) -> bool:
        """Create the table from a data file unless it already exists; columns
        the dataset's schema declares (``declared``) get their declared types."""
        if self.table(name) is not None:
            return False
        if path.suffix == ".csv":
            # Declared text columns stay text ("007" is not the number 7).
            text = {k: v for k, v in (declared or {}).items() if v == pl.String}
            frame = pl.read_csv(path, try_parse_dates=True, schema_overrides=text)
        elif path.suffix == ".json":
            frame = pl.read_json(path)
        else:
            frame = pl.read_parquet(path)
        self.write(name, conform(frame, declared or {}), "append")
        return True

    def write(
        self,
        name: str,
        frame: pl.DataFrame,
        mode: str,
        partition_by: list[str] | None = None,
    ) -> None:
        if not frame.columns:
            return
        # Delta has no null type; an all-null column is stored as a string.
        frame = frame.with_columns(
            pl.col(c).cast(pl.String) for c, t in frame.schema.items() if t == pl.Null
        )
        uri = self.uri(name)
        data = frame.to_arrow()
        table = self.table(name)
        if table is None:
            write_deltalake(
                uri,
                data,
                mode="append",
                partition_by=partition_by,
                configuration={"delta.enableChangeDataFeed": "true"},
                storage_options=self.options,
            )
            return
        if mode == "append":
            write_deltalake(
                uri, data, mode="append", schema_mode="merge", storage_options=self.options
            )
        elif mode == "overwrite":
            write_deltalake(
                uri, data, mode="overwrite", schema_mode="overwrite", storage_options=self.options
            )
        elif mode == "overwrite_partitions":
            columns = partition_by or table.metadata().partition_columns
            if not columns:
                raise StorageError(f"dataset {name}: overwrite_partitions needs partition_by")
            if frame.is_empty():
                return
            predicate = " OR ".join(
                "(" + " AND ".join(f"{_ident(c)} = {_literal(row[c])}" for c in columns) + ")"
                for row in frame.select(columns).unique().to_dicts()
            )
            write_deltalake(
                uri, data, mode="overwrite", predicate=predicate, storage_options=self.options
            )
        else:
            raise StorageError(f"dataset {name}: unsupported write_mode {mode!r}")


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _literal(value: object) -> str:
    if value is None:
        raise StorageError("a partition value cannot be null")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return f"'{value.isoformat()}'"
    return "'" + str(value).replace("'", "''") + "'"
