# dhub-runner

D.Hub for AI에서 만든 **파이프라인과 에이전트를 그대로 실행만** 하는 가벼운 러너입니다.
D.Hub for AI에서 내보낸 컬렉션을 가져오면, 인증·권한·클러스터 없이 로컬이나
docker compose로 실행할 수 있습니다.

- **웹 UI (`dhub-runner web`)**: 컬렉션이나 리소스를 가져오고, 파이프라인을 실행·확인하고, 데이터셋을 보고, 에이전트와 대화합니다. 예약된 파이프라인도 여기서 돕니다.
- **카탈로그 (`/#/catalog`)**: 모든 컬렉션의 Delta 데이터셋을 찾고 스키마·통계·이력·계보를 봅니다. 카탈로그는 Postgres에 둡니다.
- **챗봇 (`/chat`)**: 에이전트 하나와 대화하는 화면입니다. 링크로 공유하거나 iframe으로 다른 페이지에 넣을 수 있습니다.
- **pipeline-runner / agent-runner**: UI 없이 컬렉션 하나를 돌리는 명령입니다. 에이전트는 OpenAI 호환 API(`/v1/chat/completions`)로 서비스합니다.

모두 Python으로 되어 있고 UI는 빌드가 필요 없는 정적 HTML/JS입니다. 외부 인프라는 설정(컬렉션)과 데이터(Delta 테이블)를 두는 RustFS 하나만 쓰며, RustFS 없이 로컬 디렉터리만으로도 실행할 수 있습니다.

```
backend/    Python 패키지 (설치하면 dhub-runner 명령)
  cli.py      명령줄
  commons/    컬렉션, 시크릿, LLM, 커넥터, 저장소(Delta), 상태(SQLite), 가져오기
  pipeline/   파이프라인 실행기, transform, 스케줄러
  agent/      ReAct 에이전트, OpenAI 호환 API
  web/        웹 API, 실행기(executor), 스케줄러
frontend/   웹 UI (index.html, app.js, style.css), 챗봇 (chat.html, chat.js, chat.css)
examples/   예제 컬렉션
tests/
```

문서:
- [사용자 매뉴얼](docs/user-manual.md): 웹 화면으로 컬렉션을 가져오고, 파이프라인을 돌리고, 카탈로그를 쓰고, 에이전트와 대화하는 법
- [관리자 매뉴얼](docs/admin-manual.md): 설치, 설정, 저장 위치, 시크릿과 마스터 키, 카탈로그 운영, 보안, 백업, 문제 해결
- API 문서: 실행 중인 웹 앱의 `/api/docs` (Swagger UI), `/api/redoc`, `/api/openapi.json`

## 빠른 시작

### docker compose

```bash
cp .env.example .env          # RustFS·Postgres 접속 정보만 (시크릿은 UI에서 입력)
docker compose up -d --build  # rustfs + web
open http://localhost:8080
```

1. **가져오기**에서 D.Hub for AI에서 내보낸 컬렉션 ZIP(또는 `examples/` 디렉터리를 압축한 ZIP)을 올립니다.
2. 컬렉션의 **시크릿** 탭에서 시크릿과 LLM 키를 입력합니다. 값은 다시 보여주지 않습니다.
3. 파이프라인을 **실행**하고 스텝 상태, 실행 이력, 오류를 확인합니다. 결과 데이터셋은 표로 봅니다.
4. 에이전트와 **대화**합니다. 승인이 필요한 actor는 승인·거부 버튼으로 진행합니다. 지난 세션은 왼쪽 목록에서 다시 열거나 지웁니다.
5. **운영** 화면에서 모든 컬렉션의 파이프라인과 에이전트를 한눈에 보고 관리합니다.
6. **카탈로그**에서 데이터셋을 찾고, 스키마·버전별 데이터·이력·계보를 보고, 문서와 태그를 남기고, SQL로 조회합니다.

| 대상 | 동작 | 뜻 |
|---|---|---|
| 파이프라인 | 실행 / 중지 | 지금 실행합니다. 중지하면 로컬은 진행 중인 스텝이 끝난 뒤 멈추고, Kubernetes는 Job을 지웁니다. |
| 파이프라인 | 스케줄 정지 / 재개 | 예약 실행(`options.schedule`)만 멈춥니다. 직접 실행은 계속 됩니다. 컬렉션은 바뀌지 않고 runner 상태에만 남습니다. |
| 에이전트 | 중지 / 시작 | 중지하면 대화와 호출을 거부합니다. Kubernetes에서는 에이전트 파드도 내리고, 시작하면 파드를 띄워 준비될 때까지 기다립니다. |

