"""Batch pipeline execution: one step at a time, in dependency order."""

from __future__ import annotations

import logging
import traceback
import uuid
from collections.abc import Callable
from typing import Any

import polars as pl
from commons.collection import Collection
from commons.llm import create_chat_model, resolve_llm
from commons.schema import dtypes
from commons.scripts import SUPPORTED, Resources, close_all, load_function, scan_decorators
from commons.state import State, now
from commons.storage import Storage

from .filters import apply_filters
from .models import Pipeline, PipelineData, PipelineStep, unsupported_features
from .transform import apply_transform

log = logging.getLogger(__name__)


class PipelineError(Exception):
    pass


class RunCancelled(Exception):
    pass


class PipelineRunner:
    def __init__(
        self,
        collection: Collection,
        storage: Storage,
        state: State,
        install_packages: bool = True,
    ):
        self.collection = collection
        self.storage = storage
        self.state = state
        self.resources = Resources(collection, install_packages=install_packages)

    def load(self, name: str) -> Pipeline:
        pipeline = Pipeline.model_validate(self.collection.get("pipeline", name))
        for step in pipeline.steps:
            step.inputs = {p: self._normalize(p, d) for p, d in step.inputs.items()}
            step.outputs = {p: self._normalize(p, d) for p, d in step.outputs.items()}
        problems = unsupported_features(pipeline)
        for step in pipeline.steps:
            problems += _variable_problems(step, self.collection)
            problems += _agent_problems(step, self.collection)
            for port, data in {**step.inputs, **step.outputs}.items():
                kind = (
                    (self.collection.find("dataset", data.dataset) or {}).get("type")
                    if data.dataset
                    else None
                )
                if kind not in (None, "delta"):
                    problems.append(
                        f"step {step.name!r} port {port!r}: dataset {data.dataset!r} is a "
                        f"{kind!r} dataset; only delta datasets are supported"
                    )
            if step.script:
                code = self.collection.get("code", step.script)
                if code.get("type") != "sql":
                    problems += [
                        f"step {step.name!r}: @{d} is not supported"
                        for d, _ in scan_decorators(code.get("content") or "")
                        if d not in SUPPORTED
                    ]
        if problems:
            raise PipelineError(f"pipeline {name!r} cannot run here:\n  " + "\n  ".join(problems))
        return pipeline

    def _normalize(self, port: str, data: PipelineData) -> PipelineData:
        """A port with no target is the dataset named like the port when the
        collection has one, else a variable."""
        if data.kind is None:
            if self.collection.find("dataset", port):
                data.dataset = port
            else:
                data.variable = port
        return data

    def seed(self) -> list[str]:
        """Load the collection's data files into datasets that do not exist yet."""
        seeded = []
        for name, path in self.collection.data_files().items():
            doc = self.collection.find("dataset", name)
            if self.storage.seed((doc or {}).get("name") or name, path, dtypes(doc)):
                log.info("seeded dataset %s from %s", name, path.name)
                seeded.append(name)
        return seeded

    # -- run ---------------------------------------------------------------

    def run(
        self,
        name: str,
        run_id: str | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Run ``name`` once. A pipeline that cannot run is refused before a
        run is recorded, unless the caller already promised a ``run_id``.
        ``cancelled`` is asked before each step; a step that started finishes."""
        if run_id is None:
            self.load(name)
        run_id = run_id or uuid.uuid4().hex[:12]
        self.state.start_run(run_id, name)
        log.info("pipeline %s run %s started", name, run_id)
        try:
            pipeline = self.load(name)
            self.seed()
            self.resources.ensure_packages(pipeline.options.get("packages"))
            contexts: dict[str, Any] = {}
            for step in order_steps(pipeline.steps):
                if cancelled is not None and cancelled():
                    raise RunCancelled(f"cancelled before step {step.name!r}")
                self._run_step(pipeline, step, contexts)
        except RunCancelled as exc:
            self.state.end_run(run_id, "cancelled", str(exc))
            log.info("pipeline %s run %s %s", name, run_id, exc)
            return {"run_id": run_id, "pipeline": name, "state": "cancelled"}
        except BaseException as exc:
            self.state.end_run(run_id, "error", str(exc))
            log.error("pipeline %s run %s failed: %s", name, run_id, exc)
            raise
        self.state.end_run(run_id, "ready")
        log.info("pipeline %s run %s finished", name, run_id)
        return {"run_id": run_id, "pipeline": name, "state": "ready"}

    def _run_step(self, pipeline: Pipeline, step: PipelineStep, contexts: dict[str, Any]) -> None:
        checkpoint = self.state.get_checkpoint(pipeline.name, step.name)
        prev_offsets = checkpoint["offsets"]
        start = now()
        self.state.set_checkpoint(
            pipeline.name, step.name, state="running", start_time=start, end_time=None
        )
        log.info("step %s started", step.name)
        try:
            offsets: dict[str, int] = {}
            inputs = {
                port: self._read(port, data, prev_offsets.get(port, 0), offsets, contexts)
                for port, data in step.inputs.items()
            }
            if step.script:
                outputs = self._run_script(step, inputs, contexts)
            else:
                port = _single_output(step)
                outputs = {port: apply_transform(step.transform, inputs, self._llm)}
            for port, data in step.outputs.items():
                self._write(step, port, data, outputs, contexts)
        except BaseException as exc:
            self.state.set_checkpoint(
                pipeline.name,
                step.name,
                state="error",
                end_time=now(),
                description=str(exc),
                stacktrace=traceback.format_exc(),
            )
            raise
        self.state.set_checkpoint(
            pipeline.name,
            step.name,
            state="ready",
            offsets={**prev_offsets, **offsets},
            end_time=now(),
            description=None,
            stacktrace=None,
        )
        log.info("step %s finished", step.name)

    # -- inputs and outputs ------------------------------------------------

    def _dataset(self, data: PipelineData) -> str:
        """The dataset's name: inputs and outputs may refer to it by id."""
        return (
            (self.collection.find("dataset", data.dataset) or {}).get("name") or data.dataset or ""
        )

    def _read(
        self,
        port: str,
        data: PipelineData,
        offset: int,
        offsets: dict[str, int],
        contexts: dict[str, Any],
    ) -> Any:
        if data.variable:
            return contexts.get(data.variable)
        name = self._dataset(data)
        # Filters and masks read the whole table.
        pushed = bool(data.row_filters or data.column_masks)
        mode = data.options.get("read_mode", "snapshot" if pushed else "cdc")
        if mode == "cdc":
            frame, offsets[port] = self.storage.read_changes(
                name, offset, changes=data.options.get("changes", "inserts")
            )
        elif mode in ("batch", "snapshot"):
            frame = self.storage.read(name, version=data.version)
        else:
            raise PipelineError(f"input {port!r}: unknown read_mode {mode!r}")
        if pushed and frame.columns:
            frame = apply_filters(frame, data.row_filters, data.column_masks)
        return frame

    def _write(
        self,
        step: PipelineStep,
        port: str,
        data: PipelineData,
        outputs: dict[str, Any],
        contexts: dict[str, Any],
    ) -> None:
        if data.variable:
            contexts[data.variable] = outputs.get(port)
            return
        if port not in outputs:
            raise PipelineError(f"step {step.name!r} returned no value for output {port!r}")
        frame = to_frame(outputs[port], f"step {step.name!r} output {port!r}")
        name = self._dataset(data)
        mode = data.options.get("write_mode", "append")
        # Runs of other pipelines may write the same dataset; one writer at a time.
        with self.state.dataset_lock(name):
            self.storage.write(name, frame, mode, partition_by=data.options.get("partition_by"))
        log.info("step %s wrote %d rows to dataset %s (%s)", step.name, frame.height, name, mode)

    # -- step kinds --------------------------------------------------------

    def _run_script(
        self, step: PipelineStep, inputs: dict[str, Any], contexts: dict[str, Any]
    ) -> dict[str, Any]:
        code = self.collection.get("code", step.script)
        source = code.get("content")
        if not source:
            raise PipelineError(f"code {step.script!r} has no source")
        if code.get("type") == "sql":
            sql = pl.SQLContext(
                frames={p: f for p, f in inputs.items() if isinstance(f, pl.DataFrame)}
            )
            result = sql.execute(source, eager=True)
            return {port: result for port in step.outputs}
        namespace, closables = self.resources.namespace(source)
        try:
            run = load_function(source, "run", namespace, f"code:{code.get('name')}")
            step_contexts = dict(contexts)
            step_contexts["secrets"] = {
                s.name: self.collection.secret_value(s.ref) for s in step.secrets
            }
            outputs = run(**inputs, options=step.options, contexts=step_contexts)
            step_contexts.pop("secrets", None)
            contexts.update(step_contexts)
        finally:
            close_all(closables)
        if outputs is None:
            return {}
        if not isinstance(outputs, dict):
            raise PipelineError(f"step {step.name!r}: run() must return a dict of outputs")
        return outputs

    def _llm(self, ref: str) -> Any:
        return create_chat_model(resolve_llm(self.collection, ref))


def order_steps(steps: list[PipelineStep]) -> list[PipelineStep]:
    """Topological order: a step runs after the steps whose outputs it reads
    and the steps it lists in ``dependencies``."""
    writers: dict[tuple[str, str], list[str]] = {}
    for step in steps:
        for data in step.outputs.values():
            writers.setdefault((data.kind or "", data.ref or ""), []).append(step.name)
    by_name = {s.name: s for s in steps}
    after: dict[str, set[str]] = {}
    for step in steps:
        deps = set(step.dependencies)
        for data in step.inputs.values():
            deps.update(writers.get((data.kind or "", data.ref or ""), []))
        deps.discard(step.name)
        unknown = deps - by_name.keys()
        if unknown:
            raise PipelineError(f"step {step.name!r} depends on unknown steps {sorted(unknown)}")
        after[step.name] = deps
    ordered: list[PipelineStep] = []
    done: set[str] = set()
    while len(ordered) < len(steps):
        ready = [s for s in steps if s.name not in done and after[s.name] <= done]
        if not ready:
            cycle = sorted(set(by_name) - done)
            raise PipelineError(f"steps {cycle} form a dependency cycle")
        ordered += ready
        done.update(s.name for s in ready)
    return ordered


def to_frame(value: Any, where: str) -> pl.DataFrame:
    if isinstance(value, pl.LazyFrame):
        return value.collect()
    if isinstance(value, pl.DataFrame):
        return value
    if value is None:
        return pl.DataFrame()
    try:
        if type(value).__module__.startswith("pandas"):
            return pl.from_pandas(value)
        return pl.DataFrame(value)
    except Exception as exc:
        raise PipelineError(f"{where}: cannot turn {type(value).__name__} into a table") from exc


def _single_output(step: PipelineStep) -> str:
    if len(step.outputs) != 1:
        raise PipelineError(f"transform step {step.name!r} needs exactly one output")
    return next(iter(step.outputs))


def _variable_problems(step: PipelineStep, collection: Collection) -> list[str]:
    """SQL and transform steps read and write tables only."""
    if step.transform:
        kind = "transform"
    elif step.script and (collection.find("code", step.script) or {}).get("type") == "sql":
        kind = "SQL"
    else:
        return []
    return [
        f"{direction} variable {port!r} of step {step.name!r}: variable {direction}s are not "
        f"supported for {kind} steps; use a Python step"
        for direction, ports in (("input", step.inputs), ("output", step.outputs))
        for port, data in ports.items()
        if data.variable
    ]


def _agent_problems(step: PipelineStep, collection: Collection) -> list[str]:
    """An agent step calls a workflow agent, as in dhub2; the runner has none."""
    if not step.agent:
        return []
    ref = step.agent.get("agent_id")
    agent = collection.find("agent", ref)
    if agent is None:
        return [f"step {step.name!r}: agent {ref!r} not found"]
    mode = agent.get("mode") or "react"
    if mode != "workflow":
        return [
            f"step {step.name!r}: agent {ref!r} is mode {mode!r}; only workflow agents "
            "declare the variables/outputs contract a pipeline step calls"
        ]
    return [f"step {step.name!r}: workflow agents are not supported"]
