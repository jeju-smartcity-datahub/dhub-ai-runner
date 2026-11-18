"""What the web API's OpenAPI document (Swagger UI at /api/docs) says about
requests and responses: request bodies, path parameters, the catalog's
answers, and the tags that group the endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import Path
from pydantic import BaseModel, ConfigDict, Field

DESCRIPTION = """
dhub2에서 내보낸 컬렉션의 파이프라인과 에이전트를 실행하는 런너의 API입니다.

- 컬렉션은 이름으로 가리킵니다. 경로의 `{name}`이 컬렉션 이름입니다.
- 오류는 `{"detail": "..."}`로 옵니다. 없는 대상은 404, 실행할 수 없는 정의나 잘못된 값은 422,
  지금 할 수 없는 동작(실행 중, 중지됨)은 409입니다.
- 카탈로그 검색, 데이터셋 노트, 저장한 쿼리는 Postgres(`RUNNER_DATABASE_URL`)에 저장하며,
  없으면 503을 돌려줍니다.
- 인증이 없습니다. 밖에 공개하려면 앞단에 인증을 두세요.
"""

TAGS = [
    {"name": "컬렉션", "description": "컬렉션을 가져오고, 내용과 상태를 보고, 지웁니다."},
    {
        "name": "시크릿",
        "description": "공용·컬렉션 시크릿. 값은 다시 돌려주지 않고 어디서 오는지만 알려줍니다.",
    },
    {"name": "리소스", "description": "컬렉션에 든 리소스 정의 (자격증명은 가림)."},
    {"name": "운영", "description": "모든 컬렉션의 파이프라인과 에이전트 상태."},
    {
        "name": "파이프라인",
        "description": "파이프라인 실행, 취소, 스케줄 정지·재개, 체크포인트 초기화.",
    },
    {"name": "카탈로그", "description": "모든 컬렉션의 Delta 데이터셋 검색과 동기화 (Postgres)."},
    {
        "name": "데이터셋",
        "description": "데이터셋의 테이블 정보, 컬럼 통계, 이력, 계보, 노트, 버전별 행, 내려받기.",
    },
    {"name": "SQL", "description": "컬렉션 데이터셋에 대한 읽기 전용 SELECT와 저장한 쿼리."},
    {"name": "에이전트", "description": "에이전트 상태, 시작·중지, 대화, 세션, 한 번 호출."},
]

CollectionName = Annotated[str, Path(description="컬렉션 이름", examples=["quickstart"])]
Pipeline = Annotated[str, Path(description="파이프라인 이름 또는 id", examples=["orders"])]
Dataset = Annotated[str, Path(description="데이터셋 이름 또는 id", examples=["orders_raw"])]
Agent = Annotated[str, Path(description="에이전트 이름 또는 id", examples=["sales_assistant"])]


# -- requests ----------------------------------------------------------------


class SecretValues(BaseModel):
    values: dict[str, str | None] = Field(
        description="`<시크릿>/<키>`마다 값. null이나 빈 문자열이면 그 키를 지웁니다.",
        examples=[{"dhub2_llm/base_url": "http://vllm:8000/v1", "dhub2_llm/api_key": None}],
    )


class SyncRequest(BaseModel):
    collection: str | None = Field(None, description="이 컬렉션만. 없으면 모든 컬렉션")
    force: bool = Field(False, description="버전이 그대로인 데이터셋도 다시 읽기")


class NotesRequest(BaseModel):
    """보낸 키만 바뀝니다. 빠진 키는 그대로입니다."""

    owner: str | None = Field(None, description="소유자 (담당자나 팀). null이면 지움")
    tags: list[str] | None = Field(
        None, description="사람이 붙인 태그 (정의의 태그와 합쳐 보임). 대소문자 무시하고 중복 제거"
    )
    readme: str | None = Field(None, description="문서")
    columns: dict[str, str | None] | None = Field(
        None, description="컬럼 메모. 빈 값이나 null이면 그 컬럼의 메모를 지움"
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "owner": "커머스 데이터팀",
                    "tags": ["매출", "대시보드"],
                    "readme": "결제 주문만 고객별로 합산합니다.",
                    "columns": {"amount": "부가세 포함 금액"},
                }
            ]
        },
    )


class QueryRequest(BaseModel):
    sql: str = Field(
        description="SELECT (또는 WITH ... SELECT) 한 문장. Polars SQL, 식별자는 큰따옴표",
        examples=['SELECT status, COUNT(*) AS n FROM "orders_raw" GROUP BY status'],
    )
    max_rows: int = Field(200, ge=1, le=1000, description="돌려줄 최대 행 수")


class SavedQueryRequest(BaseModel):
    id: str | None = Field(None, description="있으면 그 쿼리를 고치고, 없으면 새로 저장")
    title: str = Field(description="이름", examples=["상태별 주문 수"])
    sql: str = Field(examples=['SELECT status, COUNT(*) AS n FROM "orders_raw" GROUP BY status'])


class ChatRequest(BaseModel):
    session_id: str | None = Field(None, description="이어갈 세션. 없으면 새 세션을 만듭니다")
    messages: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "OpenAI 형식의 새 메시지. 승인 대기 중이면 "
            '`{"role": "tool", "tool_call_id": "<id>", "content": "{\\"approved\\": true}"}`'
        ),
        examples=[[{"role": "user", "content": "이번 주 매출을 알려줘"}]],
    )


class InvokeRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict, description="에이전트 입력 변수")


# -- catalog answers ---------------------------------------------------------


class Table(BaseModel):
    collection: str
    name: str
    dataset_id: str | None = None
    alias: str | None = None
    description: str | None = None
    collection_alias: str | None = None
    uri: str = Field(description="Delta 테이블 위치 (로컬 디렉터리 또는 s3://)")
    defined: bool = Field(description="datasets/*.json에 정의가 있는지 (없으면 파이프라인이 쓴 것)")
    has_data: bool = Field(description="Delta 테이블이 있는지")
    version: int | None = None
    rows: int | None = None
    size_bytes: int | None = None
    files: int | None = None
    partition_columns: list[str] = []
    properties: dict[str, str] = Field({}, description="Delta 테이블 속성")
    created_at: datetime | None = None
    modified_at: datetime | None = Field(None, description="마지막 커밋 시각")
    synced_at: datetime | None = Field(None, description="카탈로그가 마지막으로 읽은 시각")
    error: str | None = Field(None, description="테이블을 읽지 못한 이유")
    tags: list[str] = Field([], description="정의의 태그와 노트의 태그")
    owner: str | None = None


class SearchItem(Table):
    score: int = Field(description="관련도 (이름 > 별칭 > 태그 > 컬럼 > 설명)")
    matched_columns: list[str] = Field(description="검색어에 맞은 컬럼")


class Facet(BaseModel):
    value: Any
    label: str | None = None
    count: int


class SearchTotals(BaseModel):
    datasets: int
    rows: int
    size_bytes: int
    with_data: int
    collections: int


class SearchFacets(BaseModel):
    collections: list[Facet]
    tags: list[Facet]
    owners: list[Facet]
    has_data: list[Facet]


class SearchQuery(BaseModel):
    terms: list[str] = Field(description="모두 맞아야 하는 단어")
    fields: dict[str, list[str]] = Field(description="tag, column, type, owner, collection 필터")


class SearchResult(BaseModel):
    query: SearchQuery
    total: int
    totals: SearchTotals = Field(description="조건에 맞는 전체의 합계")
    items: list[SearchItem]
    facets: SearchFacets = Field(
        description="검색어에 맞는 것의 컬렉션·태그·소유자·데이터 유무별 개수"
    )
    synced_at: datetime | None = None


class SyncResult(BaseModel):
    collection: str
    synced: list[str] = Field([], description="다시 읽은 데이터셋")
    removed: list[str] = Field([], description="컬렉션에서 빠져 카탈로그에서 지운 데이터셋")
    error: str | None = None


class SyncResults(BaseModel):
    results: list[SyncResult]


class Column(BaseModel):
    name: str
    type: str | None = None
    nullable: bool | None = None
    description: str | None = Field(None, description="정의(schema.fields)의 설명")
    declared: bool = Field(description="정의에 있는 컬럼인지")
    null_count: int | None = Field(None, description="파일 통계의 null 수")
    min: str | None = None
    max: str | None = None
    note: str | None = Field(None, description="사람이 남긴 메모")


class Commit(BaseModel):
    version: int
    committed_at: datetime | None = None
    operation: str | None = Field(None, description="WRITE, DELETE, MERGE, ...")
    parameters: dict[str, Any] = {}
    metrics: dict[str, Any] = {}


class StepUse(BaseModel):
    pipeline: str
    step: str
    direction: str = Field(description="read 또는 write")
    mode: str | None = Field(None, description="read_mode 또는 write_mode")


class Neighbour(BaseModel):
    dataset: str
    pipeline: str
    step: str


class Lineage(BaseModel):
    writers: list[StepUse] = Field(description="이 데이터셋을 쓰는 스텝")
    readers: list[StepUse] = Field(description="이 데이터셋을 읽는 스텝")
    upstream: list[Neighbour] = Field(description="쓰는 스텝이 읽는 데이터셋")
    downstream: list[Neighbour] = Field(description="읽는 스텝이 쓰는 데이터셋")


class Notes(BaseModel):
    owner: str | None = None
    tags: list[str] = []
    readme: str | None = None
    columns: dict[str, str] = {}
    updated_at: datetime | None = None


class DatasetDetail(Table):
    columns: list[Column]
    history: list[Commit] = Field(description="최근 커밋부터")
    lineage: Lineage
    notes: Notes
    catalog: bool = Field(description="카탈로그(Postgres)에서 온 것인지. false면 노트를 쓸 수 없음")


class Rows(BaseModel):
    name: str
    exists: bool = Field(description="테이블이 있는지")
    version: int | None = Field(None, description="요청한 버전 (null이면 최신)")
    total: int = Field(description="그 버전의 전체 행 수")
    columns: list[dict[str, str]]
    rows: list[list[Any]]


class QueryResult(BaseModel):
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool = Field(description="max_rows보다 많았는지")
    tables: list[str] = Field(description="읽은 데이터셋")
    query: str


class SavedQuery(BaseModel):
    id: str
    title: str
    sql: str
    created_at: datetime
    updated_at: datetime


class Deleted(BaseModel):
    deleted: Any