컬렉션은 RustFS의 `s3://dhub-runner/collections/<이름>`, 테이블은 `s3://dhub-runner/tables/<이름>`에 쌓입니다. RustFS 콘솔은 http://localhost:9001 입니다.
실행 상태(체크포인트, 실행 이력, 세션), 암호화한 시크릿, 데이터셋 카탈로그는 Postgres에 남습니다.

UI 없이 컬렉션 하나만 돌리려면 `headless` 프로필을 씁니다. `.env`의 `COLLECTION_DIR`, `COLLECTION_NAME`으로 컬렉션을 고릅니다.

```bash
docker compose --profile headless up -d --build   # collection-init → agent-runner(:8000), pipeline-runner
docker compose exec pipeline-runner dhub-runner pipeline run orders
```

### 실행 방식: 로컬은 스레드, Kubernetes는 파드

| | 로컬 / docker compose (기본) | Kubernetes (`RUNNER_EXECUTOR=kubernetes`) |
|---|---|---|
| 파이프라인 실행 | 웹 앱 안의 스레드 풀 (기본 4개) | 실행마다 Job 하나 (`dhub-runner pipeline run`) |
| 에이전트 | 웹 앱 안에서 요청마다 처리 | 에이전트마다 Deployment + Service (`dhub-runner agent serve --agent`). 처음 쓸 때 띄우고, 컬렉션이나 시크릿이 바뀌면 내렸다가 다음에 새로 띄움 |
| 실행 상태·세션·데이터셋 잠금 | SQLite 파일, 파일 잠금 (`RUNNER_DATABASE_URL`이 있으면 Postgres) | Postgres (여러 파드가 함께 봄), Postgres advisory lock |
| 컬렉션·테이블 | 로컬 디렉터리 또는 RustFS | RustFS (`s3://`) |

공통 규칙:
- 같은 파이프라인은 한 번에 하나만 돕니다. 실행 중에 다시 요청하면 같은 실행을 돌려줍니다.
- 같은 데이터셋에 쓰는 실행은 데이터셋별 잠금으로 차례로 씁니다.
- 취소는 로컬에서는 다음 스텝 전에 멈추고, Kubernetes에서는 Job을 지웁니다.
- Kubernetes에서 파드가 실행 결과를 남기지 못하고 끝나면(이미지 문제, 메모리 초과 등) 실패로 기록합니다.

#### Kubernetes에 배포

`deploy/k8s/runner.yaml`에 서비스 계정과 권한(Job·Deployment·Service 관리), 설정, 웹 앱 Deployment가 있습니다. RustFS와 Postgres는 따로 준비합니다.

```bash
docker build -t <registry>/dhub-runner:<tag> . && docker push <registry>/dhub-runner:<tag>
# runner.yaml의 runner-env(인프라 자격증명), runner-config(RUNNER_K8S_IMAGE 등)를 채우고,
# Deployment dhub-runner-web의 image를 위에서 올린 <registry>/dhub-runner:<tag>로 바꾼 뒤
kubectl apply -n <namespace> -f deploy/k8s/runner.yaml
```

| 설정 | 뜻 |
|---|---|
| `RUNNER_EXECUTOR=kubernetes` | 파이프라인과 에이전트를 파드로 실행 |
| `RUNNER_K8S_IMAGE` | 실행·에이전트 파드의 이미지 (웹 앱과 같은 이미지) |
| `RUNNER_K8S_NAMESPACE` | 파드를 만들 네임스페이스 (없으면 웹 앱 파드의 네임스페이스) |
| `RUNNER_K8S_ENV_FROM` | 파드에 넘길 인프라 설정, 예: `secret/runner-env,configmap/runner-config` |
| `RUNNER_K8S_SERVICE_ACCOUNT` | 실행·에이전트 파드의 서비스 계정 (선택) |

Kubernetes 모드에서는 `RUNNER_DATABASE_URL`, `RUNNER_SECRET_KEY`, `s3://` 위치의 `RUNNER_COLLECTIONS`·`RUNNER_TABLES`가 필수입니다. 파드마다 따로 만든 키로는 서로의 시크릿을 읽을 수 없기 때문입니다.

