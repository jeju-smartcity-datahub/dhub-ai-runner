"""D.Hub for AI dataset schemas (``{"fields": [{"name", "type": {"name": ...}}]}``, the
Arrow-based JSON of D.Hub for AI's ``cores/commons/arrow.py``) as Polars dtypes."""

from __future__ import annotations

from typing import Any

import polars as pl

_SIMPLE = {
    "bool": pl.Boolean,
    "int8": pl.Int8,
    "int16": pl.Int16,
    "int32": pl.Int32,
    "int64": pl.Int64,
    "uint8": pl.UInt8,
    "uint16": pl.UInt16,
    "uint32": pl.UInt32,
    "uint64": pl.UInt64,
    "float16": pl.Float32,
    "float32": pl.Float32,
    "float64": pl.Float64,
    "utf8": pl.String,
    "large_utf8": pl.String,
    "binary": pl.Binary,
    "large_binary": pl.Binary,
    "date32": pl.Date,
    "date64": pl.Date,
}


def dtype(type_json: dict[str, Any]) -> pl.DataType | None:
    """The Polars dtype of a scalar field type; None for nested or unknown ones."""
    name = (type_json or {}).get("name")
    if name in _SIMPLE:
        return _SIMPLE[name]
    if name == "timestamp":
        unit = {"s": "ms", "ms": "ms", "us": "us", "ns": "ns"}.get(type_json.get("unit"), "us")
        return pl.Datetime(unit, type_json.get("timezone"))
    if name in ("decimal", "decimal256"):
        return pl.Decimal(type_json.get("precision"), type_json.get("scale", 0))
    return None


def dtypes(dataset: dict[str, Any] | None) -> dict[str, pl.DataType]:
    """``{column: dtype}`` for the scalar fields a dataset declares."""
    fields = ((dataset or {}).get("schema") or (dataset or {}).get("schema_") or {}).get(
        "fields"
    ) or []
    out = {}
    for field in fields:
        dt = dtype(field.get("type") or {})
        if field.get("name") and dt is not None:
            out[field["name"]] = dt
    return out


def conform(frame: pl.DataFrame, declared: dict[str, pl.DataType]) -> pl.DataFrame:
    """Cast the columns a schema declares to their declared types."""
    casts = [
        pl.col(name).cast(dt, strict=False)
        for name, dt in declared.items()
        if name in frame.columns and frame.schema[name] != dt
    ]
    return frame.with_columns(casts) if casts else frame
