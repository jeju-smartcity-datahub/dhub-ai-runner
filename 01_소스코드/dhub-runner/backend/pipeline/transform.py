"""Native transform steps: ``stages``, ``join``, ``union`` and ``llm``.

Same JSON as D.Hub for AI. Expression
strings are Polars-SQL expressions parsed with ``pl.sql_expr``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

import polars as pl

log = logging.getLogger(__name__)


class TransformError(Exception):
    pass


DTYPES: dict[str, pl.DataType] = {
    "i8": pl.Int8(),
    "i16": pl.Int16(),
    "i32": pl.Int32(),
    "i64": pl.Int64(),
    "int": pl.Int64(),
    "integer": pl.Int64(),
    "u8": pl.UInt8(),
    "u16": pl.UInt16(),
    "u32": pl.UInt32(),
    "u64": pl.UInt64(),
    "f32": pl.Float32(),
    "float32": pl.Float32(),
    "f64": pl.Float64(),
    "float": pl.Float64(),
    "double": pl.Float64(),
    "bool": pl.Boolean(),
    "boolean": pl.Boolean(),
    "str": pl.String(),
    "string": pl.String(),
    "utf8": pl.String(),
    "date": pl.Date(),
    "datetime": pl.Datetime("us"),
}


def apply_transform(
    spec: dict[str, Any],
    inputs: dict[str, pl.DataFrame],
    llm_factory: Callable[[str], Any] | None = None,
) -> pl.DataFrame:
    op = spec.get("op")
    config = spec.get("config") or {}
    if op == "stages":
        frame = _input(inputs, config.get("input")).lazy()
        for index, stage in enumerate(config.get("pipeline") or []):
            frame = apply_stage(stage, frame, f"stage {index} ({stage.get('op')})")
        return frame.collect()
    if op == "join":
        return _join(config, inputs)
    if op == "union":
        ports = config.get("inputs") or []
        if len(ports) < 2:
            raise TransformError("union needs at least two inputs")
        return pl.concat([_input(inputs, p) for p in ports], how="diagonal")
    if op == "llm":
        if llm_factory is None:
            raise TransformError("llm transform needs an llm factory")
        return _llm(config, inputs, llm_factory)
    raise TransformError(f"unsupported transform op {op!r}")


def _input(inputs: dict[str, pl.DataFrame], port: str | None) -> pl.DataFrame:
    if port not in inputs:
        raise TransformError(f"transform input port {port!r} is not a step input")
    return inputs[port]


def _expr(expr: str, where: str) -> pl.Expr:
    try:
        return pl.sql_expr(expr)
    except Exception as exc:
        raise TransformError(f"{where}: cannot parse expression {expr!r}: {exc}") from exc


def _named(entries: list[dict[str, str]], where: str) -> list[pl.Expr]:
    return [_expr(e["expr"], where).alias(e["name"]) for e in entries]


def apply_stage(stage: dict[str, Any], frame: pl.LazyFrame, where: str) -> pl.LazyFrame:
    op = stage.get("op")
    if op == "select":
        if stage.get("exprs") is not None:
            return frame.select(_named(stage["exprs"], where))
        return frame.select([pl.col(c) for c in stage["columns"]])
    if op == "with_columns":
        return frame.with_columns(_named(stage["exprs"], where))
    if op == "filter":
        return frame.filter(_expr(stage["expr"], where))
    if op == "drop":
        return frame.drop(stage["columns"])
    if op == "rename":
        return frame.rename(stage["map"])
    if op == "cast":
        try:
            return frame.with_columns(
                pl.col(name).cast(DTYPES[dtype]) for name, dtype in stage["dtypes"].items()
            )
        except KeyError as exc:
            raise TransformError(f"{where}: unknown dtype {exc}") from exc
    if op == "sort":
        return frame.sort(stage["by"], descending=stage.get("descending", False), nulls_last=False)
    if op == "unique":
        return frame.unique(subset=stage.get("subset"), keep=stage.get("keep", "any"))
    if op == "head":
        return frame.head(stage["n"])
    if op == "tail":
        return frame.tail(stage["n"])
    if op == "slice":
        return frame.slice(stage["offset"], stage["len"])
    if op == "drop_nulls":
        return frame.drop_nulls(subset=stage.get("subset"))
    if op == "fill_null":
        return frame.fill_null(_expr(stage["expr"], where))
    if op == "with_row_index":
        return frame.with_row_index(stage["name"], offset=stage.get("offset", 0))
    if op == "group_by":
        return frame.group_by(stage["by"]).agg(_named(stage["agg"], where))
    if op == "group_by_dynamic":
        return frame.group_by_dynamic(
            stage["index_column"],
            every=stage["every"],
            period=stage.get("period") or stage["every"],
            offset=stage.get("offset") or "0ns",
            closed=stage.get("closed", "left"),
            group_by=stage.get("by") or None,
        ).agg(_named(stage["agg"], where))
    if op == "explode":
        return frame.explode(stage["columns"])
    if op == "unnest":
        return frame.unnest(stage["columns"])
    if op == "unpivot":
        return frame.unpivot(on=stage["on"], index=stage["index"])
    raise TransformError(f"{where}: unsupported stage op {op!r}")


def _join(config: dict[str, Any], inputs: dict[str, pl.DataFrame]) -> pl.DataFrame:
    left_port, right_port = config["left_input"], config["right_input"]
    left = _input(inputs, left_port).lazy()
    right = _input(inputs, right_port).lazy()
    # Prefix each side so same-named columns stay addressable.
    left = left.rename({c: f"__l__{c}" for c in left.collect_schema().names()})
    right = right.rename({c: f"__r__{c}" for c in right.collect_schema().names()})
    joined = left.join(
        right,
        left_on=[f"__l__{k['left']}" for k in config["keys"]],
        right_on=[f"__r__{k['right']}" for k in config["keys"]],
        how=config.get("join_type", "inner"),
        coalesce=False,
    )
    columns = []
    for col in config["columns"]:
        if col["input"] not in (left_port, right_port):
            raise TransformError(f"join column input {col['input']!r} is not a join input")
        prefix = "__l__" if col["input"] == left_port else "__r__"
        columns.append(pl.col(prefix + col["name"]).alias(col.get("alias") or col["name"]))
    return joined.select(columns).collect()


_PLACEHOLDER = re.compile(r"\{\{\s*([^}\s]+)\s*\}\}")


def _llm(
    config: dict[str, Any],
    inputs: dict[str, pl.DataFrame],
    llm_factory: Callable[[str], Any],
) -> pl.DataFrame:
    """One LLM call per row; ``{{column}}`` placeholders are filled from the row."""
    frame = _input(inputs, config.get("input"))
    max_rows = config.get("max_rows", 1000)
    if frame.height > max_rows:
        raise TransformError(f"llm transform: {frame.height} rows is above max_rows {max_rows}")
    output = config["output"]
    error_column = config.get("error_column") or f"{output['column']}_error"
    model = llm_factory(config["model_id"]).bind(max_tokens=config.get("max_tokens", 512))
    answers, errors = [], []
    for row in frame.iter_rows(named=True):
        messages = []
        if config.get("system"):
            messages.append(("system", _render(config["system"], row)))
        messages.append(("human", _render(config["prompt"], row)))
        try:
            reply = model.invoke(messages)
            answers.append(_parse_answer(_text(reply.content), output))
            errors.append(None)
        except Exception as exc:  # a failed row is recorded, not raised
            answers.append(None)
            errors.append(str(exc))
    dtype = {"string": pl.String, "integer": pl.Int64, "number": pl.Float64, "boolean": pl.Boolean}[
        output.get("type", "string")
    ]
    return frame.with_columns(
        pl.Series(output["column"], answers, dtype=dtype),
        pl.Series(error_column, errors, dtype=pl.String),
    )


def _render(text: str | None, row: dict[str, Any]) -> str:
    return _PLACEHOLDER.sub(
        lambda m: "" if row.get(m.group(1)) is None else str(row[m.group(1)]), text or ""
    )


def _text(content: Any) -> str:
    if isinstance(content, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content)


def _parse_answer(answer: str, output: dict[str, Any]) -> Any:
    answer = answer.strip()
    kind = output.get("type", "string")
    if kind == "integer":
        return int(answer)
    if kind == "number":
        return float(answer)
    if kind == "boolean":
        lowered = answer.lower()
        if lowered not in ("true", "false"):
            raise ValueError(f"not a boolean: {answer!r}")
        return lowered == "true"
    choices = output.get("choices")
    if choices:
        match = next((c for c in choices if c.casefold() == answer.casefold()), None)
        if match is None:
            raise ValueError(f"answer {answer!r} is not one of {choices}")
        return match
    return answer