### 로컬

```bash
uv sync
uv run dhub-runner --data data web            # http://127.0.0.1:8080, 컬렉션은 data/collections

# UI 없이 컬렉션 하나
export RUNNER_COLLECTION_DIR=examples/quickstart RUNNER_DATA_DIR=data
uv run dhub-runner check                      # 컬렉션 검증 + 값이 없는 시크릿 확인
echo -n "$KEY" | uv run dhub-runner secret set dhub_llm/api_key   # 시크릿 입력 (--shared: 공용)
uv run dhub-runner pipeline run orders
uv run dhub-runner agent chat sales_assistant # 터미널 대화 (승인 프롬프트 포함)
uv run dhub-runner agent serve --port 8000
```

로컬에서 RustFS(또는 다른 S3 호환 저장소)를 쓰려면 표준 AWS 변수와 위치를 지정합니다.

```bash
export AWS_ENDPOINT_URL=http://localhost:9000 AWS_ACCESS_KEY_ID=rustfsadmin AWS_SECRET_ACCESS_KEY=rustfsadmin
export RUNNER_COLLECTION_DIR=s3://dhub-runner/collections/quickstart RUNNER_TABLES=s3://dhub-runner/tables/quickstart
uv run dhub-runner collection push examples/quickstart
uv run dhub-runner pipeline run orders
```

## 시크릿

시크릿은 `secret://<이름>/<키>`(또는 문자열 안의 `${secret:<이름>/<키>}`)로 참조하고, 값은 컬렉션이 아니라 **시크릿 저장소**(`backend/commons/secrets.py`의 `SecretStore`)에 둡니다.
**시크릿은 환경변수로 넘기지 않습니다.** 환경변수로 받는 것은 runner 자신의 인프라 접속 정보(RustFS `AWS_*`, Postgres `RUNNER_DATABASE_URL`, 마스터 키 `RUNNER_SECRET_KEY`)뿐입니다.

| 저장소 | 언제 | 비고 |
|---|---|---|
| `PostgresSecretStore` | `RUNNER_DATABASE_URL`이 있을 때 (docker compose 기본) | `runner_secrets` 테이블, 값은 마스터 키로 암호화한 뒤 저장 |
| `FileSecretStore` | 그 밖의 로컬 실행 | 컬렉션마다 `<data>/state/<컬렉션>/secrets.enc`, 공용은 `<data>/shared/secrets.enc` (Fernet 암호화) |

- **컬렉션 시크릿과 공용 시크릿**: 컬렉션 값이 먼저 쓰이고, 없으면 공용 값을 씁니다. 여러 컬렉션이 함께 쓰는 LLM 엔드포인트 같은 값은 공용에 넣습니다.
- **입력**: 웹 UI 사이드바의 **시크릿**(공용), 컬렉션의 **시크릿** 탭(컬렉션), 또는 `dhub-runner secret set <이름>/<키> [--shared]`(값은 표준입력). 한 줄에 `<이름>/<키>=값`으로 여러 개를 한 번에 붙여 넣을 수 있습니다. 값은 다시 보여주지 않고 어디서 오는지(컬렉션, 공용, 없음)만 보여줍니다.
- **가져오기**: export에 없는 시크릿은 `requirements.secrets`와 쓰이는 `secret://` 참조에서 찾아 "입력이 필요한 시크릿"으로 보여줍니다. 키가 빠진 LLM 모델에는 `api_key_ref: secret://<모델>/api_key`를 붙입니다. D.Hub for AI에서 키 없이 쓰던 모델(`has_api_key: false`)은 그대로 둡니다.
- **환경변수 방식의 설정은 거부**: `api_key_env`, `credentials_env`처럼 환경변수에서 값을 읽는 설정은 이유와 함께 거부합니다. `api_key_ref`, `<옵션>_ref`를 쓰세요.
- **마스터 키**: `RUNNER_SECRET_KEY`입니다. 없으면 처음 실행할 때 `<data>/secret.key`(권한 0600)를 만들며, 키를 잃으면 저장된 시크릿을 읽을 수 없습니다.
- **다른 저장소**: k8s Secret 등은 `SecretStore`를 구현해 `Workspace(secret_store=...)`로 넘기면 됩니다.

