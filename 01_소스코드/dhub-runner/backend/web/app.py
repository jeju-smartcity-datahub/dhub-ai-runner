"""The runner's web app: a JSON API under /api and the UI in frontend/.

GET    /api/collections                                   collections
POST   /api/collections                                   import (multipart: name, replace, files)
GET    /api/collections/{b}                               what the collection holds and whether it runs
DELETE /api/collections/{b}
GET    /api/secrets                                   shared secrets, and what each collection lacks
PUT    /api/secrets                                   {"values": {"<secret>/<key>": value | null}}
GET    /api/collections/{b}/secrets                       a collection's secret keys and where each is set
PUT    /api/collections/{b}/secrets                       {"values": {"<secret>/<key>": value | null}}
GET    /api/collections/{b}/resources/{kind}/{name}       a resource's definition
GET    /api/operations                                every collection's pipelines and agents, with state
GET    /api/collections/{b}/pipelines/{p}                 steps, checkpoints, runs
POST   /api/collections/{b}/pipelines/{p}/runs            start a run
POST   /api/collections/{b}/runs/{run_id}/cancel
POST   /api/collections/{b}/pipelines/{p}/pause           skip its schedule (it can still be run by hand)
POST   /api/collections/{b}/pipelines/{p}/resume
POST   /api/collections/{b}/pipelines/{p}/reset           forget checkpoints (CDC offsets)
GET    /api/collections/{b}/datasets/{d}                  table, columns, history, lineage, notes
GET    /api/collections/{b}/datasets/{d}/rows             ?version=&limit= rows (time travel)
GET    /api/collections/{b}/datasets/{d}/export           ?version=&format=csv|parquet
PUT    /api/collections/{b}/datasets/{d}/notes            {"owner", "tags", "readme", "columns"}
POST   /api/collections/{b}/query                         {"sql", "max_rows"} one read-only SELECT
GET    /api/collections/{b}/queries                       saved queries
POST   /api/collections/{b}/queries                       {"title", "sql", "id"?}
DELETE /api/collections/{b}/queries/{id}
GET    /api/collections/{b}/agents/{a}                    status, problems and sessions
POST   /api/collections/{b}/agents/{a}/start              (Kubernetes: start its pod)
POST   /api/collections/{b}/agents/{a}/stop               refuse chats; (Kubernetes: remove its pod)
POST   /api/collections/{b}/agents/{a}/chat               {"session_id", "messages"} -> answer
POST   /api/collections/{b}/agents/{a}/chat/stream        the same turn as SSE: tool events, then done
GET    /api/collections/{b}/agents/{a}/sessions/{s}
DELETE /api/collections/{b}/agents/{a}/sessions/{s}
POST   /api/collections/{b}/agents/{a}/invoke             {"inputs"} -> {"output"}

GET    /api/catalog                                   ?q=&collection=&tag=&owner=&has_data=
                                                      &sort=&order=&limit=&offset= search
POST   /api/catalog/sync                              {"collection"?, "force"?} read tables again

The OpenAPI document is /api/openapi.json, with Swagger UI at /api/docs and
ReDoc at /api/redoc.

Notes, saved queries and search keep their data in the catalog, which needs
Postgres (RUNNER_DATABASE_URL); without it they answer 503 and a dataset is
described straight from its Delta log.
"""

from __future__ import annotations

import io
import json
import logging
import os
import queue
import threading
import uuid
from importlib import metadata
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from agent.runtime import AgentError
from agent.tools import Tools
from commons.collection import CollectionError
from commons.catalog import Catalog, CatalogError, datasets, describe
from commons.scripts import missing_secrets
from commons.secrets import SHARED, SecretStoreError
import polars as pl
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pipeline.runner import PipelineError, order_steps

from .runs import Executor, Scheduler, ThreadExecutor
from .schemas import (
    DESCRIPTION,
    TAGS,
    Agent,
    CollectionName,
    ChatRequest,
    Dataset,
    DatasetDetail,
    Deleted,
    InvokeRequest,
    NotesRequest,
    Pipeline,
    QueryRequest,
    QueryResult,
    Rows,
    SavedQuery,
    SavedQueryRequest,
    SearchResult,
    SecretValues,
    SyncRequest,
    SyncResults,
)
from .workspace import Opened, Workspace, collection_info

log = logging.getLogger(__name__)

# The UI lives in the repository's frontend/ (RUNNER_FRONTEND_DIR in the image).
FRONTEND = Path(os.environ.get("RUNNER_FRONTEND_DIR") or Path(__file__).parents[2] / "frontend")
_SECRET_FIELDS = ("api_key", "password", "token", "secret")


