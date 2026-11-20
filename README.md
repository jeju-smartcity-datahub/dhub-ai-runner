# D.Hub for AI 실행기 (파이프라인·에이전트 러너)

「2025년 제주 스마트도시 데이터허브 시범솔루션 발굴사업」 공통모듈 공개 패키지입니다. (디토닉㈜)

D.Hub for AI에서 내보낸 컬렉션(파이프라인·에이전트)을 가져와, 인증·권한·클러스터 없이 로컬, Docker Compose, Kubernetes 환경에서 실행하는 경량 실행기입니다.

소스코드는 [01_소스코드/dhub-runner/](01_소스코드/dhub-runner/)에, 공개 문서 9종(PDF)은 [docs/](docs/)에 있습니다.

## 공개 범위

| 구분 | 내용 |
|---|---|
| 공개(공통모듈) | 파이프라인 실행기·스케줄러, 에이전트 실행(OpenAI 호환 API), 웹 UI·API, 데이터 카탈로그 동기화, Kubernetes 실행 매니페스트 |
| 비공개(핵심 코어) | D.Hub for AI 데이터 연계 플랫폼 본체, 음성 인식(STT), AI 분석 모델 — 표준 API로 데이터허브와 연계 |
| 제외 | 실제 민원 데이터·개인정보, 계정·인증 키·DB 접속 정보 등 민감 설정 |

## 문서

| No | 문서 | 파일 |
|---|---|---|
| 1 | 소스코드 구성 | [01_소스코드/dhub-runner/](01_소스코드/dhub-runner/) |
| 2 | 설치·배포 가이드(인프라 요구사항 포함) | [docs/02_설치배포가이드.pdf](docs/02_설치배포가이드.pdf) |
| 3 | 빌드 스크립트·실행 환경(Docker Compose, Kubernetes Manifest) | [docs/03_빌드스크립트_실행환경.pdf](docs/03_빌드스크립트_실행환경.pdf) |
| 4 | API 명세서 및 호출 예제(cURL, Python) | [docs/04_API명세서_호출예제.pdf](docs/04_API명세서_호출예제.pdf) |
| 5 | 사용자 가이드 | [docs/05_사용자가이드.pdf](docs/05_사용자가이드.pdf) |
| 6 | 관리자 운영 매뉴얼 | [docs/06_관리자운영매뉴얼.pdf](docs/06_관리자운영매뉴얼.pdf) |
| 7 | 아키텍처 문서 | [docs/07_아키텍처문서.pdf](docs/07_아키텍처문서.pdf) |
| 8 | 필수 관리 문서(LICENSE, README, NOTICE) | [docs/08_필수관리문서_LICENSE_README_NOTICE.pdf](docs/08_필수관리문서_LICENSE_README_NOTICE.pdf) |
| 9 | 오픈소스 사용내역(SBOM) | [docs/09_오픈소스사용내역_SBOM.pdf](docs/09_오픈소스사용내역_SBOM.pdf) |
| 10 | 실행 가능한 샘플 데이터 | [docs/10_샘플데이터.pdf](docs/10_샘플데이터.pdf) |

## 빠른 시작

```bash
cd 01_소스코드/dhub-runner
cp .env.example .env
docker compose up -d --build     # http://localhost:8080
```

`.env.example`의 기본값은 시험용입니다. 운영 환경에서는 비밀번호와 `RUNNER_SECRET_KEY`를 반드시 변경하세요. 자세한 내용은 설치·배포 가이드를 참고하세요.

## 라이선스

[MIT License](LICENSE). 제3자 오픈소스 고지는 [NOTICE](NOTICE)와 오픈소스 사용내역(SBOM)을 참고하세요.

## 문의

디토닉㈜ 사업수행팀