## 리소스별 지원

D.Hub for AI 리소스 JSON(API 응답, 내보내기의 `manifests/`)을 그대로 받습니다. 가져오기 화면에서 리소스 파일을 하나씩 올려도 됩니다.

| 리소스 | 지원 | 지원하지 않음 (이유와 함께 거부) |
|---|---|---|
| 데이터셋 (`dataset`) | `delta`. 스키마(`schema.fields`)대로 초기 데이터의 타입을 맞춤 | `object`, `kafka`, `rest` 데이터셋 |
| 코드 (`code`) | `python`(`run`), `sql`(Polars SQL). 본문은 `<이름>.py`/`.sql` | |
| 파이프라인 (`pipeline`) | 아래 "파이프라인" 참고 | |
| 커넥터 (`connector`) | 스크립트 커넥터, 내장 드라이버 `http`(none·basic·bearer·api_key·OAuth2 client credentials), `postgres`. 자격증명은 `<옵션>_ref: secret://...` | `mcp`, `clickhouse`, `qdrant`, `dhub2_manager_api`, `datahub`, `semantic`, `neo4j`, `oracle`, `ngsild` |
| LLM 모델 (`llm.model`) | `openai`(호환 엔드포인트 포함), `claude`, `gemini`. 키는 시크릿(`api_key_ref`, 없으면 `<모델>/api_key`). 시크릿 `<모델>/base_url`이 있으면 컬렉션을 고치지 않고 주소를 바꿈 | D.Hub for AI가 직접 배포한 모델(`type: internal`)은 `<모델>/base_url`을 넣어야 실행 |
| 에이전트 (`agent`) | 아래 "에이전트" 참고 | |
| 툴·액터 (`agent.tool`, `agent.actor`) | `python`(`execute`), 데이터셋 builtin 툴(아래 "데이터셋 조회 (text2sql)") | `mcp` 툴, `agent`·`workflow` 툴(건너뜀, roles 사용) |
| 프롬프트 (`prompt`) | `content`와 `variables` | `long_context` 문서(경고 후 본문만 사용) |
| 시크릿 (`secret`) | 키 이름(`keys`). 값은 runner 시크릿 저장소 | |

코드에서 쓰는 데코레이터는 `@use_connector`, `@use_connectors`, `@use_llm`을 지원합니다.

## 컬렉션 디렉터리

러너는 컬렉션 하나를 디렉터리 하나에 둡니다. 리소스 JSON 파일을 종류별 디렉터리에 나눠 둔 것입니다.
웹 앱에서는 모든 컬렉션이 `RUNNER_COLLECTIONS/<컬렉션 이름>/`에 있고, 명령줄에서는 `--collection-dir`로 하나를 가리킵니다.
리소스는 **이름**으로 참조하며, 내보낸 manifest처럼 `id`로 참조해도 찾습니다.

```
pipelines/*.json             파이프라인
codes/*.json + <name>.py|.sql  코드 (또는 content_file)
connectors/*.json            스크립트 커넥터 (script 또는 script_file)
secrets/*.json               {"name", "keys": ["<key>", ...]} (값은 시크릿 저장소에)
llm/*.json                   LLMModel (api_key_ref: secret://...)
datasets/*.json              데이터셋 (이름만 있어도 됨)
data/<dataset>.csv|.json|.parquet, data/parquet/*.parquet   초기 데이터
agents/*.json  tools/*.json + .py  actors/*.json + .py  prompts/*.json
```

- 시크릿 값은 컬렉션에 넣지 않습니다. 시크릿 저장소에서 읽습니다(위 "시크릿").
- `data/`의 파일은 같은 이름의 데이터셋이 아직 없을 때 파이프라인 실행 전에 적재됩니다(`pipeline seed`로 직접 적재도 가능).
- 커넥터·LLM 설정 문자열에는 `${secret:<secret>/<key>}`를 쓸 수 있고, 커넥터 옵션 `<이름>_ref: secret://...`는 `<이름>` 옵션으로 풀립니다.
- 스텝 포트에 대상이 없으면, 포트 이름과 같은 데이터셋이 컬렉션에 있을 때는 그 데이터셋으로, 없으면 변수(variable)로 처리합니다.

### D.Hub for AI 컬렉션 가져오기