def create_app(
    workspace: Workspace,
    executor: Executor | None = None,
    schedule: bool = True,
    max_runs: int = 4,
    pod_agents: Any = None,
    catalog_refresh: float | None = 60,
) -> FastAPI:
    """``executor`` runs pipelines (threads here by default, or Kubernetes Jobs);
    ``pod_agents`` (``kubernetes.KubernetesAgents``) runs agents in their own
    pods instead of this process. The catalog is read again after each run in
    this process and every ``catalog_refresh`` seconds (None: never), which
    picks up what runs in pods wrote."""
    executor = executor or ThreadExecutor(workspace, max_runs=max_runs)
    scheduler = Scheduler(workspace, executor)
    refresher = threading.Event()
    if workspace.catalog is not None and isinstance(executor, ThreadExecutor):
        executor.listeners.append(lambda collection, _: workspace.sync_catalog(collection))

    def refresh_catalog() -> None:
        while True:
            workspace.sync_catalog()
            if refresher.wait(catalog_refresh):
                return

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Runs this process held died with it; runs in pods are still going.
        if isinstance(executor, ThreadExecutor):
            for name in workspace.names():
                try:
                    workspace.open(name).state.interrupt_runs()
                except Exception:  # a broken collection still lets the app start
                    log.exception("collection %s could not be opened", name)
        if schedule:
            scheduler.start()
        if workspace.catalog is not None and catalog_refresh:
            threading.Thread(target=refresh_catalog, name="catalog", daemon=True).start()
        yield
        scheduler.stop()
        refresher.set()

    app = FastAPI(
        title="dhub-runner",
        version=_version(),
        description=DESCRIPTION,
        openapi_tags=TAGS,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    def opened(name: str) -> Opened:
        try:
            return workspace.open(workspace.check_name(name))
        except KeyError as exc:
            raise HTTPException(404, f"collection {name!r} not found") from exc
        except (ValueError, CollectionError) as exc:
            raise HTTPException(422, str(exc)) from exc

    # -- collections -------------------------------------------------------------

    @app.get("/api/collections", tags=["컬렉션"], summary="컬렉션 목록")
    def list_collections() -> list[dict[str, Any]]:
        items = []
        for name in workspace.names():
            try:
                collection = workspace.open(name).collection
                info = collection_info(collection)
                items.append(
                    {
                        "name": name,
                        "alias": info.get("alias"),
                        "description": info.get("description"),
                        "pipelines": len(collection.list("pipeline")),
                        "agents": len(collection.list("agent")),
                        "datasets": len(datasets(collection)),
                    }
                )
            except Exception as exc:  # shown in the list rather than hiding the collection
                items.append({"name": name, "error": str(exc)})
        return items

    @app.post("/api/collections", tags=["컬렉션"], summary="컬렉션 가져오기 (ZIP 또는 리소스 파일)")
    async def import_collection(
        name: str = Form(..., description="컬렉션 이름 (영문, 숫자, _, -, .)"),
        replace: bool = Form(False, description="같은 이름의 컬렉션을 ZIP으로 바꾸기"),
        files: list[UploadFile] = File(
            ..., description="내보낸 컬렉션 ZIP 하나, 또는 리소스 JSON·코드·데이터 파일들"
        ),
    ) -> dict[str, Any]:
        uploads = [(f.filename or "upload", await f.read()) for f in files]
        try:
            report = workspace.import_files(name, uploads, replace=replace)
        except FileExistsError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, CollectionError, json.JSONDecodeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"name": name, **report.as_dict()}

    def agent_states(o: Opened, name: str) -> dict[str, str]:
        """Each agent's state: stopped; running (in this process, or its pod is
        ready); starting (its pod is not ready yet); idle (no pod until it is used)."""
        agents = [d["name"] for d in o.collection.list("agent")]
        stopped = o.state.stopped("agent")
        pods = pod_agents.status(name, agents) if pod_agents is not None else {}
        states = {}
        for a in agents:
            if a in stopped:
                states[a] = "stopped"
            elif pod_agents is not None:
                states[a] = pods.get(a, "idle")
            else:
                states[a] = "running"
        return states

    def pipeline_rows(o: Opened, name: str) -> list[dict[str, Any]]:
        running = executor.active(name)
        paused = o.state.stopped("pipeline")
        rows = []
        for doc in o.collection.list("pipeline"):
            runs = o.state.list_runs(doc["name"], limit=1)
            rows.append(
                {
                    "name": doc["name"],
                    "description": doc.get("description"),
                    "schedule": (doc.get("options") or {}).get("schedule"),
                    "paused": doc["name"] in paused,
                    "steps": len(doc.get("steps") or []),
                    "problems": _pipeline_problems(o, doc["name"]),
                    "last_run": runs[0] if runs else None,
                    "active_run": running.get(doc["name"]),
                }
            )
        return rows

    @app.get("/api/operations", tags=["운영"], summary="모든 컬렉션의 파이프라인·에이전트 상태")
    def operations() -> list[dict[str, Any]]:
        items = []
        for name in workspace.names():
            try:
                o = workspace.open(name)
                states = agent_states(o, name)
                items.append(
                    {
                        "name": name,
                        "alias": collection_info(o.collection).get("alias"),
                        "pipelines": pipeline_rows(o, name),
                        "agents": [
                            {
                                "name": doc["name"],
                                "description": doc.get("description"),
                                "status": states[doc["name"]],
                                "sessions": len(o.state.list_sessions(doc["name"])),
                            }
                            for doc in o.collection.list("agent")
                        ],
                    }
                )
            except Exception as exc:  # shown in the list rather than hiding the collection
                items.append({"name": name, "error": str(exc)})
        return items

    @app.get("/api/collections/{name}", tags=["컬렉션"], summary="컬렉션 내용과 실행 가능 여부")
    def collection_summary(name: CollectionName) -> dict[str, Any]:
        o = opened(name)
        pipelines = pipeline_rows(o, name)
        states = agent_states(o, name)
        agents = []
        for doc in o.collection.list("agent"):
            node = doc.get("agent") or {}
            agents.append(
                {
                    "name": doc["name"],
                    "description": doc.get("description"),
                    "mode": doc.get("mode") or "react",
                    "model": node.get("model"),
                    "tools": [
                        _ref_name(t) for t in node.get("tools") or node.get("tool_names") or []
                    ],
                    "actors": [
                        _ref_name(a) for a in node.get("actors") or node.get("actor_names") or []
                    ],
                    "problems": _agent_problems(o, doc["name"]),
                    "status": states[doc["name"]],
                }
            )
        dataset_rows = []
        for doc in datasets(o.collection).values():
            info = o.storage.describe(doc["name"])
            dataset_rows.append(
                {
                    "name": doc["name"],
                    "description": doc.get("description"),
                    "rows": info["rows"] if info else None,
                    "version": info["version"] if info else None,
                }
            )
        others = {
            kind: [d["name"] for d in o.collection.list(kind)]
            for kind in ("code", "connector", "llm", "tool", "actor", "prompt", "secret")
        }
        return {
            "name": name,
            "collection": collection_info(o.collection),
            "pipelines": pipelines,
            "agents": agents,
            "datasets": dataset_rows,
            "resources": others,
            "missing_secrets": missing_secrets(o.collection),
        }

    @app.delete(
        "/api/collections/{name}",
        tags=["컬렉션"],
        summary="컬렉션 삭제 (테이블, 상태, 시크릿, 카탈로그 포함)",
    )
    def delete_collection(name: CollectionName) -> dict[str, Any]:
        opened(name)
        if executor.active(name):
            raise HTTPException(409, "the collection has runs in progress")
        workspace.delete(name)
        return {"deleted": name}

    # -- variables -----------------------------------------------------------

    @app.get("/api/secrets", tags=["시크릿"], summary="공용 시크릿과 컬렉션마다 빠진 값")
    def all_secrets() -> dict[str, Any]:
        """Shared secrets and variables, and what each collection still lacks."""
        collections = []
        for name in workspace.names():
            try:
                o = workspace.open(name)
                collections.append(
                    {
                        "name": name,
                        "alias": collection_info(o.collection).get("alias"),
                        "secrets": workspace.secret_status(name),
                        "missing": missing_secrets(o.collection),
                    }
                )
            except Exception as exc:  # shown in the list rather than failing the page
                collections.append({"name": name, "error": str(exc)})
        return {"shared": workspace.shared(), "collections": collections}

    @app.put("/api/secrets", tags=["시크릿"], summary="공용 시크릿 설정")
    def put_shared(body: SecretValues) -> dict[str, Any]:
        """Values every collection uses unless it sets its own."""
        try:
            workspace.set_secrets(SHARED, _secret_values(body.values))
        except (ValueError, SecretStoreError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return all_secrets()

    @app.get(
        "/api/collections/{name}/secrets",
        tags=["시크릿"],
        summary="컬렉션 시크릿 키와 값이 오는 곳",
    )
    def get_secrets(name: CollectionName) -> list[dict[str, Any]]:
        opened(name)
        return workspace.secret_status(name)

    @app.put("/api/collections/{name}/secrets", tags=["시크릿"], summary="컬렉션 시크릿 설정")
    def put_secrets(name: CollectionName, body: SecretValues) -> list[dict[str, Any]]:
        """``{"values": {"<secret>/<key>": value | null}}``"""
        opened(name)
        try:
            workspace.set_secrets(name, _secret_values(body.values))
        except (ValueError, SecretStoreError) as exc:
            raise HTTPException(422, str(exc)) from exc
        return get_secrets(name)

    # -- resources -----------------------------------------------------------

    @app.get(
        "/api/collections/{name}/resources/{kind}/{ref}", tags=["리소스"], summary="리소스 정의"
    )
    def get_resource(name: CollectionName, kind: str, ref: str) -> dict[str, Any]:
        o = opened(name)
        try:
            doc = o.collection.find(kind, ref)
        except KeyError as exc:
            raise HTTPException(404, f"unknown resource kind {kind!r}") from exc
        if doc is None:
            raise HTTPException(404, f"{kind} {ref!r} not found")
        return _mask(doc)

    # -- pipelines -----------------------------------------------------------

    @app.get(
        "/api/collections/{name}/pipelines/{pipeline}",
        tags=["파이프라인"],
        summary="스텝, 체크포인트, 실행 이력",
    )
    def pipeline_detail(name: CollectionName, pipeline: Pipeline) -> dict[str, Any]:
        o = opened(name)
        doc = o.collection.find("pipeline", pipeline)
        if doc is None:
            raise HTTPException(404, f"pipeline {pipeline!r} not found")
        checkpoints = {c["step"]: c for c in o.state.list_checkpoints(doc["name"])}
        try:
            steps = order_steps(o.runner.load(doc["name"]).steps)
            problems: list[str] = []
        except PipelineError as exc:
            steps, problems = [], str(exc).splitlines()[1:] or [str(exc)]
        return {
            "name": doc["name"],
            "description": doc.get("description"),
            "schedule": (doc.get("options") or {}).get("schedule"),
            "paused": doc["name"] in o.state.stopped("pipeline"),
            "problems": [p.strip() for p in problems],
            "steps": [
                {
                    "name": s.name,
                    "kind": "script" if s.script else "transform" if s.transform else "agent",
                    "detail": s.script
                    or (s.transform or {}).get("op")
                    or (s.agent or {}).get("agent_id"),
                    "inputs": {p: _port(d) for p, d in s.inputs.items()},
                    "outputs": {p: _port(d) for p, d in s.outputs.items()},
                    "checkpoint": checkpoints.get(s.name),
                }
                for s in steps
            ],
            "runs": o.state.list_runs(doc["name"], limit=20),
            "active_run": executor.active(name).get(doc["name"]),
            "definition": _mask(doc),
        }

    @app.post(
        "/api/collections/{name}/pipelines/{pipeline}/runs", tags=["파이프라인"], summary="실행"
    )
    def start_run(name: CollectionName, pipeline: Pipeline) -> dict[str, Any]:
        opened(name)
        try:
            return {"run_id": executor.submit(name, pipeline)}
        except CollectionError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PipelineError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post(
        "/api/collections/{name}/runs/{run_id}/cancel", tags=["파이프라인"], summary="실행 취소"
    )
    def cancel_run(name: CollectionName, run_id: str) -> dict[str, Any]:
        opened(name)
        if not executor.cancel(name, run_id):
            raise HTTPException(404, "no such run in progress")
        return {"cancelling": run_id}

    @app.post(
        "/api/collections/{name}/pipelines/{pipeline}/reset",
        tags=["파이프라인"],
        summary="체크포인트(CDC 오프셋) 초기화",
    )
    def reset_pipeline(name: CollectionName, pipeline: Pipeline) -> dict[str, Any]:
        o = opened(name)
        if pipeline in executor.active(name):
            raise HTTPException(409, "the pipeline is running")
        o.state.reset_checkpoints(pipeline)
        return {"reset": pipeline}

    def pipeline_name(o: Opened, pipeline: str) -> str:
        doc = o.collection.find("pipeline", pipeline)
        if doc is None:
            raise HTTPException(404, f"pipeline {pipeline!r} not found")
        return doc["name"]

    @app.post(
        "/api/collections/{name}/pipelines/{pipeline}/pause",
        tags=["파이프라인"],
        summary="스케줄 정지",
    )
    def pause_pipeline(name: CollectionName, pipeline: Pipeline) -> dict[str, Any]:
        o = opened(name)
        o.state.set_stopped("pipeline", pipeline_name(o, pipeline), True)
        return {"paused": True}

    @app.post(
        "/api/collections/{name}/pipelines/{pipeline}/resume",
        tags=["파이프라인"],
        summary="스케줄 재개",
    )
    def resume_pipeline(name: CollectionName, pipeline: Pipeline) -> dict[str, Any]:
        o = opened(name)
        o.state.set_stopped("pipeline", pipeline_name(o, pipeline), False)
        return {"paused": False}

    # -- catalog: search across collections ------------------------------------

    def catalog() -> Catalog:
        if workspace.catalog is None:
            raise HTTPException(503, "the catalog needs Postgres: set RUNNER_DATABASE_URL")
        return workspace.catalog

    @app.get(
        "/api/catalog", tags=["카탈로그"], summary="데이터셋 검색", response_model=SearchResult
    )
    def catalog_search(
        q: str | None = Query(
            None,
            description="검색어. 여러 단어는 모두 맞아야 하고, `tag:` `col:` `type:` `owner:` "
            '`collection:`으로 한 필드만 찾습니다 (공백이 있으면 `col:"order id"`)',
            examples=["주문 col:customer_id"],
        ),
        collection: str | None = Query(None, description="이 컬렉션만"),
        tag: list[str] = Query([], description="이 태그가 모두 붙은 것 (여러 번 줄 수 있음)"),
        owner: str | None = Query(None, description="이 소유자 (대소문자 무시)"),
        has_data: bool | None = Query(None, description="테이블이 있는 것(true) / 없는 것(false)"),
        sort: Literal[
            "relevance", "name", "modified", "rows", "size", "version", "collection"
        ] = Query("relevance", description="정렬 기준"),
        order: Literal["asc", "desc"] | None = Query(
            None, description="정렬 방향. 없으면 rows·size·modified는 내림차순, 나머지는 오름차순"
        ),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        try:
            return catalog().search(
                q,
                collection=collection,
                tags=tag,
                owner=owner,
                has_data=has_data,
                sort=sort,
                descending=None if order is None else order == "desc",
                limit=limit,
                offset=offset,
            )
        except CatalogError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post(
        "/api/catalog/sync",
        tags=["카탈로그"],
        summary="동기화 (Delta 로그 다시 읽기)",
        response_model=SyncResults,
    )
    def catalog_sync(body: SyncRequest | None = None) -> dict[str, Any]:
        """Read the tables again: one collection or all; ``force`` also the
        ones whose version did not change."""
        body = body or SyncRequest()
        catalog()
        if body.collection:
            opened(body.collection)
        results = workspace.sync_catalog(body.collection, force=body.force)
        return {"results": results}

    # -- datasets ------------------------------------------------------------

    def dataset_name(o: Opened, dataset: str) -> str:
        """A dataset the collection defines or its pipelines name (by name or id)."""
        name = (o.collection.find("dataset", dataset) or {}).get("name") or dataset
        if name not in datasets(o.collection):
            raise HTTPException(404, f"dataset {dataset!r} not found")
        return name

    def dataset_scan(o: Opened, dataset: str, version: int | None) -> pl.LazyFrame | None:
        try:
            scan = o.storage.scan(dataset_name(o, dataset), version=version)
            if scan is not None:
                scan.collect_schema()  # opens the version
        except HTTPException:
            raise
        except Exception as exc:  # e.g. a version the log does not have
            raise HTTPException(422, f"{type(exc).__name__}: {exc}") from exc
        return scan

    @app.get(
        "/api/collections/{name}/datasets/{dataset}",
        tags=["데이터셋"],
        summary="데이터셋 정보: 테이블, 컬럼 통계, 이력, 계보, 노트",
        response_model=DatasetDetail,
    )
    def dataset_detail(name: CollectionName, dataset: Dataset) -> dict[str, Any]:
        """The table, its columns with statistics, commit history, lineage and
        notes: from the catalog, or from the Delta log without Postgres."""
        o = opened(name)
        dataset = dataset_name(o, dataset)
        if workspace.catalog is None:
            alias = collection_info(o.collection).get("alias")
            return describe(name, o.collection, o.storage, dataset, alias) or {}
        found = workspace.catalog.get(name, dataset)
        if found is None:  # not synced yet
            workspace.sync_catalog(name)
            found = workspace.catalog.get(name, dataset)
        if found is None:
            raise HTTPException(502, f"dataset {dataset!r} could not be read into the catalog")
        return found

    @app.get(
        "/api/collections/{name}/datasets/{dataset}/rows",
        tags=["데이터셋"],
        summary="행 보기 (버전 지정 가능)",
        response_model=Rows,
    )
    def dataset_rows(
        name: CollectionName,
        dataset: Dataset,
        version: int | None = Query(None, ge=0, description="이 버전의 행 (없으면 최신)"),
        limit: int = Query(100, ge=1, le=1000),
    ) -> dict[str, Any]:
        """The first ``limit`` rows, at ``version`` (time travel) or the latest."""
        scan = dataset_scan(opened(name), dataset, version)
        if scan is None:
            return {"name": dataset, "exists": False, "columns": [], "rows": [], "total": 0}
        frame = scan.head(max(1, min(limit, 1000))).collect()
        return {
            "name": dataset,
            "exists": True,
            "version": version,
            "total": scan.select(pl.len()).collect().item(),
            "columns": [{"name": c, "type": str(t)} for c, t in frame.schema.items()],
            "rows": json.loads(json.dumps(frame.rows(), default=str)),
        }

    @app.get(
        "/api/collections/{name}/datasets/{dataset}/export",
        tags=["데이터셋"],
        summary="내려받기 (CSV, Parquet)",
        response_class=Response,
        responses={
            200: {
                "description": "`<데이터셋>[_v<버전>].csv|parquet` 첨부 파일",
                "content": {"text/csv": {}, "application/vnd.apache.parquet": {}},
            }
        },
    )
    def dataset_export(
        name: CollectionName,
        dataset: Dataset,
        version: int | None = Query(None, ge=0, description="이 버전의 테이블 (없으면 최신)"),
        format: Literal["csv", "parquet"] = Query("csv", description="파일 형식"),
    ) -> Response:
        scan = dataset_scan(opened(name), dataset, version)
        if scan is None:
            raise HTTPException(404, f"dataset {dataset!r} has no table yet")
        frame = scan.collect()
        buffer = io.BytesIO()
        if format == "csv":
            # Nested values have no CSV form; they go out as JSON text.
            frame = frame.with_columns(
                pl.col(c).map_elements(_json_text, return_dtype=pl.String)
                for c, t in frame.schema.items()
                if t.is_nested()
            )
            frame.write_csv(buffer)
            media = "text/csv; charset=utf-8"
        else:
            frame.write_parquet(buffer)
            media = "application/vnd.apache.parquet"
        suffix = f"_v{version}" if version is not None else ""
        return Response(
            buffer.getvalue(),
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{dataset}{suffix}.{format}"'},
        )

    @app.put(
        "/api/collections/{name}/datasets/{dataset}/notes",
        tags=["데이터셋"],
        summary="노트 고치기: 소유자, 태그, 문서, 컬럼 메모",
        response_model=DatasetDetail,
    )
    def dataset_notes(name: CollectionName, dataset: Dataset, body: NotesRequest) -> dict[str, Any]:
        """``{"owner", "tags", "readme", "columns": {column: note}}``; keys left out stay."""
        o = opened(name)
        dataset = dataset_name(o, dataset)
        dataset_detail(name, dataset)  # in the catalog before it is annotated
        try:
            catalog().set_notes(name, dataset, body.model_dump(exclude_unset=True))
        except CatalogError as exc:
            raise HTTPException(422, str(exc)) from exc
        return dataset_detail(name, dataset)

    # -- SQL over a collection's datasets ------------------------------------

    @app.post(
        "/api/collections/{name}/query",
        tags=["SQL"],
        summary="SELECT 실행",
        response_model=QueryResult,
    )
    def query(name: CollectionName, body: QueryRequest) -> dict[str, Any]:
        """``{"sql", "max_rows"}``: one read-only SELECT over the collection's
        datasets, run as the text2sql tool runs it."""
        o = opened(name)
        try:
            result = Tools(o.collection, o.storage).analytics_query(
                query=body.sql, max_rows=body.max_rows
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return json.loads(json.dumps(result, default=str))

    @app.get(
        "/api/collections/{name}/queries",
        tags=["SQL"],
        summary="저장한 쿼리 목록",
        response_model=list[SavedQuery],
    )
    def saved_queries(name: CollectionName) -> list[dict[str, Any]]:
        opened(name)
        return catalog().queries(name)

    @app.post(
        "/api/collections/{name}/queries",
        tags=["SQL"],
        summary="쿼리 저장 (id가 있으면 고침)",
        response_model=SavedQuery,
    )
    def save_query(name: CollectionName, body: SavedQueryRequest) -> dict[str, Any]:
        """``{"title", "sql"}`` saves a new query; with ``"id"`` it replaces one."""
        opened(name)
        try:
            return catalog().save_query(name, body.title, body.sql, body.id or None)
        except KeyError as exc:
            raise HTTPException(404, "saved query not found") from exc
        except CatalogError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete(
        "/api/collections/{name}/queries/{query_id}",
        tags=["SQL"],
        summary="저장한 쿼리 삭제",
        response_model=Deleted,
    )
    def delete_query(name: CollectionName, query_id: str) -> dict[str, Any]:
        opened(name)
        if not catalog().delete_query(name, query_id):
            raise HTTPException(404, "saved query not found")
        return {"deleted": query_id}

    # -- agents --------------------------------------------------------------

    def agent(o: Opened, name: str) -> Any:
        try:
            return o.agents.get(name)
        except (AgentError, CollectionError) as exc:
            raise HTTPException(422, str(exc)) from exc

    def agent_name_of(o: Opened, agent_name: str) -> str:
        doc = o.collection.find("agent", agent_name)
        if doc is None:
            raise HTTPException(404, f"agent {agent_name!r} not found")
        return doc["name"]

    def running_agent(o: Opened, agent_name: str) -> Any:
        a = agent(o, agent_name)
        if a.name in o.state.stopped("agent"):
            raise HTTPException(409, f"agent {a.name!r} is stopped; start it first")
        return a

    @app.get(
        "/api/collections/{name}/agents/{agent_name}", tags=["에이전트"], summary="상태, 문제, 세션"
    )
    def agent_detail(name: CollectionName, agent_name: Agent) -> dict[str, Any]:
        o = opened(name)
        a = agent_name_of(o, agent_name)
        return {
            "name": a,
            "description": (o.collection.find("agent", a) or {}).get("description"),
            "status": agent_states(o, name)[a],
            "problems": _agent_problems(o, a),
            "sessions": o.state.list_sessions(a),
        }

    @app.post(
        "/api/collections/{name}/agents/{agent_name}/start", tags=["에이전트"], summary="시작"
    )
    def start_agent(name: CollectionName, agent_name: Agent) -> dict[str, Any]:
        o = opened(name)
        a = agent(o, agent_name)  # 422 for an agent that cannot be built
        o.state.set_stopped("agent", a.name, False)
        if pod_agents is not None:
            try:
                pod_agents.ensure(name, a.name)
            except AgentError as exc:
                raise HTTPException(502, str(exc)) from exc
        return {"status": agent_states(o, name)[a.name]}

    @app.post("/api/collections/{name}/agents/{agent_name}/stop", tags=["에이전트"], summary="중지")
    def stop_agent(name: CollectionName, agent_name: Agent) -> dict[str, Any]:
        o = opened(name)
        a = agent_name_of(o, agent_name)
        o.state.set_stopped("agent", a, True)
        o.agents.drop(a)
        if pod_agents is not None:
            pod_agents.stop(name, a)
        return {"status": "stopped"}

    def chat_turn(
        name: str, agent_name: str, body: dict[str, Any], emit: Callable[[dict], None]
    ) -> dict[str, Any]:
        """One chat turn; tool activity goes to ``emit`` as it happens (from an
        agent pod, once its turn is over)."""
        o = opened(name)
        a = running_agent(o, agent_name)
        session_id = body.get("session_id") or uuid.uuid4().hex[:12]
        messages = body.get("messages") or []
        try:
            if pod_agents is not None:
                result = pod_agents.chat(name, a.name, messages, session_id)
                for event in result.pop("events", None) or []:
                    emit(event)
            else:
                result = o.agents.chat(
                    a,
                    messages,
                    session_id,
                    on_event=lambda kind, data: emit({"type": kind, **data}),
                )
        except AgentError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:  # the model or a tool's backend failed
            log.exception("agent %s/%s failed", name, agent_name)
            raise HTTPException(502, f"{type(exc).__name__}: {exc}") from exc
        return {**result, "session_id": session_id}

    @app.post(
        "/api/collections/{name}/agents/{agent_name}/chat", tags=["에이전트"], summary="대화 한 턴"
    )
    def chat(name: CollectionName, agent_name: Agent, body: ChatRequest) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        result = chat_turn(name, agent_name, body.model_dump(), events.append)
        return {**result, "events": events}

    @app.post(
        "/api/collections/{name}/agents/{agent_name}/chat/stream",
        tags=["에이전트"],
        summary="대화 한 턴 (SSE)",
    )
    def chat_stream(
        name: CollectionName, agent_name: Agent, body: ChatRequest
    ) -> StreamingResponse:
        """SSE: ``{"type": "tool" | "tool_result" | "approval", ...}`` while the
        agent works, then ``{"type": "done", "session_id", "content", "pending"}``
        or ``{"type": "error", "status", "message"}``."""
        running_agent(opened(name), agent_name)  # 404 / 409 before the stream starts
        return StreamingResponse(
            _sse(lambda emit: chat_turn(name, agent_name, body.model_dump(), emit)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get(
        "/api/collections/{name}/agents/{agent_name}/sessions/{session_id}",
        tags=["에이전트"],
        summary="세션 보기",
    )
    def get_session(name: CollectionName, agent_name: Agent, session_id: str) -> dict[str, Any]:
        session = opened(name).agents.session(agent_name, session_id)
        if session is None:
            raise HTTPException(404, "session not found")
        return session

    @app.delete(
        "/api/collections/{name}/agents/{agent_name}/sessions/{session_id}",
        tags=["에이전트"],
        summary="세션 삭제",
    )
    def delete_session(name: CollectionName, agent_name: Agent, session_id: str) -> dict[str, Any]:
        return {"deleted": opened(name).state.delete_session(agent_name, session_id)}

    @app.post(
        "/api/collections/{name}/agents/{agent_name}/invoke",
        tags=["에이전트"],
        summary="한 번 호출 (대화 없이)",
    )
    def invoke(name: CollectionName, agent_name: Agent, body: InvokeRequest) -> dict[str, Any]:
        o = opened(name)
        a = running_agent(o, agent_name)
        if pod_agents is not None:
            return {"output": pod_agents.invoke(name, a.name, body.inputs)}
        return {"output": a.invoke(body.inputs)}

    # -- UI ------------------------------------------------------------------

    if (FRONTEND / "index.html").exists():
        app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(FRONTEND / "index.html")

        @app.get("/chat", include_in_schema=False)
        def chatbot() -> FileResponse:
            """The chatbot: one agent, chosen in the page or by ?collection=&agent=."""
            return FileResponse(FRONTEND / "chat.html")
    else:
        log.warning("no UI at %s (set RUNNER_FRONTEND_DIR); serving the API only", FRONTEND)

    return app


def _sse(run: Callable[[Callable[[dict], None]], dict[str, Any]]) -> Iterator[str]:
    """``run(emit)`` in a thread, its events and result as server-sent events."""
    events: queue.Queue = queue.Queue()

    def worker() -> None:
        try:
            events.put({"type": "done", **run(events.put)})
        except HTTPException as exc:
            events.put({"type": "error", "status": exc.status_code, "message": str(exc.detail)})
        except Exception as exc:  # reported in the stream
            log.exception("chat stream failed")
            events.put({"type": "error", "status": 500, "message": str(exc)})

    threading.Thread(target=worker, daemon=True).start()
    while True:
        event = events.get()
        yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
        if event["type"] in ("done", "error"):
            return


def _secret_values(values: dict[str, Any]) -> dict[tuple[str, str], str | None]:
    """``{"<secret>/<key>": value}`` -> ``{(secret, key): value}``."""
    parsed = {}
    for ref, value in values.items():
        secret, _, key = str(ref).partition("/")
        if not secret or not key:
            raise HTTPException(422, f"{ref!r}: use '<secret>/<key>'")
        parsed[(secret, key)] = value
    return parsed


def _version() -> str:
    try:
        return metadata.version("dhub-runner")
    except metadata.PackageNotFoundError:
        return "dev"


def _json_text(value: Any) -> str:
    return json.dumps(value.to_list() if isinstance(value, pl.Series) else value, default=str)


def _pipeline_problems(o: Opened, name: str) -> list[str]:
    try:
        o.runner.load(name)
        return []
    except (PipelineError, CollectionError) as exc:
        lines = str(exc).splitlines()
        return [line.strip() for line in lines[1:]] or [str(exc)]


def _agent_problems(o: Opened, name: str) -> list[str]:
    try:
        o.agents.get(name)
        return []
    except HTTPException as exc:
        return [str(exc.detail)]
    except Exception as exc:  # what keeps the agent from starting, shown in the UI
        return [str(exc)]


def _ref_name(ref: Any) -> str:
    return ref if isinstance(ref, str) else ref.get("name") or ref.get("id") or "?"


def _port(data: Any) -> dict[str, Any]:
    return {"kind": data.kind, "ref": data.ref, **({"mode": m} if (m := _mode(data)) else {})}


def _mode(data: Any) -> str | None:
    return data.options.get("read_mode") or data.options.get("write_mode")


def _mask(doc: Any) -> Any:
    """A definition with credential-like values replaced."""
    if isinstance(doc, dict):
        return {
            k: "••••" if isinstance(v, str) and v and k.lower() in _SECRET_FIELDS else _mask(v)
            for k, v in doc.items()
        }
    if isinstance(doc, list):
        return [_mask(v) for v in doc]
    return doc
