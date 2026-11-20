"""Input ``row_filters`` and ``column_masks``, compiled to Polars expressions.

Same JSON as D.Hub for AI: a row filter is a tree of
``{"kind": "and"|"or", "conditions": [...]}`` groups and
``{"kind": "condition", "column", "operator", "value"}`` leaves; column masks
map a column to ``{"type": ..., "value"?, "length"?, "algorithm"?}`` and also
project the input to the masked columns.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from collections.abc import Mapping
from typing import Any

import polars as pl

PARTIAL_DEFAULT_LENGTH = 4
EMAIL_DEFAULT_LENGTH = 2


def apply_filters(frame: pl.DataFrame, row_filters: Any, column_masks: Any) -> pl.DataFrame:
    if row_filters:
        frame = frame.filter(compile_row_filter(row_filters, frame.schema))
    if column_masks:
        frame = frame.select(compile_column_mask(n, m) for n, m in column_masks.items())
    return frame


def compile_row_filter(node: dict[str, Any], schema: Mapping[str, pl.DataType]) -> pl.Expr:
    if node.get("kind", "and") == "condition":
        return _condition(node, schema)
    children = [compile_row_filter(c, schema) for c in node.get("conditions") or []]
    if not children:
        return pl.lit(True)
    result = children[0]
    for child in children[1:]:
        result = result & child if node.get("kind", "and") == "and" else result | child
    return result


def _typed(value: Any, dtype: pl.DataType | None) -> Any:
    """An ISO text value compared with a date/time column is read as that type."""
    if not isinstance(value, str) or dtype is None:
        return value
    if dtype == pl.Date:
        return dt.date.fromisoformat(value)
    if isinstance(dtype, pl.Datetime):
        return dt.datetime.fromisoformat(value)
    return value


def _condition(cond: dict[str, Any], schema: Mapping[str, pl.DataType]) -> pl.Expr:
    col = pl.col(cond["column"])
    op = cond["operator"]
    dtype = schema.get(cond["column"])
    value = cond.get("value")
    if op in ("in", "not_in", "between"):
        value = [_typed(v, dtype) for v in value]
    elif op != "like":
        value = _typed(value, dtype)
    if op == "eq":
        return col == value
    if op == "ne":
        return col != value
    if op == "gt":
        return col > value
    if op == "gte":
        return col >= value
    if op == "lt":
        return col < value
    if op == "lte":
        return col <= value
    if op == "in":
        return col.is_in(value)
    if op == "not_in":
        return ~col.is_in(value)
    if op == "between":
        return col.is_between(pl.lit(value[0]), pl.lit(value[1]))
    if op == "like":
        return col.cast(pl.String).str.contains(_like_regex(value))
    if op == "is_null":
        return col.is_null()
    if op == "is_not_null":
        return col.is_not_null()
    raise ValueError(f"unknown row filter operator {op!r}")


def _like_regex(pattern: str) -> str:
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        out.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
        i += 1
    return "^" + "".join(out) + "$"


def compile_column_mask(name: str, mask: dict[str, Any]) -> pl.Expr:
    kind = mask.get("type")
    col = pl.col(name)
    if kind == "passthrough":
        return col
    if kind == "null":
        return pl.lit(None).alias(name)
    if kind == "redact":
        return pl.lit(mask.get("value") if mask.get("value") is not None else "****").alias(name)
    default = EMAIL_DEFAULT_LENGTH if kind == "email_local" else PARTIAL_DEFAULT_LENGTH
    length = max(0, int(mask["length"] if mask.get("length") is not None else default))
    functions = {
        "partial_start": lambda v: v[:length] + "*" * max(len(v) - length, 0),
        "partial_end": lambda v: (
            "*" * (len(v) - min(length, len(v))) + v[len(v) - min(length, len(v)) :]
        ),
        "phone_middle": _phone_middle,
        "email_local": lambda v: _email_local(v, length),
        "hash": lambda v: _hash(v, (mask.get("algorithm") or "sha256").lower()),
    }
    if kind not in functions:
        raise ValueError(f"column mask type {kind!r} is not supported on pipeline inputs")
    return col.cast(pl.String).map_elements(functions[kind], return_dtype=pl.String).alias(name)


def _phone_middle(text: str) -> str:
    digits = sum(c.isascii() and c.isdigit() for c in text)
    if digits <= 8:
        hide, out = (digits + 1) // 2, list(text)
        for i in range(len(out) - 1, -1, -1):
            if hide == 0:
                break
            if out[i].isascii() and out[i].isdigit():
                out[i], hide = "*", hide - 1
        return "".join(out)
    middle = text[3 : len(text) - 4]
    return text[:3] + "".join("*" if c.isascii() and c.isdigit() else c for c in middle) + text[-4:]


def _email_local(text: str, length: int) -> str:
    at = text.find("@")
    local, rest = (text, "") if at < 0 else (text[:at], text[at:])
    keep = 0 if len(local) <= length else length
    return local[:keep] + "*" * (len(local) - keep) + rest


def _hash(text: str, algorithm: str) -> str:
    raw = text.encode("utf-8")
    if algorithm == "md5":
        return hashlib.md5(raw, usedforsecurity=False).hexdigest()
    return hashlib.sha256(raw).hexdigest()