D.Hub for AI 포탈에서 컬렉션을 내보낸 ZIP을 받아 러너의 컬렉션 디렉터리로 바꿉니다. 압축을 푼 디렉터리를 줘도 됩니다.

```bash
dhub-runner import demo-export.zip collections/demo                              # 로컬 컬렉션
dhub-runner import demo-export.zip s3://dhub-runner/collections/demo             # RustFS에 바로

# docker compose에서
docker compose run --rm -v ./demo-export.zip:/export.zip:ro collection-init \
  import /export.zip s3://dhub-runner/collections/demo
```

**가져오는 것**
- `manifest.json`에 적힌 것과 각 디렉터리에 있는 데이터셋, 코드(`<name>.py` 본문 포함, SQL 코드는 `.sql`로 저장), 파이프라인
- `manifests/`의 정의 리소스: 에이전트, 툴, actor, LLM 모델, 프롬프트, 커넥터, 시크릿
- `data/`의 초기 데이터. 첫 실행 때 해당 데이터셋에 적재됩니다.

**시크릿**
- 시크릿 값은 export에 없습니다. 쓰이는 `secret://<시크릿>/<키>`와 `requirements.secrets`마다 `secrets/*.json`(키 목록)을 만듭니다.
- 키가 빠진 LLM 모델에는 `api_key_ref: secret://<모델>/api_key`를 붙입니다. D.Hub for AI에서 키 없이 쓰던 모델(`has_api_key: false`)은 그대로 둡니다.
- 값은 가져온 뒤 웹 UI나 `dhub-runner secret set`으로 입력합니다.

**결과 보고**
- 명령이 끝나면 가져온 리소스, 건너뛴 리소스(폴더, 대시보드, 지식 등 러너가 실행하지 않는 종류), 적재할 데이터, 입력이 필요한 시크릿을 JSON으로 출력합니다.
- 이어서 `dhub-runner check`를 돌리면 러너가 지원하지 않는 기능을 쓰는 파이프라인과 에이전트를 알 수 있습니다.

## 파이프라인

지원:
- **스텝**
  - `script` (Python `run(**inputs, options, contexts)`, Polars SQL)
  - `transform` (`stages` 19종, `join`, `union`, `llm`)
- **LLM 호출**: Python 스텝에서 `@use_llm`, transform의 `llm`
- **변수**: Python 스텝만 변수를 주고받습니다. SQL과 transform 스텝은 테이블만 읽고 씁니다.
- **입력**: dataset, `read_mode` `cdc`(기본) · `batch` · `snapshot`, 변수
- **출력**: dataset `append`(기본) · `overwrite` · `overwrite_partitions`, 변수
- **시크릿 주입**: `steps[].secrets` → `contexts["secrets"]`
- **패키지 설치**: `options.packages`, 커넥터 `packages`
- **입력 필터**: `row_filters`, `column_masks` (passthrough, null, redact, partial_start/end, phone_middle, email_local, hash)
- **데코레이터**: `@use_connector`, `@use_connectors`, `@use_llm`

지원하지 않음 (실행 전에 이유와 함께 거부합니다):
- 온톨로지 (entity/relation 입출력, `@use_ontology`/`@use_walker`)
- event 파이프라인(Kafka)
- `engine: spark`
- source/view(ClickHouse) 입력
- `write_mode: merge`
- `partition`
- 그 밖의 `@use_*` 데코레이터
- `rrule` 스케줄
- `agent` 스텝: D.Hub for AI는 workflow 에이전트만 스텝에서 부를 수 있고, 러너는 workflow 에이전트를 지원하지 않습니다. LLM 호출은 `@use_llm` 코드 스텝이나 transform `llm`으로 합니다.
- 일부 내장 드라이버 커넥터 (위 "리소스별 지원" 참고)

스텝은 의존 순서대로 하나씩 실행합니다. 스텝별 상태와 CDC 오프셋은 `pipeline status`로 보고,
`pipeline reset`으로 초기화합니다.

## 에이전트

지원:
- `mode: react`
- python tools/actors (`execute(**inputs)`)
- actor `confirmation_policy` (`confirm`이면 승인 대기, `deny`면 제외)
- `roles` (`delegate_to_<role>` 툴로 위임)
- `prompts`, `conversations` 변수, `outputs` 구조화 출력
- `settings.fixed_inputs` / `guidance`
- `function.decorators` (소스의 `@use_*` 설정을 덮어씀)
- 예전 필드명 `tool_names` / `actor_names`
- 데이터셋 builtin 툴로 컬렉션의 Delta 테이블을 SQL로 조회 (아래 "데이터셋 조회 (text2sql)")

