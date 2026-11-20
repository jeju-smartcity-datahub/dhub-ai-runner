# dhub2-runner 관리자 매뉴얼

dhub2-runner를 설치하고, 설정하고, 운영하는 방법을 설명합니다. 화면 사용법은 [사용자 매뉴얼](user-manual.md)을 보세요.

- [1. 구성](#1-구성)
- [2. 설치](#2-설치)
- [3. 설정 (환경변수)](#3-설정-환경변수)
- [4. 데이터가 저장되는 곳](#4-데이터가-저장되는-곳)
- [5. 시크릿과 마스터 키](#5-시크릿과-마스터-키)
- [6. 데이터 카탈로그 운영](#6-데이터-카탈로그-운영)
- [7. API와 Swagger](#7-api와-swagger)
- [8. UI 없이 실행 (headless, CLI)](#8-ui-없이-실행-headless-cli)
- [9. 보안](#9-보안)
- [10. 백업과 복구](#10-백업과-복구)
- [11. 업그레이드](#11-업그레이드)
- [12. 모니터링과 문제 해결](#12-모니터링과-문제-해결)

## 1. 구성

```
브라우저 ──▶ web (dhub2-runner web, :8080)  ── UI + /api + 스케줄러 + 카탈로그 동기화
                │
                ├── RustFS / S3   컬렉션 정의  s3://<bucket>/collections/<컬렉션>
                │                 Delta 테이블       s3://<bucket>/tables/<컬렉션>/<데이터셋>
                └── Postgres      실행 상태, 대화 세션, 시크릿(암호화), 데이터 카탈로그
```

| 구성 요소 | 하는 일 | 필수 |
|---|---|---|
| web | 웹 UI와 API, 예약 실행, 파이프라인 실행(로컬 모드), 에이전트 대화(로컬 모드), 카탈로그 동기화 | 예 |
| RustFS(또는 S3 호환) | 컬렉션과 Delta 테이블 | 로컬 디렉터리로 대신 가능. Kubernetes 모드는 필수 |
| Postgres | 상태·세션·시크릿·카탈로그 | 없으면 SQLite·암호화 파일로 동작하지만 **카탈로그 검색·노트·저장한 쿼리를 쓸 수 없음**. Kubernetes 모드는 필수 |
| 실행·에이전트 파드 | Kubernetes 모드에서 파이프라인 실행마다 Job, 에이전트마다 Deployment | Kubernetes 모드만 |

실행 방식은 두 가지입니다.

| | 로컬 / docker compose (기본) | Kubernetes (`RUNNER_EXECUTOR=kubernetes`) |
|---|---|---|
| 파이프라인 | web 안의 스레드 풀 (동시 4개) | 실행마다 Job (`dhub2-runner pipeline run`) |
| 에이전트 | web 안에서 처리 | 에이전트마다 Deployment + Service. 처음 쓸 때 띄우고, 컬렉션·시크릿이 바뀌면 내렸다가 다음에 새로 띄움 |
| 잠금 | 파일 잠금 또는 Postgres advisory lock | Postgres advisory lock |

## 2. 설치

### 2.1 docker compose (단일 서버)

```bash
cp .env.example .env
# .env: RUSTFS_ACCESS_KEY, RUSTFS_SECRET_KEY, POSTGRES_PASSWORD, RUNNER_SECRET_KEY 를 바꿉니다
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # RUNNER_SECRET_KEY 값
docker compose up -d --build
```

| 서비스 | 포트 | 비고 |
|---|---|---|
| web | `8080` | UI `http://<서버>:8080`, API 문서 `/api/docs` |
| rustfs | `127.0.0.1:9000` (S3), `127.0.0.1:9001` (콘솔) | 서버 안에서만 열림 |
| postgres | 열지 않음 | compose 네트워크 안에서만 |

이미 8080, 9000 포트를 쓰는 서비스가 있으면 `docker-compose.yaml`의 `ports`를 바꿉니다.

### 2.2 Kubernetes

RustFS(또는 S3)와 Postgres를 먼저 준비합니다.

```bash
docker build -t <registry>/dhub2-runner:<tag> . && docker push <registry>/dhub2-runner:<tag>
```

`deploy/k8s/runner.yaml`을 고칩니다.

| 대상 | 고칠 것 |
|---|---|
| Secret `runner-env` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `RUNNER_DATABASE_URL`, `RUNNER_SECRET_KEY` |
| ConfigMap `runner-config` | `AWS_ENDPOINT_URL`, `RUNNER_COLLECTIONS`, `RUNNER_TABLES`, `RUNNER_K8S_IMAGE`(위에서 올린 이미지), `RUNNER_K8S_SERVICE_ACCOUNT` |
| Deployment `dhub2-runner-web` | `image` |

```bash
kubectl apply -n <namespace> -f deploy/k8s/runner.yaml
kubectl -n <namespace> port-forward svc/dhub2-runner-web 8080:80   # 확인용
```

매니페스트에는 웹 앱이 Job·Deployment·Service를 만들고 지울 수 있는 ServiceAccount와 Role이 들어 있습니다. 밖에 공개하려면 Ingress를 따로 두고 [9. 보안](#9-보안)을 따릅니다.

**web은 레플리카 하나**로 둡니다. 스케줄러와 카탈로그 동기화가 web 안에서 돌기 때문입니다.

### 2.3 로컬 (개발·시험)

```bash
uv sync
uv run dhub2-runner --data data web                       # http://127.0.0.1:8080, 상태는 data/
# 카탈로그까지 쓰려면 Postgres를 연결합니다
RUNNER_DATABASE_URL=postgresql://runner:runner@127.0.0.1:5432/runner uv run dhub2-runner --data data web
```

### 2.4 설치 확인

1. UI가 열리고 **API 문서**(`/api/docs`)가 보이는지 확인합니다.
2. `examples/quickstart`를 ZIP으로 묶어 **가져오기** → `orders` 파이프라인 **실행** → 완료 확인.
3. **카탈로그**에 quickstart의 데이터셋 3개가 보이는지 확인합니다(Postgres 연결 시).

## 3. 설정 (환경변수)

환경변수로는 **런너 자신의 인프라 설정만** 받습니다. 파이프라인·에이전트의 API 키 같은 시크릿은 환경변수로 넘기지 않고 [시크릿 저장소](#5-시크릿과-마스터-키)에 넣습니다.

| 변수 | 기본 | 뜻 |
|---|---|---|
| `RUNNER_DATABASE_URL` | 없음 | Postgres 접속 주소. 있으면 상태·세션·시크릿·카탈로그를 Postgres에 둠 |
| `RUNNER_SECRET_KEY` | 없음 | 시크릿을 암호화하는 Fernet 키. 없으면 `<data>/secret.key`를 만들어 씀 |
| `RUNNER_COLLECTIONS` | `<data>/collections` | 컬렉션 위치. 디렉터리 또는 `s3://bucket/prefix` |
| `RUNNER_COLLECTION_DIR` | `collection` | 명령줄(headless)에서 쓸 컬렉션 하나의 위치. 디렉터리 또는 `s3://bucket/prefix/<컬렉션>` |
| `RUNNER_COLLECTION` | `RUNNER_COLLECTION_DIR`의 마지막 이름 | 명령줄에서 쓸 컬렉션의 이름 (시크릿과 상태를 이 이름으로 찾음) |
| `RUNNER_TABLES` | `<data>/tables` | Delta 테이블 위치. 디렉터리 또는 `s3://bucket/prefix` |
| `RUNNER_DATA_DIR` | `data` (이미지: `/data`) | 로컬 상태(SQLite, 암호화 파일, 마스터 키) 디렉터리 |
| `RUNNER_HOST`, `RUNNER_PORT` | `127.0.0.1`, `8080` | web이 듣는 주소 (컨테이너에서는 `--host 0.0.0.0`) |
| `RUNNER_LOG_LEVEL` | `INFO` | 로그 수준 |
| `RUNNER_FRONTEND_DIR` | 저장소의 `frontend/` | UI 파일 위치 (이미지에서 설정됨) |
| `AWS_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` | | RustFS/S3 접속 |
| `RUNNER_EXECUTOR` | 없음 | `kubernetes`면 파이프라인·에이전트를 파드로 실행 |
| `RUNNER_K8S_IMAGE` | | 실행·에이전트 파드 이미지 (web과 같은 이미지) |
| `RUNNER_K8S_NAMESPACE` | web 파드의 네임스페이스 | 파드를 만들 네임스페이스 |
| `RUNNER_K8S_ENV_FROM` | | 파드에 넘길 설정. 예: `secret/runner-env,configmap/runner-config` |
| `RUNNER_K8S_SERVICE_ACCOUNT` | | 실행·에이전트 파드의 서비스 계정 |

Kubernetes 모드에서는 `RUNNER_DATABASE_URL`, `RUNNER_SECRET_KEY`, `s3://` 위치의 `RUNNER_COLLECTIONS`·`RUNNER_TABLES`가 없으면 web이 시작하지 않습니다. 파드마다 따로 만든 키로는 서로의 시크릿을 읽을 수 없기 때문입니다.

## 4. 데이터가 저장되는 곳

| 데이터 | Postgres가 있을 때 | 없을 때 |
|---|---|---|
| 컬렉션 정의 | `RUNNER_COLLECTIONS/<컬렉션>/` | 같음 |
| Delta 테이블 | `RUNNER_TABLES/<컬렉션>/<데이터셋>/` | 같음 |
| 스텝 체크포인트, 실행 이력, 대화 세션, 정지 상태 | `runner_checkpoints`, `runner_runs`, `runner_sessions`, `runner_stopped` | `<data>/state/<컬렉션>/runner.db` (SQLite) |
| 시크릿 | `runner_secrets` (값은 마스터 키로 암호화) | `<data>/state/<컬렉션>/secrets.enc`, `<data>/shared/secrets.enc` |
| 데이터 카탈로그 | `runner_catalog_*` 6개 테이블 | 없음 (데이터셋 화면은 Delta 로그를 바로 읽음) |
| 마스터 키 | `RUNNER_SECRET_KEY` 또는 `<data>/secret.key` | 같음 |

카탈로그 테이블:

| 테이블 | 담는 것 | 다시 만들 수 있는가 |
|---|---|---|
| `runner_catalog_tables` | 데이터셋: 위치, 버전, 행·크기·파일 수, 파티션, 속성, 시각, 정의의 별칭·설명·태그, 검색용 텍스트 | 예 (동기화) |
| `runner_catalog_columns` | 컬럼 타입, null 허용, 설명, 파일 통계(null 수, 최소, 최대) | 예 |
| `runner_catalog_history` | Delta 커밋 이력 (데이터셋마다 최근 200개) | 예 |
| `runner_catalog_lineage` | 데이터셋을 읽고 쓰는 파이프라인 스텝 | 예 |
| `runner_catalog_notes` | 사람이 쓴 소유자, 태그, 문서, 컬럼 메모 | **아니오 — 백업 대상** |
| `runner_catalog_queries` | 저장한 SQL | **아니오 — 백업 대상** |

## 5. 시크릿과 마스터 키

- 시크릿은 컬렉션 안에서 `secret://<이름>/<키>`로 참조하고, 값은 시크릿 저장소에 둡니다. 컬렉션 값이 먼저, 없으면 공용 값을 씁니다.
- 값 입력: 웹 UI(공용 **시크릿** 메뉴, 컬렉션의 **시크릿** 탭), API `PUT /api/secrets`·`PUT /api/collections/{name}/secrets`, CLI.

  ```bash
  echo -n "$KEY" | dhub2-runner secret set dhub2_llm/api_key --shared     # 공용
  echo -n "$TOKEN" | dhub2-runner --collection demo secret set crm/token    # 컬렉션
  dhub2-runner secret list --shared
  dhub2-runner --collection demo secret delete crm/token
  ```
  docker compose에서는 `docker compose exec web dhub2-runner secret set …`.
- `api_key_env`, `credentials_env`처럼 환경변수에서 값을 읽는 설정은 이유와 함께 거부합니다. `api_key_ref`, `<옵션>_ref`를 쓰게 하세요.
- **마스터 키(`RUNNER_SECRET_KEY`)를 잃으면 저장된 시크릿을 읽을 수 없습니다.** 키는 시크릿 관리 도구에 따로 보관하고, 운영 환경에서는 반드시 명시적으로 설정하세요.
- 키를 바꿔야 하면 모든 시크릿을 새 키로 다시 입력해야 합니다(자동 재암호화는 없습니다).

## 6. 데이터 카탈로그 운영

### 동기화

카탈로그는 Delta 로그(커밋, 파일 목록·통계)만 읽고 데이터 파일은 읽지 않습니다. 버전과 정의가 그대로인 데이터셋은 건너뛰므로 자주 돌려도 가볍습니다.

| 언제 | 범위 |
|---|---|
| 컬렉션을 가져올 때 | 그 컬렉션 |
| 웹 앱 안의 파이프라인 실행이 끝날 때 | 그 컬렉션 |
| 1분마다 (web 백그라운드) | 모든 컬렉션. 없어진 컬렉션은 카탈로그에서 지움 |
| UI **동기화** / 데이터셋 **다시 읽기** | 모든 컬렉션 / 그 컬렉션 (다시 읽기는 강제) |
| `POST /api/catalog/sync` `{"collection"?, "force"?}` | 지정한 대로 |

Kubernetes 모드에서 Job이 쓴 테이블은 1분 주기 동기화로 반영됩니다. 컬렉션을 지우면 카탈로그 정보(노트, 저장한 쿼리 포함)도 지워집니다.

### 다시 만들기

카탈로그가 어긋났다고 의심되면 강제 동기화를 돌립니다.

```bash
curl -X POST http://<web>/api/catalog/sync -H 'content-type: application/json' -d '{"force": true}'
```

처음부터 다시 만들려면 노트와 저장한 쿼리를 남기고 나머지만 비운 뒤 강제 동기화합니다.

```sql
TRUNCATE runner_catalog_tables, runner_catalog_columns, runner_catalog_history, runner_catalog_lineage;
```

### 검색 성능

시작할 때 `pg_trgm` 확장을 설치하고 검색 텍스트에 trigram 인덱스를 만듭니다. DB 계정에 확장 설치 권한이 없으면 로그에 "catalog search without a trigram index"를 남기고 인덱스 없이 동작합니다. 데이터셋이 수천 개를 넘으면 DBA가 한 번 설치해 주세요.

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

### 읽을 수 없는 테이블

테이블을 읽다 실패하면 그 데이터셋에 오류를 기록하고(UI에 빨간 배지와 내용) 나머지는 계속 동기화합니다. 스토리지 권한, 깨진 `_delta_log`를 확인하세요.

## 7. API와 Swagger

| 주소 | 내용 |
|---|---|
| `/api/docs` | Swagger UI — 모든 API를 기능별(컬렉션, 시크릿, 리소스, 운영, 파이프라인, 카탈로그, 데이터셋, SQL, 에이전트)로 보고 바로 호출 |
| `/api/redoc` | ReDoc 문서 |
| `/api/openapi.json` | OpenAPI 3.1 문서 (클라이언트 생성에 사용) |

- 오류는 `{"detail": "..."}`입니다. 404 없는 대상, 409 지금 할 수 없는 동작(실행 중, 중지됨), 422 잘못된 값·실행할 수 없는 정의, 502 모델이나 툴 백엔드 실패, 503 Postgres가 필요한 기능.
- 에이전트의 OpenAI 호환 API(`dhub2-runner agent serve`, 기본 `:8000`)는 별도 서버이며 `/docs`에 자체 문서가 있습니다.

## 8. UI 없이 실행 (headless, CLI)

컬렉션 하나만 돌릴 때는 compose의 `headless` 프로필을 씁니다. `.env`의 `COLLECTION_DIR`, `COLLECTION_NAME`으로 컬렉션을 고릅니다.

```bash
docker compose --profile headless up -d --build   # collection-init → agent-runner(:8000), pipeline-runner
docker compose exec pipeline-runner dhub2-runner pipeline run orders
```

CLI 요약 (`--collection-dir`, `--tables`, `--data`, `--collection` 또는 같은 이름의 `RUNNER_*` 변수로 위치를 지정):

| 명령 | 뜻 |
|---|---|
| `dhub2-runner check` | 컬렉션 검증, 실행할 수 없는 파이프라인·에이전트와 값이 없는 시크릿 확인 |
| `dhub2-runner import <export.zip> <dest>` | dhub2 내보내기 → 컬렉션 (dest는 디렉터리 또는 `s3://`) |
| `dhub2-runner collection push <dir>` | 로컬 컬렉션을 `--collection-dir`(s3://)로 올리기 |
| `dhub2-runner pipeline run|serve|status|reset|seed` | 실행, 스케줄 실행, 상태, 체크포인트 초기화, 초기 데이터 적재 |
| `dhub2-runner agent serve|chat` | OpenAI 호환 API 서버, 터미널 대화 |
| `dhub2-runner table show <dataset>` | 데이터셋 출력 |
| `dhub2-runner secret set|list|delete` | 시크릿 관리 (`--shared`: 공용) |
| `dhub2-runner web` | 웹 UI와 API |

## 9. 보안

- **런너에는 인증·권한이 없습니다.** UI와 API를 쓸 수 있는 사람은 모든 컬렉션을 가져오고, 지우고, 실행하고, 시크릿을 덮어쓸 수 있습니다(값을 읽을 수는 없음).
- 밖에 공개할 때는 앞단에 인증을 두세요: 사내 SSO 프록시(oauth2-proxy 등), Ingress의 인증, VPN.
- 챗봇을 다른 서비스에 iframe으로 넣을 때도 `/chat`과 `/api/collections/*/agents/*/chat*`만 열고 나머지는 막는 구성을 권합니다.
- 파이프라인 코드와 커넥터 스크립트는 web(로컬 모드) 또는 실행 파드(Kubernetes 모드) 안에서 그대로 돕니다. 신뢰할 수 있는 컬렉션만 가져오세요.
- SQL 조회와 text2sql 툴은 `SELECT`만 받고 테이블을 읽기만 합니다.
- 시크릿 값은 API 응답·로그에 나오지 않습니다. 리소스 정의를 보여줄 때도 `api_key`, `password`, `token`, `secret` 필드는 가립니다.

## 10. 백업과 복구

| 대상 | 방법 | 주기 권장 |
|---|---|---|
| Postgres | `pg_dump` (특히 `runner_secrets`, `runner_catalog_notes`, `runner_catalog_queries`, `runner_sessions`) | 매일 |
| RustFS/S3 `tables/` | 버킷 복제나 스냅샷. Delta 테이블은 디렉터리째 복사 | 데이터 중요도에 따라 |
| RustFS/S3 `collections/` | 원본 내보내기 ZIP을 보관하면 다시 가져오기로 복구 가능 | 가져올 때마다 |
| 마스터 키 | 시크릿 관리 도구에 별도 보관 | 바꿀 때 |
| `<data>` 볼륨 | Postgres가 없을 때만 필요 (SQLite 상태, 암호화 파일, `secret.key`) | 매일 |

```bash
docker compose exec postgres pg_dump -U runner runner > runner-$(date +%F).sql
docker compose exec -T postgres psql -U runner runner < runner-2026-11-03.sql   # 복구
```

복구한 뒤 카탈로그는 `POST /api/catalog/sync {"force": true}`로 테이블 정보를 다시 맞춥니다.

## 11. 업그레이드

1. Postgres를 백업합니다.
2. 새 이미지를 빌드·배포합니다 (`docker compose up -d --build` 또는 이미지 태그 변경 후 `kubectl apply`).
3. 시작할 때 필요한 테이블(`CREATE TABLE IF NOT EXISTS`)을 스스로 만듭니다. 별도 마이그레이션 명령은 없습니다.
4. 진행 중이던 로컬 실행은 재시작 때 "중단됨"으로 기록됩니다. 필요하면 다시 실행하세요.
5. Kubernetes 모드에서는 새 `RUNNER_K8S_IMAGE`로 바꾼 뒤 에이전트를 한 번 **중지 → 시작**해 새 이미지로 파드를 띄웁니다.

## 12. 모니터링과 문제 해결

로그는 표준출력으로 나옵니다(`docker compose logs -f web`, `kubectl logs deploy/dhub2-runner-web`). `RUNNER_LOG_LEVEL=DEBUG`로 자세히 볼 수 있습니다.

| 증상 | 확인할 것 |
|---|---|
| web이 시작하지 않음 (Kubernetes) | 로그의 "needs …" 메시지: `RUNNER_DATABASE_URL`, `RUNNER_SECRET_KEY`, `s3://` 위치 |
| "cannot be decrypted with this master key" | `RUNNER_SECRET_KEY`가 바뀌었거나 다른 `<data>/secret.key`를 씀. 원래 키로 되돌리거나 시크릿을 다시 입력 |
| 카탈로그 503 | `RUNNER_DATABASE_URL`이 web에 설정되어 있는지 |
| 카탈로그가 늦게 바뀜 | 동기화 주기(1분). 로그의 "catalog sync of … failed" 확인 |
| 파이프라인이 "interrupted" | 실행 중 web이 재시작됨 (로컬 모드) |
| Kubernetes 실행이 실패로만 남음 | Job 파드가 결과를 남기지 못하고 끝남 (이미지, 메모리). `kubectl describe job`, 파드 로그 |
| 에이전트가 "starting"에서 멈춤 | 에이전트 Deployment의 이벤트, 이미지 풀, 서비스 계정 권한 |
| S3 접근 오류 | `AWS_ENDPOINT_URL`, 키, 버킷 존재. 버킷은 처음 쓸 때 만들어짐 |
| 모델 호출 502 | 시크릿 `<모델>/base_url`, `<모델>/api_key`와 엔드포인트 상태 |

상태 확인용 API:

| | |
|---|---|
| `GET /api/operations` | 모든 파이프라인·에이전트 상태 |
| `GET /api/secrets` | 컬렉션마다 빠진 시크릿 |
| `GET /api/catalog?limit=1` | 카탈로그 응답과 마지막 동기화 시각(`synced_at`) |
