"""The subset of D.Hub for AI's Pipeline model the runner executes.

Unknown fields (layout metadata, run_as, policies, ...) are accepted
and ignored; features the runner cannot execute are refused by
``unsupported_features`` before anything runs.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

DATA_KINDS = ("dataset", "entity", "relation", "source", "view", "variable")
# Kinds a D.Hub for AI pipeline may use that the runner does not run.
UNSUPPORTED_KINDS = {
    "entity": "ontology entities are not supported",
    "relation": "ontology relations are not supported",
    "source": "source (ClickHouse) is not supported",
    "view": "view (ClickHouse) is not supported",
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PipelineData(_Model):
    dataset: str | None = None
    entity: str | None = None
    relation: str | None = None
    source: str | None = None
    view: str | None = None
    variable: str | None = None
    version: int | None = None
    options: dict[str, Any] = {}
    row_filters: Any | None = None
    column_masks: Any | None = None

    @field_validator("options", mode="before")
    @classmethod
    def _none_options(cls, value: Any) -> Any:
        return value or {}

    @property
    def kind(self) -> str | None:
        return next((k for k in DATA_KINDS if getattr(self, k)), None)

    @property
    def ref(self) -> str | None:
        return getattr(self, self.kind) if self.kind else None


class SecretBinding(_Model):
    name: str
    ref: str


class PipelineStep(_Model):
    name: str
    alias: str | None = None
    script: str | None = None
    transform: dict[str, Any] | None = None
    agent: dict[str, Any] | None = None
    engine: str | None = None
    partition: dict[str, Any] | None = None
    inputs: dict[str, PipelineData] = {}
    outputs: dict[str, PipelineData] = {}
    dependencies: list[str] = []
    options: dict[str, Any] = {}
    secrets: list[SecretBinding] = []

    @field_validator("inputs", "outputs", "options", mode="before")
    @classmethod
    def _none_dict(cls, value: Any) -> Any:
        return value or {}

    @field_validator("dependencies", "secrets", mode="before")
    @classmethod
    def _none_list(cls, value: Any) -> Any:
        return value or []

    @model_validator(mode="after")
    def _one_source(self) -> PipelineStep:
        sources = [s for s in ("script", "transform", "agent") if getattr(self, s)]
        if len(sources) != 1:
            raise ValueError(
                f"step {self.name!r} needs exactly one of script, transform or agent "
                f"(got {sources or 'none'})"
            )
        return self


class Pipeline(_Model):
    name: str
    type: Literal["batch", "event"] = "batch"
    steps: list[PipelineStep]
    options: dict[str, Any] = {}

    @field_validator("options", mode="before")
    @classmethod
    def _none_options(cls, value: Any) -> Any:
        return value or {}


def unsupported_features(pipeline: Pipeline) -> list[str]:
    problems = []
    if pipeline.type == "event":
        problems.append("event pipelines (Kafka) are not supported; use type 'batch'")
    for step in pipeline.steps:
        where = f"step {step.name!r}"
        if step.engine not in (None, "polars"):
            problems.append(f"{where}: engine {step.engine!r} is not supported")
        if step.partition:
            problems.append(f"{where}: partitioned steps are not supported")
        for direction, ports in (("input", step.inputs), ("output", step.outputs)):
            for port, data in ports.items():
                if data.kind in UNSUPPORTED_KINDS:
                    problems.append(f"{where} {direction} {port!r}: {UNSUPPORTED_KINDS[data.kind]}")
                if data.options.get("write_mode") == "merge":
                    problems.append(
                        f"{where} {direction} {port!r}: write_mode 'merge' is not supported"
                    )
    return problems