지원하지 않음 (실행 전에 이유와 함께 거부합니다):
- semantic layer (`tables`, `templates`, `ontologies`와 semantic 툴)
- workflow 모드
- MCP 툴
- D.Hub for AI API에 의존하는 builtin 툴 (데이터셋 builtin 툴은 예외)
- 지식(RAG)
- 컨텍스트 압축

### 데이터셋 조회 (text2sql)

에이전트의 `tools`에 아래 이름(또는 D.Hub for AI builtin id `agent.tool.<이름>`)을 넣으면, 러너가 컬렉션의 Delta 테이블(RustFS `s3://.../tables/<컬렉션>/<데이터셋>` 또는 로컬 디렉터리)을 직접 읽어 답합니다.
D.Hub for AI에서는 같은 툴이 D.Hub for AI API와 ClickHouse 미러로 답하므로, 이 툴을 쓰는 D.Hub for AI 에이전트는 내보낸 그대로 실행됩니다.

| 툴 | 하는 일 |
|---|---|
| `resources_search` / `datasets_search` | 컬렉션의 데이터셋 목록 (이름·별칭 필터, 데이터가 있는지 `has_data`). `dataset` 외의 종류는 거부 |
| `analytics_source` | 데이터셋의 테이블 이름(`qualified_table`)과 컬럼(타입, 스키마에 적힌 설명) |
| `analytics_query` | SELECT 하나를 실행해 행을 돌려줌 (기본 100행, `max_rows`로 최대 1000행, 넘으면 `truncated: true`) |

- SQL은 Polars SQL(PostgreSQL에 가까움)입니다. CTE, 서브쿼리, 집계, 윈도 함수, 컬렉션 데이터셋끼리의 조인을 쓸 수 있습니다.
- `SELECT`(또는 `WITH ... SELECT`) 한 문장만 받습니다. 쿼리는 테이블을 지연 스캔해 읽기만 하고 Delta 테이블에 쓰지 않습니다.
- 읽을 수 있는 것은 컬렉션에 있는 데이터셋(`datasets/*.json`)뿐입니다. 파이프라인이 아직 쓰지 않은 데이터셋은 조회할 수 없습니다.
- 컬렉션에 같은 이름의 컬렉션 툴(`is_builtin`이 아닌 정의)이 있으면 그 툴을 씁니다.
- 참조의 `settings.guidance`, `settings.fixed_inputs`, `options.timeout_seconds`가 적용됩니다.

```json
"agent": {
  "model": "gemma4",
  "instructions": "데이터셋을 SQL로 조회해 답합니다.",
  "tools": ["resources_search", "analytics_source", "analytics_query"]
}
```

### 챗봇

웹 앱의 `/chat`은 에이전트와 대화하는 화면입니다. 에이전트 화면의 **챗봇 열기**로도 엽니다.

- 답을 마크다운(표, 코드, 목록)으로 보여주고, 툴 실행(text2sql의 SQL 포함)을 단계별로 실시간 표시합니다.
- 승인이 필요한 actor는 승인·거부 버튼으로 진행합니다. 지난 대화는 왼쪽 목록에서 다시 열거나 지웁니다.
- `/chat?collection=<컬렉션>&agent=<에이전트>`로 에이전트를 고정하고, `&embed=1`을 붙이면 목록 없이 대화만 보여 iframe에 넣기 좋습니다. `&session=<id>`는 지난 대화를 엽니다. `&theme=light|dark`도 됩니다.
- 스트림은 `POST /api/collections/<컬렉션>/agents/<에이전트>/chat/stream`(SSE)입니다. 툴 이벤트(`tool`, `tool_result`, `approval`)가 오고 `done`(답, 승인 대기) 또는 `error`로 끝납니다. Kubernetes 모드에서는 에이전트 파드의 대화가 끝난 뒤 이벤트를 한꺼번에 보냅니다.
- 웹 UI와 마찬가지로 인증이 없습니다. 밖에 공개하려면 앞단에 인증을 두세요.

API:

| | |
|---|---|
| `POST /v1/chat/completions` | `model`은 에이전트 이름 (에이전트가 하나면 생략 가능). `X-Session-Id`를 주면 서버가 대화 이력을 보관하고, `X-Session-Mode: ephemeral`이면 저장하지 않습니다. `stream: true`이면 SSE로 보내고, 툴 진행 상황은 `delta.tool_progress`로 전달합니다. |
| `GET /v1/sessions/{id}?agent=` / `DELETE` | 세션 조회와 삭제 |
| `POST /v1/agents/{name}/invoke` | `{"inputs": {...}}` → `{"output": ...}`. 대화 없이 한 번 호출하고, `outputs`가 있으면 그 형식으로 받습니다. |
| `GET /v1/models`, `GET /health` | |

승인이 필요한 actor 호출이 나오면 응답이 `finish_reason: "tool_calls"`로 끝나고 `message.tool_calls`가 담깁니다.
같은 세션으로 `{"role": "tool", "tool_call_id": "<id>", "content": "{\"approved\": true}"}`를 보내면 이어서 진행합니다.

## 데이터셋 카탈로그

모든 컬렉션의 Delta 데이터셋을 한곳에서 찾고 살펴봅니다. 사이드바의 **카탈로그**에서 열고, 데이터셋을 누르면 컬렉션의 데이터셋 화면(`#/collections/<컬렉션>/datasets/<데이터셋>`)으로 갑니다.
컬렉션 화면의 데이터셋 목록도 같은 화면으로 이어집니다.

카탈로그는 **Postgres**(`RUNNER_DATABASE_URL`, docker compose 기본)의 `runner_catalog_*` 테이블에 둡니다.

| 테이블 | 담는 것 |
|---|---|
| `runner_catalog_tables` | 데이터셋: 위치, 버전, 행·크기·파일 수, 파티션, 테이블 속성, 생성·변경 시각, 정의의 별칭·설명·태그 |
| `runner_catalog_columns` | 컬럼: 타입, null 허용, 정의의 설명, 파일 통계를 모은 null 수·최소·최대 |
| `runner_catalog_history` | Delta 커밋 이력 (데이터셋마다 최근 200개) |
| `runner_catalog_lineage` | 데이터셋을 읽고 쓰는 파이프라인 스텝 |
| `runner_catalog_notes` | 사람이 남긴 것: 소유자, 태그, 문서, 컬럼 메모. 동기화로 바뀌지 않습니다 |
| `runner_catalog_queries` | 컬렉션별로 저장한 SQL |

**동기화**
- Delta 로그만 읽고 데이터 파일은 읽지 않습니다. 버전과 정의가 그대로인 데이터셋은 건너뜁니다.
- 컬렉션을 가져올 때, 웹 앱 안의 파이프라인 실행이 끝날 때, 그리고 1분마다 돕니다. Kubernetes Job이 쓴 테이블도 1분 안에 반영됩니다. 카탈로그의 **동기화**, 데이터셋의 **다시 읽기**로 바로 돌릴 수 있습니다.
- 컬렉션을 지우면 카탈로그에서도 지웁니다(문서와 저장한 쿼리 포함).
- 컬렉션의 데이터셋은 `datasets/*.json`에 정의된 것과, 정의 없이 파이프라인이 이름으로 쓰는 것입니다.

**검색**
- 여러 단어는 모두 맞아야 하며 이름, 별칭, 설명, 컬럼 이름·설명·메모, 태그, 소유자, 문서에서 찾습니다. 이름에 맞은 것이 먼저 나옵니다.
- 필드 검색: `tag:매출`, `col:customer_id`, `type:datetime`, `owner:kim`, `collection:demo`. 값에 공백이 있으면 `col:"order id"`.
- 컬렉션·태그·소유자·데이터 유무로 거르고(개수 표시), 관련도·이름·최근 변경·행 수·크기·버전으로 정렬합니다.

**데이터셋 화면**

| 탭 | 내용 |
|---|---|
| 개요 | 문서, 속성(위치, 파티션, 테이블 속성, 시각), 소유자·태그·문서 편집 |
| 스키마 | 컬럼별 타입, null 허용, 설명, null 수(비율), 최소·최대, 컬럼 메모 편집 |
| 데이터 | 버전을 골라 그 시점의 행 보기(time travel), CSV·Parquet 내려받기 |
| 이력 | 커밋마다 작업, 모드, 추가 행, 파일 수, "이 버전 보기" |
| 계보 | 상위 데이터셋 → 쓰는 스텝 → 이 데이터셋 → 읽는 스텝 → 하위 데이터셋 |
| SQL | 컬렉션 데이터셋에 대한 읽기 전용 `SELECT` (text2sql 툴과 같은 Polars SQL), 쿼리 저장 |

Postgres가 없으면 데이터셋 화면은 Delta 로그를 바로 읽어 보여주고, 검색·문서·태그·저장한 쿼리는 쓸 수 없습니다(API는 503).

API:

| | |
|---|---|
| `GET /api/catalog` | `q`, `collection`, `tag`(여러 번), `owner`, `has_data`, `sort`(`relevance` `name` `modified` `rows` `size` `version` `collection`), `order`(`asc`/`desc`), `limit`, `offset` → `items`, `total`, `totals`, `facets` |
| `POST /api/catalog/sync` | `{"collection"?, "force"?}` |
| `GET /api/collections/{b}/datasets/{d}` | 테이블, 컬럼과 통계, 이력, 계보, 노트 |
| `GET /api/collections/{b}/datasets/{d}/rows` | `version`, `limit` (최대 1000) |
| `GET /api/collections/{b}/datasets/{d}/export` | `version`, `format=csv\|parquet` |
| `PUT /api/collections/{b}/datasets/{d}/notes` | `{"owner", "tags", "readme", "columns": {"<컬럼>": "메모"}}`, 빠진 키는 그대로 |
| `POST /api/collections/{b}/query` | `{"sql", "max_rows"}` |
| `GET` / `POST` `/api/collections/{b}/queries`, `DELETE /api/collections/{b}/queries/{id}` | 저장한 쿼리 (`id`를 주면 고침) |

## 예제

[examples/](examples/)의 디렉터리 하나가 컬렉션 하나입니다. 웹 UI에서는 디렉터리를 ZIP으로 압축해 가져오고, headless 프로필에서는 `.env`의 `COLLECTION_DIR`로 고릅니다.

| 컬렉션 | 내용 | 필요한 것 |
|---|---|---|
| [quickstart](examples/quickstart) | 주문 생성 → 일별 집계(stages) → 고객별 합계(SQL), 쿠폰을 추천하는 툴과 승인이 필요한 actor를 쓰는 에이전트 | 에이전트만 Gemma 4 엔드포인트 |
| [pipelines](examples/pipelines) | batch(CDC)·variable 예제, 스크립트 커넥터로 날씨 수집, native transform(stages·join·union·row_filters·column_masks), llm transform | `weather`는 인터넷, `llm_review`는 Gemma 4 엔드포인트 |
| [agents](examples/agents) | 고객지원(tools+actors+변수), 결제(승인 대기), 여행 플래너(roles 위임), 요약(`@use_llm` 툴), `@use_llm` 코드 스텝으로 피드백을 요약하는 파이프라인 | Gemma 4 엔드포인트 |
| [city-monitor](examples/city-monitor) | 공개 REST API 세 곳(Open-Meteo 날씨·대기질, USGS 지진)에서 스크립트 커넥터로 수집 → stages·join으로 도시별 일간 요약 → `@use_llm` 코드 스텝에서 Gemma 4가 위험 수준과 경보를 분석해 `insights`에 저장(매시 실행). 대화용 에이전트 `city_assistant`는 실시간 날씨·지진을 툴로 조회하고, `city_analyst`는 쌓인 데이터셋을 SQL로 조회(text2sql) | 인터넷, Gemma 4 엔드포인트 |

예제의 에이전트와 LLM 스텝은 모두 각 컬렉션의 `llm/gemma4.json`(Gemma 4 31B IT)을 씁니다.
OpenAI 호환 엔드포인트(vLLM, Ollama 등)로 부르며, 주소와 키는 시크릿 `dhub_llm/base_url`, `dhub_llm/api_key`에서 읽습니다.
웹 UI의 **시크릿**(공용)에 한 번 넣으면 모든 컬렉션이 씁니다. 다른 모델을 쓰려면 이 파일만 바꾸면 됩니다.

## 개발

```bash
uv sync --extra k8s
uv run --extra k8s pytest
```

## 라이선스

[MIT](LICENSE)
