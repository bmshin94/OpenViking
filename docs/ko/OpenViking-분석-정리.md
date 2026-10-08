# OpenViking 전수조사 분석 및 활용 전략 정리

> 이 문서는 OpenViking 저장소를 전수조사하여 분석한 내용과, 설치·활용·수익화 전략에 대한
> 대화 내용을 정리한 기록입니다.

- **원본 저장소**: https://github.com/volcengine/OpenViking
- **현재 저장소(포크)**: https://github.com/bmshin94/OpenViking
- **공식 사이트**: https://www.openviking.ai
- **공식 문서**: https://docs.openviking.ai
- **라이브 데모(Studio)**: https://openviking.ai/studio
- **블로그**: https://blog.openviking.ai
- **작성일**: 2026-10-07
- **분석 기준 커밋**: `212066e` (claude/keen-euler-udvalh 브랜치)

---

## 목차

1. [프로젝트 개요](#1-프로젝트-개요)
2. [핵심 개념](#2-핵심-개념)
3. [기술적 특징 — 3단 로딩(L0/L1/L2)](#3-기술적-특징--3단-로딩l0l1l2)
4. [벤치마크 및 연구 논문](#4-벤치마크-및-연구-논문)
5. [폴더 전수조사 결과](#5-폴더-전수조사-결과)
6. [Claude Code 플러그인 분석](#6-claude-code-플러그인-분석)
7. [지원 에이전트 목록](#7-지원-에이전트-목록)
8. [CLAUDE.md 설명과 실제의 차이](#8-claudemd-설명과-실제의-차이)
9. [설치 및 사용법](#9-설치-및-사용법)
10. [플러그인 / 스킬 / MCP 구분](#10-플러그인--스킬--mcp-구분)
11. [API 토큰 필요 여부](#11-api-토큰-필요-여부)
12. [AI 에이전트 구축 활용도](#12-ai-에이전트-구축-활용도)
13. [React / PHP 연동 가능성](#13-react--php-연동-가능성)
14. [유튜브 강의 영상 제작 가능성](#14-유튜브-강의-영상-제작-가능성)
15. [수익화 아이디어 상세](#15-수익화-아이디어-상세)
16. [결론 및 체크리스트](#16-결론-및-체크리스트)

---

## 1. 프로젝트 개요

### 한 줄 요약

> **AI 에이전트 전용 "기억 데이터베이스"** — AI가 대화·문서·업무 경험을 잊지 않고 쌓아두고,
> 필요할 때만 꺼내 쓰게 해주는 서버 소프트웨어.

- 개발: **ByteDance / Volcengine (볼케이노 엔진)**
- 공식 포지셔닝: **"The Context Database for AI Agents"**
- 라이선스: **AGPLv3** (일부 예외: `crates/ov_cli`, `examples` = Apache 2.0, `examples/hermes-plugin` = MIT)
- 성숙도: `Development Status :: 3 - Alpha`
- 요구사항: Python 3.10+, 임베딩 모델 + VLM(비전언어모델)
- 기본 포트: `1933`

### 해결하는 문제

| 기존 AI의 한계 | OpenViking의 해법 |
|---|---|
| 대화창 닫으면 전부 잊음 | 기억을 가상 파일시스템(`viking://`)에 영구 저장 |
| 문서를 매번 통째로 넣어야 함 | L0/L1/L2 3단 로딩으로 필요한 만큼만 |
| 같은 삽질을 반복 | `experiences/`, `trajectories/`에 실행 경험 축적 |
| AI 도구 갈아타면 기억 소실 | 여러 에이전트가 같은 기억 공유 |
| 토큰 비용 폭발 | 입력 토큰 34~91% 절감 |

### 가상 파일시스템 구조

```
viking://
├── resources/                  # ① 자료 (사람이 추가)
│   └── my_project/
│       ├── docs/api/
│       └── src/
└── user/{user_id}/
    ├── memories/               # ② 기억 (AI가 자동 기록)
    │   ├── profile.md          # 사용자 기본 정보
    │   ├── preferences/        # 취향 (주제별)
    │   ├── entities/           # 사람·조직·프로젝트
    │   ├── events/             # 의사결정·마일스톤
    │   ├── experiences/        # 실행에서 얻은 재사용 경험
    │   ├── trajectories/       # 재사용 가능한 작업 수행 경로
    │   ├── cases/              # 학습·평가용 케이스
    │   ├── identity.md         # 어시스턴트 이름·성격
    │   └── soul.md             # 원칙·경계·말투·연속성
    ├── resources/              # 개인 자료
    ├── skills/                 # ③ 스킬 (SKILL.md)
    └── peers/                  # 다른 사용자/봇과의 관계
```

> `viking://~` 는 홈 별칭으로, 서버가 인증된 호출자의 `viking://user/{user_id}/...` 로 확장합니다.

---

## 2. 핵심 개념

출처: `docs/en/concepts/02-context-types.md`

| 종류 | 뜻 | 누가 만듦 | 생애주기 |
|---|---|---|---|
| **Resource (자료)** | API 문서, 매뉴얼, 코드 저장소, 논문, 웹페이지 | 사람이 추가 | 장기·정적 |
| **Memory (기억)** | 사용자 취향, 과거 결정, 실행 경험 | **AI가 스스로 기록** | 장기·동적 갱신 |
| **Skill (스킬)** | "이 작업은 이렇게 해라" 절차서 | 사람 또는 시스템 | 장기·정적 |

### 비유: "캐비닛을 가진 신입사원"

매일 기억이 리셋되는 신입사원에게, 요약 쪽지가 붙은 캐비닛을 하나 준다.
- `회사자료/` → Resource
- `김과장에_대해/` → Memory (preferences)
- `내가_배운_것/` → Memory (experiences)

신입은 캐비닛을 통째로 읽지 않고, 겉의 포스트잇(L0) → 목차(L1) → 필요한 한 장(L2)만 꺼내 읽는다.

---

## 3. 기술적 특징 — 3단 로딩(L0/L1/L2)

일반 RAG는 문서를 잘게 쪼개 벡터 검색만 한다. OpenViking은 **폴더마다 요약문을 붙여둔다.**

```
viking://resources/my_project/
├── .abstract.md     # L0: 한 문장 요약 (빠른 관련성 판단)
├── .overview.md     # L1: 핵심 정보 + 사용 시나리오 (계획 수립)
└── docs/api/
    ├── auth.md      # L2: 전체 원문 (필요할 때만 로드)
    └── endpoints.md
```

| 단계 | 내용 | 비유 | 소요 |
|---|---|---|---|
| **L0 (Abstract)** | 한 문장 요약 | 책등 제목 훑기 | 0.5초 |
| **L1 (Overview)** | 핵심 정보 + 언제 쓰는지 | 목차·서문 읽기 | 5초 |
| **L2 (Details)** | 전체 원문 | 해당 페이지 펼쳐 읽기 | 필요시만 |

### 2단계 검색

1. 벡터 검색으로 **어느 디렉토리**가 관련 있는지 찾음 (TrieHI 인덱스가 디렉토리 스코프 해석)
2. 그 디렉토리 안만 탐색

- `find` = 빠른 단순 의미 검색
- `search` = 세션 맥락까지 고려한 계획형 검색 (`mode="context"` 시 서버가 컨텍스트 조립)
- `grep` / `glob` = 정확 문자열·패턴 검색

### 비용 효과 시뮬레이션

하루 50회 질문, 매번 프로젝트 문서 참조, 입력 $3/1M 토큰 가정:

| | 기존 방식 | OpenViking |
|---|---|---|
| 1회 입력 토큰 | 50,000 | 8,000 (84% 감소) |
| 월(22일) 합계 | 5,500만 토큰 | 880만 토큰 |
| **월 비용** | **약 $165** | **약 $26** |
| **절감** | — | **월 약 $139 (약 20만원)** |

> 단, 임베딩 + VLM API 비용이 별도로 발생합니다. 초기 인덱싱 비용이 크고 이후 검색 비용만 듭니다.
> Ollama 로컬 임베딩 사용 시 이 부분은 0원.

---

## 4. 벤치마크 및 연구 논문

### 벤치마크 (OpenViking 0.3.22, Doubao 2.0 Pro 기준)

**장기 대화 기억력 (LoCoMo)**

| 에이전트 | 기본 기억 | + OpenViking |
|---|---|---|
| OpenClaw | 24.20% | **82.08%** |
| Hermes | 33.38% | **82.86%** |
| Claude Code | 57.21% | **80.32%** |

**다단계 업무 수행 (tau2-bench)**

| 분야 | 기억 없음 | + 경험 기억 |
|---|---|---|
| 리테일 | 70.94% | **77.81%** (+6.87pp) |
| 항공 | 54.38% | **66.25%** (+11.87pp) |

- 입력 토큰 **34.3~91.0% 감소**
- 질의 지연 **58.45~66.10% 감소**
- 재현 스크립트: `./benchmark` (locomo, tau2, longmemeval, skillsbench, RAG, vectordb_perf)

> 주의: 자체 측정이며 바이트댄스 Doubao 2.0 Pro + doubao-embedding-vision 기준입니다.

### 논문 3편

| 논문 | 내용 | 게재 |
|---|---|---|
| **VikingMem** (arXiv:2605.29640) | 이벤트 기반 장기기억 추출·갱신·통합 | VLDB 2026 |
| **Directory-Aware Query and Maintenance in Vector Databases** (arXiv:2606.16903) | 디렉토리 인식 벡터 검색, TrieHI 인덱스 | ICDE 채택 |
| **VikingRAG** (arXiv:2609.11390) | 구조화 문서 대상 토큰 효율 RAG | 투고 중 |

---

## 5. 폴더 전수조사 결과

### 실측 규모

| 항목 | 수치 |
|---|---|
| 저장소 크기 | **112MB** (.git 제외) |
| Python 파일 | **1,835개** |
| 테스트 파일 | **665개** (31개 하위 디렉토리) |
| 문서(.md) | **261개** (en/zh 2개 언어) |
| Python 코드 | 약 **19만 줄** |
| C++ / Rust 코드 | 약 **12만 줄** |
| TS / JS 코드 | 약 **7만 줄** |
| **총 코드량** | **약 38만 줄** |

### 디렉토리 구조

| 폴더 | 정체 | 역할 |
|---|---|---|
| `openviking/` | **본체** (19만 줄) | 서버·서비스·검색·세션·저장소 |
| └ `core/` | 핵심 로직 | URI 검증, 트리 구성, 스킬 로더, MCP 변환기, 네임스페이스 |
| └ `server/` | HTTP 서버 | FastAPI. `routers/` 24개 엔드포인트 |
| └ `server/mcp_endpoint.py` | **MCP 서버** | 도구 16개 (`find/search/read/...`) |
| └ `service/` | 비즈니스 로직 | 검색·세션·리소스·컴파일·agent_evolution·태스크큐 |
| └ `retrieve/` | 검색 엔진 | IntentAnalyzer, HierarchicalRetriever, memory_lifecycle |
| └ `session/` | 세션 관리 | auto_commit_policy, compressor_v3, memory_policy, train/ |
| └ `storage/` | 저장소 | VikingFS 가상FS, vectordb, ACL, 암호화, 트랜잭션, ovpack |
| └ `parse/` | 문서 파싱 | PDF/MD/HTML → L0/L1/L2 추출 |
| └ `connector/` | 커넥터 | client, delegate, routing (에이전트 간 위임) |
| └ `ingest/` | 수집 | orchestrator, poller, replay, sources |
| └ `integrations/` | 통합 | langchain |
| `src/` | **C++ 엔진** (12만 줄) | 벡터 인덱스, 스칼라 인덱스, 저장 엔진 |
| `crates/` | **Rust** | `ov_cli` (CLI 본체, Apache 2.0), `ragfs`, `ragfs-python` |
| `sdk/` | **3개 언어 SDK** | `python/`, `go/`, `typescript/` |
| `openviking_cli/` | CLI 래퍼 | `ov` 명령, setup_wizard, doctor, rust_cli |
| `bot/` | **VikingBot** | OpenViking 위의 AI 에이전트 프레임워크 |
| `web-studio/` | **웹 UI** | React + Vite + TS + shadcn/ui (셀프호스팅 가능) |
| `examples/` | **28개 통합 예제** | 하단 별도 표 |
| `agent-plugins/` | Agent Plugins 1.0 | 벤더 중립 플러그인 패키지 (stdio MCP 프록시) |
| `.claude-plugin/` | Claude 마켓플레이스 | `marketplace.json` |
| `.agents/` | 에이전트 플러그인 | `plugins/marketplace.json` |
| `benchmark/` | 성능 측정 | locomo, tau2, longmemeval, skillsbench, RAG, cuvs |
| `docs/` | 문서 261개 | concepts 17편, guides 18편, api 25편 |
| `deploy/` | 배포 | Helm 차트 (쿠버네티스) |
| `docker/`, `Dockerfile`, `docker-compose.yml` | 컨테이너 | 공식 이미지 |
| `integrations/langchain/` | LangChain 통합 | Tools + Store |
| `npm/cli/` | npm 배포 | CLI npm 패키지 |
| `third_party/` | 외부 라이브러리 | croaring, krl, leveldb-1.23, rapidjson, spdlog-1.14.1 |
| `tests/` | 테스트 | 665개 |

### `examples/` 상세

```
claude-code-memory-plugin/   # Claude Code용 (훅 10개 + MCP + 스킬 3개)
codex-memory-plugin/         # OpenAI Codex용
opencode-plugin/             # OpenCode용
openclaw-plugin/             # OpenClaw용
hermes-plugin/               # Hermes Agent용 (MIT)
dsh-memory-plugin/           # DSH용
pi-coding-agent-extension/   # pi용
pi-experimental-context-management/
openwebui-plugin/            # OpenWebUI용
agent-hook-plugin/           # 범용 훅 플러그인
memory-plugin-shared/        # 공통 설치 스크립트(install.sh)
langchain-langgraph/         # LangChain/LangGraph 연동
llamaparse-understanding-bridge/
skills/                      # 바로 쓰는 스킬 7개
  ├── openviking-memory/     #   회상 + 저장 루프
  ├── ov-experience-memory/  #   실행 경험 활용
  ├── ov-add-paper/          #   논문 추가
  ├── ov-resources/          #   자료 관리
  ├── ov-server-operate/     #   서버 운영
  ├── ov-skills/             #   스킬 관리
  └── ov_dream/              #   실험적
k8s-helm/, cloud/, grafana/  # 운영 배포용
multi_tenant/                # 멀티테넌트 예제
snapshot/, compile/          # 스냅샷·컴파일
quick_start.py               # 입문 예제
ov.conf.example              # 서버 설정 템플릿 (404줄)
ovcli.conf.example           # 클라이언트 설정 템플릿
```

### 아키텍처 (docs/en/concepts/01-architecture.md)

```
              Client (OpenViking)
                     │ delegates
              Service Layer
                     │
      ┌──────────────┼──────────────┐
      ▼              ▼              ▼
  Retrieve        Session         Parse
 (search/find)  (add/commit)   (문서파싱, L0/L1/L2)
  Intent            │            Tree build
  Rerank            ▼
              Compressor
          (압축 / 중복제거)
                     │
      ┌──────────────┴──────────────┐
      │        Storage Layer         │
      │  AGFS(파일) + Vector Index   │
      └──────────────────────────────┘
```

| 모듈 | 책임 |
|---|---|
| **Client** | 통합 진입점, Service 레이어로 위임 |
| **Service** | FSService, SearchService, SessionService, ResourceService, PackService, DebugService |
| **Retrieve** | IntentAnalyzer(의도분석), HierarchicalRetriever(계층검색), Rerank |
| **Session** | 메시지 기록, 사용 추적, 세션 압축, 메모리 커밋 |
| **Parse** | PDF/MD/HTML 파싱, TreeBuilder, 비동기 의미 생성 |
| **Compressor** | 스키마 기반 메모리 추출 + LLM 중복제거 판단 |
| **Storage** | VikingFS 가상파일시스템, 벡터 인덱스, AGFS 통합 |

### 벡터DB 어댑터

`openviking/storage/vectordb_adapters/`
- `local_adapter.py` — 로컬 (기본)
- `volcengine_adapter.py` — VikingDB 클라우드
- `vikingdb_private_adapter.py` — VikingDB 프라이빗
- `http_adapter.py` — HTTP 범용

---

## 6. Claude Code 플러그인 분석

출처: `examples/claude-code-memory-plugin/hooks/hooks.json` (실측)

### 훅 10개 — AI의 "선택" 없이 자동 실행

| 훅 시점 | 스크립트 | 타임아웃 | 하는 일 |
|---|---|---|---|
| `SessionStart` | session-start.mjs | 120s | 세션 시작 시 관련 기억 자동 주입 |
| `UserPromptSubmit` | auto-recall.mjs | 60s | **질문할 때마다** 관련 기억 자동 검색·첨부 |
| `PostToolUse(Read)` | skill-experience.mjs | 5s | 파일 읽을 때 경험 기억 연결 |
| `PreToolUse(Read\|Glob\|Grep\|Edit\|Write\|Bash)` | uri-guard.mjs | 5s | `viking://` URI 가로채기 |
| `Stop` | auto-capture.mjs | 45s | **응답 끝날 때마다** 중요 정보 자동 저장 |
| `PreCompact` | pre-compact.mjs | 30s | 컨텍스트 압축 전 기억 보존 |
| `SessionEnd` | session-end.mjs | 30s | 세션 종료 시 커밋 → 백그라운드 기억 추출 |
| `SubagentStart` | subagent-start.mjs | 10s | 서브에이전트에 기억 공유 |
| `SubagentStop` | subagent-stop.mjs | 45s | 서브에이전트 결과 캡처 |

### 스킬 3개

| 스킬 | 역할 |
|---|---|
| `openviking-memory` | 회상 + 저장 루프를 모델에게 교육 |
| `ov-experience-memory` | 실행 가능한 다단계 작업 전 과거 경험 활용 |
| `ov-memory-doctor` | 기억 추출 문제 진단 (읽기 전용) |

### 플러그인 메타 (`.claude-plugin/plugin.json`)

```json
{
  "name": "openviking-memory",
  "version": "0.5.2",
  "license": "Apache-2.0",
  "mcpServers": "./.mcp.json"
}
```

### 핵심 결론

> 리포 원문: *"If your harness has a hook system, **prefer the dedicated plugin** — hook-driven
> recall and capture cost no tool calls and don't depend on the model choosing to remember."*

**Claude Code를 쓰면 반드시 훅 플러그인을 쓸 것.** MCP만 쓰면 AI가 "기억해야겠다"고 결심해야 하지만,
훅은 무조건 실행되고 토큰 비용도 0이다.

### 하루 시나리오 예시

```
09:00  Claude Code 시작
       → [SessionStart 훅] 어제 작업 + 사용자 선호 자동 주입
       → AI: "어제 결제 모듈 리팩토링 중이었고, 금요일 배포 금지 기억합니다."

10:00  "결제 승인 실패 시 재시도 로직 넣어줘"
       → [UserPromptSubmit 훅] experiences/ 검색 → "재시도 3회 이상 시 중복결제 사고" 발견
       → AI: "재시도는 2회로 제한하겠습니다. 과거 중복결제 사고 기록이 있어서요."

11:00  "Redis 연결 타임아웃 나는데" → 해결 (포트 오타)
       → [Stop 훅] experiences/ 에 자동 기록

다음달  "Redis 타임아웃 나는데"
       → AI: "REDIS_URL 포트를 확인하세요. 지난달 같은 문제가 있었습니다."
```

---

## 7. 지원 에이전트 목록

| 에이전트 | 연동 방식 |
|---|---|
| **Claude Code** | 훅 + MCP |
| **Codex (OpenAI)** | 훅 + MCP |
| **Cursor** | 훅 + MCP |
| **TRAE / TRAE CN** | 훅 + MCP |
| **ZCode** | 훅 |
| **OpenClaw** | 컨텍스트 엔진 (내장급) |
| **Hermes Agent** | 빌트인 |
| **OpenCode** | 플러그인 + MCP |
| **pi** | 네이티브 확장 |
| **DeerFlow** | 플러그인 + MCP |
| **DSH** | 플러그인 + MCP |
| **Doubao Work** | 커넥터 |
| **LangChain / LangGraph** | 툴 + 스토어 |
| **OpenWebUI** | 플러그인 |
| **모든 MCP 클라이언트** | MCP 범용 |
| **Agent Plugins 1.0** | 벤더 중립 규격 |

### Agent Plugins 1.0 참고

`agent-plugins/` 는 Amazon, Cursor, Microsoft, OpenAI, Vercel 등이 지지하는 벤더 중립 규격 패키지.
`plugin.json` + `skills/` + `mcp.json` 구조.

- **훅은 규격에서 제외** → 자동 캡처 불가, 스킬이 모델에게 수동 수행을 교육
- `mcp.json`에 `streamable-http`를 직접 못 넣는 이유: 서버 URL이 배포마다 다르고, 규격이
  static headers에 자격증명을 금지 → **stdio 프록시**(`servers/mcp-proxy.mjs`)가 런타임에 URL/키 주입
- npm 의존성 0, Node 18+ 표준 라이브러리만 사용

---

## 8. CLAUDE.md 설명과 실제의 차이

이 저장소의 `CLAUDE.md`는 자동 생성된 한국어 요약이며, 실제와 일부 차이가 있습니다.

| CLAUDE.md 표현 | 실제 검증 결과 |
|---|---|
| "자기 진화형" | **절반 사실.** `experiences/`, `trajectories/`에 경험이 누적되고 `agent_evolution` 서비스가 존재하지만 **기본값은 `enabled: false`** (`examples/ov.conf.example:9`). 공식 포지셔닝은 "The Context Database for AI Agents" |
| "도구 사용법을 진화시킨다" | **주의.** `memories/tools/`, `memories/skills/` 스키마는 **비활성화 상태**로 명시됨 (`docs/en/concepts/02-context-types.md`). 단 trajectory·experience 축적은 실제 작동 |
| "단기기억 + RAG + 스킬 통합" | **사실.** Resource / Memory / Skill 3종 통합이 정확한 설명 |
| "바이트댄스 오픈소스" | **사실.** `pyproject.toml` author = ByteDance |

### 정확한 이해

자율적으로 "진화"하는 AI가 아니라, **경험을 자동으로 축적해서 다음 작업 때 다시 꺼내 쓰는
기억 인프라**다. 진화의 재료를 모아주는 역할.

---

## 9. 설치 및 사용법

### 사전 준비

| 항목 | 요구사항 |
|---|---|
| Python | 3.10 이상 |
| OS | Linux / macOS / Windows |
| 포트 | 1933 (기본) |
| 모델 | **임베딩 모델 + VLM 둘 다 필요** |

### 방법 A: Python 패키지

```bash
# 설치 (uv 권장)
uv tool install openviking --upgrade
# 또는
pip install openviking --upgrade --force-reinstall
# 또는
pipx install openviking

openviking-server init      # 대화형 설정 → ~/.openviking/ov.conf
openviking-server doctor    # 설정·연결 진단 (반드시 실행)
openviking-server           # 서버 실행
```

### 방법 B: Docker (권장)

```bash
mkdir -p ~/.openviking && touch ~/.openviking/ov.conf
```

`docker-compose.yml`:

```yaml
services:
  openviking:
    image: ghcr.io/volcengine/openviking:latest
    # GitHub 접근 어려우면:
    # openviking-cn-beijing.cr.volces.com/volcengine/openviking:latest
    container_name: openviking
    ports:
      - "1933:1933"
    volumes:
      - ~/.openviking:/app/.openviking
    restart: unless-stopped
```

```bash
docker-compose up -d
```

- Docker 이미지는 **VikingBot + Web Studio 번들** 포함
- Web Studio: `http://localhost:1933/studio`
- VikingBot 끄기: `command: ["--without-bot"]` 또는 `OPENVIKING_WITH_BOT=0`

> **macOS 주의:** 기본적으로 `127.0.0.1`만 리스닝 → Docker on Mac에서 connection reset 발생 가능.
> 문서에 `socat` 포트포워딩 해법 제공 (호스트 1933 → 컨테이너 1934 → 내부 1933).

### 모델 설정 (`~/.openviking/ov.conf`)

**① 볼케이노 엔진 (공식 추천, 신규 무료 쿼터)**

```json
{
  "embedding": {
    "dense": {
      "provider": "volcengine",
      "model": "doubao-embedding-vision-251215",
      "api_key": "{your-api-key}",
      "api_base": "https://ark.cn-beijing.volces.com/api/v3",
      "dimension": 1024,
      "input": "multimodal"
    }
  },
  "vlm": {
    "provider": "volcengine",
    "model": "doubao-seed-2-0-lite-260428",
    "api_key": "{your-api-key}",
    "api_base": "https://ark.cn-beijing.volces.com/api/v3",
    "temperature": 0.0
  }
}
```

**② 완전 무료 (Ollama, API 키 불필요)**

```json
{
  "embedding": {
    "dense": {
      "provider": "ollama",
      "model": "nomic-embed-text",
      "api_base": "http://localhost:11434/v1",
      "dimension": 768,
      "input": "text"
    }
  }
}
```

### 지원 제공자 전체

| 제공자 | 설정값 | API 키 |
|---|---|---|
| Volcengine (Doubao) | `provider: volcengine` | 필요 |
| OpenAI | `provider: openai` | 필요 |
| **OpenAI Codex (OAuth)** | `provider: openai-codex` | **구독 로그인** (키 불필요) |
| Kimi Coding | `provider: kimi` | 구독 키 |
| GLM / Z.AI | `provider: glm` | 구독 키 |
| **Ollama (로컬)** | `provider: ollama` | **불필요** |
| 기타 OpenAI 호환 | `provider: openai` + `api_base` | 필요 |

> **최적 전략:** ChatGPT Plus/Pro 보유 시 `provider: openai-codex` OAuth → VLM 비용 0원.
> 임베딩은 Ollama → 0원. **총 추가 비용 없이** 운영 가능.
> Codex 토큰은 `~/.openviking/codex_auth.json`에 저장되며, 기존 Codex CLI 인증 파일에서 부트스트랩도 가능.

### Volcengine 구독 플랜 사용 시 api_base

| 플랜 | api_base |
|---|---|
| Agent Plan | `https://ark.cn-beijing.volces.com/api/plan/v3` |
| Coding Plan | `https://ark.cn-beijing.volces.com/api/coding/v3` |
| BytePlus ModelArk Coding | `https://ark.ap-southeast.bytepluses.com/api/coding/v3` |

### 기본 사용법 (`ov` CLI)

```bash
ov status                                   # 상태 확인

# 자료 추가 (GitHub / PDF / 웹페이지 / 로컬 파일)
ov add-resource https://github.com/volcengine/OpenViking
ov add-resource ./내문서.pdf
ov task status TASK_ID                      # 인덱싱 상태 (completed 까지 반복)

# 둘러보기
ov ls viking://resources/
ov tree viking://resources/volcengine -L 2

# 검색
ov find "openviking가 뭐야"
ov grep "openviking" --uri viking://resources/volcengine/OpenViking/docs/en

ov config                                   # 클라이언트 설정
ov chat                                     # VikingBot 채팅 (bot 설치 시)
```

### VikingBot 추가

```bash
pip install "openviking[bot]"
openviking-server --with-bot
ov chat   # 다른 터미널
```

### Claude Code 연동

**원라인 설치 (macOS / Linux)**

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/volcengine/OpenViking/main/examples/memory-plugin-shared/install.sh) --harness claude
```

언어(한/영/중), 다운로드 소스(GitHub 또는 TOS 미러), 자격증명을 대화형으로 질의.
재실행 안전(idempotent). GitHub 접근 어려우면 `--dist tos` 추가.
TOS 미러: `https://ovrelease.tos-cn-beijing.volces.com/memory-plugin-shared/install.sh`

**수동 설치 (4단계)**

```bash
# 1) 서버 확인
curl http://localhost:1933/health

# 2) 접속 정보 (~/.openviking/ovcli.conf) — 로컬 전용이면 생략 가능
cat > ~/.openviking/ovcli.conf <<'EOF'
{
  "url": "http://127.0.0.1:1933",
  "api_key": "<your-api-key>",
  "account": "my-team",
  "user": "alice"
}
EOF

# 3) 플러그인 설치 (원격 마켓플레이스)
claude plugin marketplace add https://raw.githubusercontent.com/volcengine/OpenViking/main/.claude-plugin/marketplace.json
claude plugin install openviking-memory@openviking

# 4) Claude Code 재시작
```

**개발 모드 (리포 수정하며 사용)**

```bash
# OpenViking 리포 루트에서
claude plugin marketplace add "$(pwd)/examples"
claude plugin install openviking-memory@openviking
```

> 디렉토리 모드 주의: 소스 디렉토리를 옮기거나 이름 변경·삭제하면 플러그인이 깨집니다.
> 두 모드 모두 `openviking` 이라는 마켓플레이스를 등록하므로 플러그인 id는 항상
> `openviking-memory@openviking`.

**디버깅**

```bash
export OPENVIKING_DEBUG=1
# 로그: ~/.openviking/logs/
```

스킬 `ov-memory-doctor` 로 기억 추출 문제 진단 가능.

### 프로덕션 배포

- Helm 차트: `deploy/helm/openviking`
- 멀티테넌트 + 사용자 격리 지원
- 리소스 ACL (opt-in)
- **localhost 밖으로 노출 전 반드시 인증 설정** (`docs/en/guides/04-authentication.md`)
- 관측: Prometheus + OpenTelemetry + Grafana (`examples/grafana/`)

---

## 10. 플러그인 / 스킬 / MCP 구분

### 결론: 전부 다다. **"서버"가 본체**이고 나머지는 접속 방법이다.

```
                OpenViking 서버 (본체, 포트 1933)
                           │
   ┌────────┬────────┬─────┴────┬────────┬────────┐
   ▼        ▼        ▼          ▼        ▼        ▼
① MCP   ② 스킬   ③ 훅플러그인  ④ CLI  ⑤ SDK  ⑥ HTTP API
```

| # | 형태 | 정체 | 특징 |
|---|---|---|---|
| ① | **MCP 서버** | `openviking/server/mcp_endpoint.py`, `/mcp` streamable HTTP | AI가 스스로 판단해 도구 호출. 도구 16개 |
| ② | **스킬** | `SKILL.md` 파일들 | AI에게 "이렇게 쓰라"고 교육 |
| ③ | **훅 플러그인** | `hooks.json` + `scripts/*.mjs` | **자동 실행.** 가장 강력 |
| ④ | **CLI** | Rust `ov` (`crates/ov_cli`) | 사람이 터미널에서 |
| ⑤ | **SDK** | Python / Go / TypeScript | 코드로 통합 |
| ⑥ | **HTTP API** | REST, 24개 라우터, 25개 API 문서 | 어떤 언어든 |

### MCP 도구 16개 (코드 실측)

| 도구 | 기능 |
|---|---|
| `find` | 빠른 의미 검색 |
| `search` | 세션 맥락 기반 심층 검색 |
| `read` | URI로 내용 읽기 |
| `list` | 디렉토리 목록 |
| `tree` | 트리 구조 |
| `grep` | 정확 문자열 검색 |
| `glob` | 패턴 매칭 |
| `remember` | 메시지를 기억으로 저장 |
| `write` | 내용 쓰기 |
| `edit` | 내용 수정 |
| `forget` | 삭제 |
| `add_resource` | 자료 추가 (URL/파일) |
| `list_watches` | 감시 목록 |
| `cancel_watch` | 감시 취소 |
| `health` | 상태 확인 |

### 훅 vs MCP (가장 중요한 구분)

| | **훅 (Hook)** | **MCP** |
|---|---|---|
| 실행 주체 | **하네스가 강제 실행** | AI가 선택해서 호출 |
| 토큰 비용 | **0** | 호출마다 소모 |
| 신뢰성 | **100%** | AI의 판단에 의존 |
| 지원 | Claude Code, Codex, Cursor, TRAE 등 | 모든 MCP 클라이언트 |

---

## 11. API 토큰 필요 여부

### 토큰/키는 3종류

| 구분 | 용도 | 필수? |
|---|---|---|
| **A. 모델 API 키** | 임베딩 + VLM 호출 | 거의 필수 (Ollama / Codex OAuth로 회피 가능) |
| **B. OpenViking 서버 키** | 내 서버 접속 인증 | 로컬 불필요, **원격 필수** |
| **C. 상용 라이선스 키** | Self-Managed 유료판 | 오픈소스판은 **불필요** |

### A. 모델 API 키 — 비용 조합

| 구성 | 임베딩 | VLM | 비용 |
|---|---|---|---|
| **완전 무료** | Ollama `nomic-embed-text` | Ollama 로컬 | **0원** (느림, 품질↓) |
| **구독 재활용 (추천)** | Ollama | Codex OAuth / Kimi / GLM | **0원 추가** |
| **가성비** | Doubao embedding-vision | Doubao seed-2.0-lite | 저렴, 신규 무료 쿼터 |
| **OpenAI** | text-embedding-3 | GPT-4V 계열 | 비쌈 |

> VLM `max_tokens` 주의: 메모리 추출은 전체 메모리 파일 재작성을 위해 **출력 ~32,768 토큰**이 필요.
> 모델 출력 한도가 낮으면(gpt-4o-mini = 16,384) `max_tokens`를 그 한도로 명시해야 잘림 방지.

### B. OpenViking 서버 인증

```
로컬 전용 (127.0.0.1:1933)  →  인증 불필요 (local mode)
원격 / 팀 공유               →  API 키 필수
```

서버 측 `ov.conf`:
```json
{ "server": { "root_api_key": "<생성한 키>" } }
```

클라이언트 측 `~/.openviking/ovcli.conf`:
```json
{
  "url": "https://my-ov.example.com",
  "api_key": "<키>",
  "account": "my-team",
  "user": "alice"
}
```

HTTP 헤더 매핑:

| 설정 | 헤더 |
|---|---|
| `api_key` | `X-API-Key` |
| `account` | `X-OpenViking-Account` |
| `user` | `X-OpenViking-User` |
| `actor_peer_id` | `X-OpenViking-Actor-Peer` |

> **localhost 밖으로 노출하기 전에 반드시 인증을 켤 것.** 안 켜면 누구나 기억을 읽고 쓸 수 있습니다.
> 추가 보안 기능: API 키 해싱(argon2), OAuth, LDAP, 암호화(cryptography), ACL.

### 자격증명 해석 우선순위 (플러그인 / CLI 공통)

1. 환경변수 — `OPENVIKING_URL` (또는 `OPENVIKING_BASE_URL`), `OPENVIKING_API_KEY`
   (또는 `OPENVIKING_BEARER_TOKEN`), `OPENVIKING_ACCOUNT`, `OPENVIKING_USER`, `OPENVIKING_PEER_ID`
2. `~/.openviking/ovcli.conf` — `url`, `api_key`, `account`, `user`, 그 다음 `plugin.agent_plugins` / `plugin` 키
   (경로 재정의: `OPENVIKING_CLI_CONFIG_FILE`)
3. `~/.openviking/ov.conf` — `agent_plugins` 섹션 → `server` 섹션 (`url` 또는 `host`/`port`, `root_api_key`)
   (경로 재정의: `OPENVIKING_CONFIG_FILE`)
4. 기본값 — `http://127.0.0.1:1933`, 인증 없음

> 설정 파일 변경은 프록시 재시작 없이 반영됩니다.

### C. 라이선스 키

| 에디션 | 라이선스 키 |
|---|---|
| **오픈소스 셀프호스팅** | **불필요** (README: "requires no activation key") |
| Managed SaaS (볼케이노 엔진) | 해당 없음 (호스팅 서비스) |
| Self-Managed 상용판 | 필요 (분산 배포 + 공식 지원) |

### 예상 월 비용 (개인 개발자)

| 항목 | 금액 |
|---|---|
| OpenViking 소프트웨어 | 0원 |
| 서버 (내 PC / VPS) | 0원 / 1~2만원 |
| 임베딩 (Ollama) | 0원 |
| VLM (Codex OAuth 구독 재활용) | 0원 추가 |
| **합계** | **0원 ~ 2만원** |

---

## 12. AI 에이전트 구축 활용도

### 결론: 매우 큼. **"기억" 파트를 통째로 아웃소싱**한다.

### 직접 만들면 필요한 작업 (= OpenViking이 대체해주는 것)

| # | 해야 할 일 | 난이도 | 직접 개발 기간 |
|---|---|---|---|
| 1 | 벡터DB 선정·구축·운영 | 중 | 1~2주 |
| 2 | 문서 파서 (PDF/MD/HTML/코드) | 중 | 2~3주 |
| 3 | 청킹 전략 + 계층 요약 생성 | 상 | 3~4주 |
| 4 | 대화에서 "기억할 것" 자동 추출 | **최상** | 4~8주 |
| 5 | 기억 중복제거·병합·갱신 | **최상** | 4~8주 |
| 6 | 검색 품질 튜닝 (리랭킹, 의도분석) | 상 | 3~6주 |
| 7 | 멀티테넌트 + ACL | 중 | 2~3주 |
| 8 | 세션 관리·압축·커밋 | 상 | 2~4주 |
| 9 | 관측·메트릭 | 하 | 1주 |
| | **총합** | | **5~10개월** |

특히 4·5번이 가장 어렵다. "다크모드 좋아해"를 어떻게 기억으로 뽑고, 나중에 "라이트모드로 바꿨어"가
오면 어떻게 갱신할지 — VikingMem 논문 주제이며 `openviking/session/compressor_v3.py` +
`memory_policy.py`에 구현되어 있다.

### 시나리오별 활용법

**① 사내 지식 챗봇**
```
회사 문서 → ov add-resource → viking://resources/
직원 질문 → find/search → 관련 L0/L1만 → LLM
직원 취향 → memories/preferences/ 자동 축적
```
얻는 것: RAG + 개인화 동시 해결. 멀티테넌트/ACL 내장 → 부서별 권한 분리.

**② 코딩 에이전트**
```
훅 플러그인 설치 → 자동 회상/캡처
experiences/   ← 에러 해결법 자동 누적
trajectories/  ← 성공한 작업 경로
skills/        ← 반복 작업 절차서
```
얻는 것: tau2-bench 기준 성공률 +6.87~11.87pp.

**③ 개인 AI 비서 (SaaS)**
```
멀티테넌트 (account/user 격리)
peers/                 ← 사용자 간 관계
identity.md / soul.md  ← 비서 성격 커스터마이즈
Python/Go/TS SDK 또는 HTTP API
```
얻는 것: 사용자별 기억 격리 인프라. **단 AGPLv3 주의.**

**④ LangChain / LangGraph 앱**
```
integrations/langchain/ 제공 → Tools + Store 인터페이스
```
얻는 것: 기존 Memory 교체만으로 벤치마크 개선.

**⑤ 멀티 에이전트 시스템**
```
SubagentStart/Stop 훅  ← 서브에이전트가 같은 기억 공유
peers/                 ← 에이전트 간 관계
connector/             ← 에이전트 간 라우팅·위임
```

### 리스크와 대응

| 리스크 | 설명 | 대응 |
|---|---|---|
| **AGPLv3** | SaaS 제공 시 소스 공개 의무 | 사내용 한정 / 상용 라이선스 / MIT 대안(Mem0) 검토 |
| **알파 단계** | `Development Status :: 3 - Alpha` | 핵심 로직에 직결하지 말고 보조로 시작 |
| **서버 의존** | 서버 다운 = 기억 상실 | 폴백 로직 설계 필수 |
| **기억 오염** | 잘못된 기억이 계속 영향 | `ov-memory-doctor`, `forget`, 주기 검수 |
| **지연** | 훅마다 네트워크 호출 (5~120s 타임아웃) | 로컬 서버 권장 |
| **중국 생태계 편중** | 기본 설정이 Doubao/Volcengine | OpenAI/Ollama 교체 가능하나 문서 적음 |

### 추천도

| 목적 | 평가 |
|---|---|
| 개인/사내 에이전트 | 매우 추천 |
| 프로토타입 / PoC | 매우 추천 (수개월 절약) |
| 연구 / 학습 | 매우 추천 (논문 3편 구현체) |
| 상용 SaaS | 신중 (AGPLv3) |
| 미션 크리티컬 | 신중 (알파 단계) |

### 경쟁 솔루션 비교

| 솔루션 | 특징 | OpenViking과 차이 |
|---|---|---|
| **Mem0** | AI 기억 전문, 간단 | 더 쉽지만 계층 로딩 없음, 폴더 구조 아님 |
| **Zep** | 대화 기억 + 시간 그래프 | 대화 중심. 문서/스킬 통합 약함 |
| **LangChain Memory** | 프레임워크 내장 | 단순. 장기 기억·경험 축적 거의 없음 |
| **Pinecone 등 벡터DB** | 순수 저장+검색 | 기억 추출·중복제거 로직 없음 (직접 구현) |
| **Claude 프로젝트 지식** | 간편, 설정 0 | 파일 업로드만. 자동 학습 없음 |
| **OpenViking** | 자료+기억+스킬 통합, 계층 로딩, 셀프호스팅 | 기능 최다, 무게 최대 |

> 포지셔닝: "AI 기억 솔루션 중 가장 본격적이고 가장 무거운 것."

---

## 13. React / PHP 연동 가능성

### Q. OpenViking 자체를 React/PHP로 재구현할 수 있나? → **불가능**

| 이유 | 설명 |
|---|---|
| 코드량 | 총 38만 줄 (Python 19만 + C++/Rust 12만 + TS/JS 7만) |
| C++ 엔진 | `src/` 12만 줄 — 벡터·스칼라 인덱스, 저장 엔진 |
| Rust 코어 | `crates/ragfs`, `crates/ov_cli` |
| 외부 의존성 | tree-sitter 11종, leveldb, croaring, scrapy, litellm, OpenTelemetry 등 |
| 논문 구현 | TrieHI 인덱스, VikingMem 추출 알고리즘 |
| 소요 | 재구현 시 수년 + 팀 규모 |

### Q. React/PHP 앱에서 OpenViking을 사용할 수 있나? → **완전히 가능**

OpenViking은 HTTP API 서버다. 언어 무관.

#### React / Next.js — 가장 잘 맞음

**① 공식 TypeScript SDK 있음** (`sdk/typescript/`)

**② 레퍼런스 구현이 이미 React** — `web-studio/`
```
web-studio/
├── src/              # React + Vite + TypeScript
├── components.json   # shadcn/ui
├── vite.config.ts
└── package.json
```
→ **셀프호스팅 가능한 React 프론트엔드가 이미 포함.** 이걸 포크하는 게 가장 빠른 길.

**③ Next.js 권장 구조**
```
React 프론트엔드 → Next.js API Route (서버사이드, 키 보관) → OpenViking 서버 :1933
```

```ts
// app/api/memory/search/route.ts
export async function POST(req: Request) {
  const { query } = await req.json();
  const res = await fetch(`${process.env.OV_URL}/api/v1/search`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-API-Key': process.env.OV_API_KEY!,   // 서버사이드에서만
      'X-OpenViking-User': 'alice',
    },
    body: JSON.stringify({ query, target_uri: 'viking://~/memories' }),
  });
  return Response.json(await res.json());
}
```

> **API 키를 브라우저에 절대 노출하지 말 것.** 반드시 API Route / Server Action에서 호출.

#### PHP — 공식 SDK는 없지만 문제 없음

공식 SDK는 Python / Go / TypeScript 3종. PHP는 없다. 하지만 HTTP API가 전부 문서화되어 있어
`curl` 또는 `Guzzle`로 간단히 구현 가능.

```php
<?php
class OpenVikingClient {
    public function __construct(
        private string $url    = 'http://127.0.0.1:1933',
        private string $apiKey = '',
        private string $user   = 'default'
    ) {}

    private function request(string $method, string $path, ?array $body = null): array {
        $ch = curl_init($this->url . $path);
        $headers = [
            'Content-Type: application/json',
            'X-OpenViking-User: ' . $this->user,
        ];
        if ($this->apiKey !== '') {
            $headers[] = 'X-API-Key: ' . $this->apiKey;
        }
        curl_setopt_array($ch, [
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_CUSTOMREQUEST  => $method,
            CURLOPT_HTTPHEADER     => $headers,
            CURLOPT_TIMEOUT        => 120,
        ]);
        if ($body !== null) {
            curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($body, JSON_UNESCAPED_UNICODE));
        }
        $raw  = curl_exec($ch);
        $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);
        if ($raw === false || $code >= 400) {
            throw new RuntimeException("OpenViking error ($code): $raw");
        }
        return json_decode($raw, true);
    }

    public function find(string $query, string $targetUri = 'viking://'): array {
        return $this->request('POST', '/api/v1/find', [
            'query'      => $query,
            'target_uri' => $targetUri,
        ]);
    }

    public function read(string $uri): array {
        return $this->request('POST', '/api/v1/read', ['uri' => $uri]);
    }

    public function addResource(string $path, string $reason = ''): array {
        return $this->request('POST', '/api/v1/resources', [
            'path'    => $path,
            'options' => ['reason' => $reason],
        ]);
    }

    public function health(): array {
        return $this->request('GET', '/health');
    }
}

// 사용 예
$ov = new OpenVikingClient('http://127.0.0.1:1933', getenv('OV_API_KEY'), 'alice');
$hits = $ov->find('환불 정책', 'viking://resources/');
foreach ($hits['results'] ?? [] as $hit) {
    echo $hit['uri'] . "\n";
}
```

> 위 코드는 **구조 예시**입니다. 정확한 엔드포인트 경로와 요청 스키마는 `docs/en/api/` 의 25개 문서
> (특히 `01-overview.md` 공통 규약, `06-retrieval.md` 검색 API)로 확정해야 합니다.

WordPress 플러그인도 같은 방식(`wp_remote_post()`)으로 가능.

### 권장 아키텍처

```
┌──────────────────────────────────────────────────────┐
│  프론트엔드: React / Next.js / Vue / PHP(Laravel)     │
│  ├─ 내 UI, 내 비즈니스 로직                            │
│  └─ 내 라이선스 (별도 프로세스)                        │
└─────────────────────┬────────────────────────────────┘
                      │ HTTP (API 키는 서버사이드 보관)
┌─────────────────────▼────────────────────────────────┐
│  OpenViking 서버 (Python, Docker) — 수정 없이 사용      │
│  └─ 단, SaaS 제공 시 AGPL §13 네트워크 조항 검토 필요    │
└──────────────────────────────────────────────────────┘
```

> **AGPLv3 주의:** OpenViking을 별도 프로세스로 두고 HTTP로만 호출하는 구조는 일반적으로 파생 저작물로
> 보지 않지만, **AGPL §13(네트워크를 통한 원격 상호작용)** 조항이 있어 외부 서비스 제공 시 해석이 갈립니다.
> 상용 서비스를 계획한다면 변호사 상담 또는 볼케이노 엔진 상용 라이선스 문의를 권합니다.
> (이 문서는 법률 조언이 아닙니다.)

### 정리

| 질문 | 답 |
|---|---|
| React로 재구현? | 불가능 (38만 줄 + C++/Rust) |
| PHP로 재구현? | 불가능 |
| React에서 사용? | 가능. TS SDK + `web-studio/` 가 React 레퍼런스 |
| PHP에서 사용? | 가능. HTTP API로 SDK 직접 작성 |
| 가장 빠른 길 | `web-studio/` 포크해서 커스터마이즈 |
| 비즈니스 기회 | **PHP/Laravel SDK 오픈소스 공개** → 생태계 공백 선점 |

---

## 14. 유튜브 강의 영상 제작 가능성

### 결론: 매우 좋은 소재. 지금이 최적 타이밍.

### 유리한 점

| 근거 | 내용 |
|---|---|
| 한국어 콘텐츠 사실상 0 | 거의 모든 자료가 영어/중국어 → 선점 가능 |
| 트렌드 정중앙 | "AI 기억", "에이전트 메모리"는 2026년 최대 화두 |
| 검색 수요 존재 | Trendshift 등재, GitHub 트렌딩 |
| 결과가 눈에 보임 | "AI가 어제 일을 기억한다" = 영상으로 극적 |
| 숫자가 강력 | "토큰 90% 절감", "24% → 82%" = 썸네일 재료 |
| 난이도 적당 | 설치는 어렵고 개념은 쉬움 → 강의 수요 발생 |
| 파생 주제 풍부 | MCP, 훅, RAG, 벤치마크, 논문 → 시리즈화 |
| 라이선스 무관 | 교육 콘텐츠는 AGPL 영향 없음 |

### 리스크와 대응

| 리스크 | 대응 |
|---|---|
| 알파 단계 → 버전마다 UI/설정 변경 | 영상에 버전 명시, 설명란 업데이트 |
| 설치 난이도 → 이탈률 | Docker 원클릭 편 별도 제작, GitHub에 설정 템플릿 |
| 중국 서비스 거부감 | Ollama/OpenAI 구성으로 촬영, "중국 API 불필요" 명시 |
| 니치 시장 (조회수 작음) | 조회수보다 전환율(강의/컨설팅)로 수익화 |
| 유료 모델 비용 설명 필요 | "0원 구성" 편을 초반 배치 |

### 추천 시리즈 구성 (12편)

| # | 제목 | 길이 | 핵심 |
|---|---|---|---|
| 1 | "AI가 어제 일을 기억한다면? 치매 걸린 AI 고치기" | 10분 | 후킹편. 문제 제기 + 비포/애프터 데모 |
| 2 | "OpenViking이 뭔가 — 바이트댄스가 만든 AI 기억 DB" | 15분 | 개요, L0/L1/L2, 벤치마크 |
| 3 | **"설치 완전 가이드 (Docker 10분)"** | 20분 | 조회수 1위 예상 |
| 4 | **"API 비용 0원으로 돌리기 (Ollama + Codex OAuth)"** | 15분 | 조회수 2위 예상 |
| 5 | **"Claude Code에 기억 심기 — 훅 플러그인"** | 20분 | 조회수 3위. 실전 체감 |
| 6 | "Cursor / Codex / OpenCode 연동" | 15분 | 다른 하네스 |
| 7 | "내 문서를 AI 지식으로 — add-resource 실전" | 15분 | PDF/GitHub/웹 인덱싱 |
| 8 | "토큰 90% 절감 실측 — 진짜인가?" | 15분 | 직접 측정, 신뢰도 상승 |
| 9 | "훅 vs MCP vs 스킬 — 뭘 써야 하나" | 12분 | 개념 정리 |
| 10 | "React/Next.js 앱에 붙이기" | 25분 | 개발자 타겟 |
| 11 | "PHP/Laravel에 붙이기 (SDK 직접 만들기)" | 25분 | 희소 콘텐츠 |
| 12 | "프로덕션 배포 — Docker, Helm, 인증, 모니터링" | 30분 | 고급 |

**숏츠 (각 60초)**
- "AI가 내 취향을 기억하는 순간" (데모 클립)
- "토큰 90% 줄이는 L0/L1/L2 3초 설명"
- "무료로 AI 기억 만드는 법"
- "바이트댄스가 오픈소스로 푼 것"

### 제작 팁

**썸네일 문구:** `24% → 82%`, `토큰 90% 절감`, `0원으로 AI 기억 만들기`, `AI 치매 고치기`

**영상 구조 (리텐션 최적화)**
```
0:00-0:15  결과 먼저 (AI가 기억하는 데모)
0:15-0:45  "어떻게 가능한가?" 호기심 유발
0:45-1:30  문제 설명 (AI가 왜 잊는가)
1:30-       본편
마지막 30초  다음 편 예고 + 구독 유도
```

**필수 준비물**
- GitHub에 설정 템플릿 리포 공개 (설명란 링크) → 이탈 방지 + 백링크
- 각 영상 버전 명시: "OpenViking 0.3.x 기준, 2026년 10월 촬영"
- 설명란 타임스탬프 챕터 (SEO + 리텐션)

**멀티 플랫폼**

| 플랫폼 | 콘텐츠 |
|---|---|
| YouTube | 본편 영상 |
| 블로그 / 벨로그 | 스크립트 → 텍스트 (SEO) |
| GitHub | 설정 템플릿, PHP SDK |
| 인프런 / 클래스101 | 유료 강의 패키징 |
| 뉴스레터 | AI 기억 트렌드 주간 |

---

## 15. 수익화 아이디어 상세

### 전제: AGPLv3가 전략을 결정한다

```
막힌 길: OpenViking을 그대로 호스팅해 SaaS 판매 → 소스 공개 의무 (AGPL §13)
열린 길: ① 교육  ② 서비스(대행/컨설팅)  ③ 사내 절감  ④ 주변부 제품  ⑤ 생태계 기여
```

### 1순위: 교육 콘텐츠 (즉시 시작, 리스크 0)

라이선스 영향 없음. 한국어 자료 사실상 0 → 선점 가능.

| 단계 | 상품 | 가격 | 월 목표 | 월 매출 |
|---|---|---|---|---|
| 1 | 유튜브 (유입용, 무료) | 0원 | 구독 3,000 | 광고 10~30만원 |
| 2 | 전자책 / 노션 템플릿 | 29,000원 | 30권 | 87만원 |
| 3 | 온라인 강의 (인프런 등) | 99,000원 | 20명 | 198만원 |
| 4 | 라이브 워크샵 (4h, 10명) | 150,000원 | 월 1회 | 150만원 |
| 5 | 1:1 멘토링 | 시간당 10만원 | 월 10시간 | 100만원 |
| | | | **합계** | **월 약 550만원** |

**구체 상품안**
1. "AI 기억 시스템 완전정복" 전자책 (150p) — 29,000원
2. "API 비용 0원 구성 가이드" — 무료 리드 매그넷 (이메일 수집)
3. 노션 템플릿 팩 (ov.conf 설정 모음, 트러블슈팅 체크리스트, 비용 계산기) — 19,000원
4. 인프런 강의 "AI 에이전트에 장기기억 심기" (6시간) — 99,000원
5. 기업 출강 반일 과정 — 300~500만원

### 2순위: 구축 대행 / 컨설팅 (단가 최고)

서비스 제공은 AGPL 공개 의무와 무관 (고객 인프라에 설치).

| 상품 | 범위 | 단가 |
|---|---|---|
| 설치 대행 (기본) | 서버 설치 + 모델 연결 + 1개 에이전트 연동 | 150~300만원 |
| 사내 지식베이스 구축 | 문서 인덱싱 + 권한 설정 + 챗봇 UI | 500~1,500만원 |
| 에이전트 기억 설계 | 기억 스키마 설계 + 추출 정책 튜닝 | 800~2,000만원 |
| 운영 유지보수 | 월 정액 (모니터링, 업데이트, 튜닝) | 월 50~200만원 |
| PoC 패키지 | 2주 단기 검증 | 400~800만원 |

**영업 포인트**
- "RAG 직접 만들면 5~10개월. 이거 쓰면 2주."
- "AI API 비용 월 $165 → $26. 연 200만원 절감."
- "VLDB/ICDE 논문 기반, 바이트댄스 프로덕션 검증."
- "셀프호스팅이라 데이터가 외부로 안 나갑니다." ← 금융/의료/공공 핵심

**타겟 고객**

| 대상 | 니즈 |
|---|---|
| AI 스타트업 | 기억 모듈 개발 기간 단축 |
| 중견기업 IT팀 | 사내 지식 챗봇, 데이터 주권 |
| 금융/의료/공공 | 외부 API 금지 → 셀프호스팅 필수 |
| SI / 개발사 | AI 제안서 차별화 요소 |
| 쇼핑몰 / CS센터 | 상담 이력 기억하는 챗봇 |

### 3순위: 사내 도입 (비용 절감 = 숨은 수익)

외부 제공 없으면 AGPL 의무 없음. 가장 안전.

| 절감 항목 | 연 효과 (개발자 5인 기준) |
|---|---|
| AI API 토큰 절감 (84%) | 연 약 1,000만원 |
| 중복 삽질 제거 (`experiences/`) | 인당 월 10h × 5명 × 12개월 = 연 600시간 |
| 온보딩 단축 | 신입 1명당 약 2주 |
| 문서 검색 시간 | 인당 주 3h → 0.5h |

→ 사내 성과 보고 → 승진/인센티브, 또는 사례화 → 컨설팅 포트폴리오.

### 4순위: 주변부 제품 (AGPL 회피 설계)

OpenViking을 수정하지 않고 별도 프로세스로 HTTP 호출만 하는 제품.

| 아이디어 | 설명 | 수익 모델 |
|---|---|---|
| ① **PHP/Laravel SDK** | 공식 SDK 공백. 오픈소스 공개 → 명성 → 컨설팅 유입 | 간접 (리드 생성) |
| ② 기억 관리 대시보드 | 더 좋은 UI, 기억 검수/편집/통계 | 유료 앱 월 1~3만원 |
| ③ ov.conf 설정 GUI | 설치 난이도가 최대 장벽 → 클릭으로 설정 생성 | 프리미엄 |
| ④ 비용 모니터링 도구 | 임베딩/VLM 호출 비용 추적·알림 | SaaS 월 1만원 |
| ⑤ 기억 품질 검수 SaaS | 기억 오염 탐지, 중복/모순 리포트 | 월 3~10만원 |
| ⑥ 원클릭 배포 템플릿 | Terraform/Helm/Railway/Fly.io | 5~15만원 |
| ⑦ 업종별 스킬 팩 | 법무/의료/쇼핑몰용 SKILL.md 번들 | 10~50만원 |
| ⑧ 한국어 기억 추출 프롬프트 팩 | 한국어 특화 extraction 템플릿 | 10~30만원 |
| ⑨ 마이그레이션 도구 | Mem0/Zep → OpenViking 데이터 이전 | 건당 200~500만원 |

> ②③⑤는 OpenViking 코드를 포함하지 않고 **API 클라이언트로만** 구현해야 안전.
> `web-studio/` 포크는 AGPL 적용 대상이므로 주의.

### 5순위: 생태계 기여 → 평판 → 수익

| 활동 | 효과 |
|---|---|
| 한국어 문서 번역 PR | 컨트리뷰터 등재 → 이력서·영업 자료 |
| 한국 모델 커넥터 기여 | Upstage Solar, Naver HyperCLOVA 어댑터 → 국내 수요 선점 |
| Discord / 커뮤니티 활동 | 공식 채널 전문가 포지션 |
| 블로그 기술 분석 | SEO 유입 → 컨설팅 문의 |
| 밋업 / 컨퍼런스 발표 | 네트워킹 → B2B 리드 |
| 한국 커뮤니티 운영 | 오픈카톡/디스코드 → 영업 파이프라인 |

### 실행 로드맵

**1~2개월: 기반 구축 (투자 단계, 수익 0원)**
- 직접 설치하고 2주 이상 실사용 (경험 축적이 자산)
- 비용 절감 실측 데이터 확보 (영업 무기)
- 유튜브 1~5편 제작
- GitHub에 설정 템플릿 리포 공개
- 블로그 3~5편

**3~4개월: 교육 수익화 (월 50~150만원)**
- 전자책 출간
- 무료 가이드로 이메일 리스트 300명
- 유튜브 10편, 구독 1,000명
- PHP SDK 오픈소스 공개 (화제성)

**5~8개월: 서비스 전환 (월 300~600만원)**
- 인프런 강의 출시
- 첫 구축 대행 수주 (포트폴리오 1건)
- 워크샵 개최
- 기업 문의 대응

**9~12개월: 스케일 (월 800~2,000만원)**
- 구축 대행 월 1~2건
- 유지보수 계약 2~3건 (반복 매출)
- 기업 출강
- 주변부 제품 1개 출시

### 최종 추천 조합

```
유튜브 (무료, 유입)
    ↓
전자책·템플릿 (3만원) ← 신뢰 구축
    ↓
온라인 강의 (10만원) ← 전문성 증명
    ↓
구축 대행 (수백만원) ← 실제 수익
    ↓
유지보수 계약 (월정액) ← 반복 수익
```

**핵심 전략:** 교육은 수익이 아니라 **영업 도구**다. 유튜브로 신뢰를 쌓고 구축 대행과
유지보수에서 실제 돈을 버는 구조가 가장 현실적.

### 리스크 관리

| 리스크 | 대응 |
|---|---|
| 프로젝트 알파 → 급변 | 특정 버전 고정 명시, 업데이트 영상 추가 |
| 더 쉬운 경쟁자 등장 (Mem0 등) | "비교 분석" 콘텐츠로 전환, 기술 중립 포지션 |
| AGPL 분쟁 | SaaS 재판매 금지, 상용 라이선스 문의 경로 확보 |
| 바이트댄스 정책 변경 | 포크 백업 보관, 대안 솔루션 지식 병행 |
| 시장 미개척 | 교육은 저비용 → 1~2개월 테스트 후 판단 |

> 라이선스·세무·법률 판단은 전문가 확인이 필요합니다. 위 내용은 기술적 분석이며 법률 조언이 아닙니다.

---

## 16. 결론 및 체크리스트

### 한 줄 결론

> OpenViking은 **AI 에이전트의 "기억"을 통째로 해결해주는 인프라**다.
> 직접 만들면 5~10개월 걸리는 것을 `pip install`과 설정 파일로 대체한다.
> 다만 **AGPLv3**와 **알파 단계**라는 두 가지 제약이 사업 전략을 좌우한다.

### 써야 하는 경우

| 상황 | 이유 |
|---|---|
| Claude Code / Cursor를 매일 쓴다 | 훅 플러그인 하나로 끝. 체감 효과 최대 |
| AI한테 같은 설명을 반복한다 | 바로 해결 |
| AI API 비용이 부담된다 | 토큰 절감 = 직접 절약 |
| 사내 문서가 많다 | `ov add-resource`로 지식베이스화 |
| AI 에이전트 제품을 만든다 | 기억 모듈 개발 기간 단축 |
| 데이터를 외부에 못 보낸다 | 셀프호스팅 |
| AI 기억 시스템을 공부하고 싶다 | 논문 3편 + 38만 줄 구현체 |

### 과한 경우

| 상황 | 이유 |
|---|---|
| AI를 가끔 쓴다 | 서버 상주 + 설정 비용 > 이득 |
| 서버 운영을 못 한다 | Docker/Python 서버 관리 필요 |
| 단순 문서 Q&A만 필요 | NotebookLM, 일반 RAG가 더 쉬움 |
| SaaS를 만들 계획 | AGPLv3 소스 공개 의무 |
| 완전 무료여야 한다 | 임베딩+VLM 비용 (Ollama로 회피 가능) |

### 시작 체크리스트

```
[ ] 1. Docker로 서버 띄우기
       mkdir -p ~/.openviking && touch ~/.openviking/ov.conf
       docker-compose up -d

[ ] 2. 모델 설정 (비용 0원 구성 추천)
       - 임베딩: Ollama nomic-embed-text
       - VLM: Codex OAuth (ChatGPT 구독 재활용) 또는 Doubao 무료 쿼터

[ ] 3. 진단
       openviking-server doctor
       curl http://localhost:1933/health

[ ] 4. Web Studio 확인
       http://localhost:1933/studio

[ ] 5. Claude Code 플러그인 설치
       bash <(curl -fsSL https://raw.githubusercontent.com/volcengine/OpenViking/main/examples/memory-plugin-shared/install.sh) --harness claude

[ ] 6. 자료 넣고 검색 테스트
       ov add-resource <내 프로젝트 저장소 URL>
       ov task status TASK_ID
       ov find "테스트 질의"

[ ] 7. 2주간 실사용 → 토큰 사용량 비포/애프터 기록
       (이 데이터가 모든 수익화의 출발점)

[ ] 8. 기억 검수
       ov-memory-doctor 스킬로 추출 품질 확인
```

### 주요 참고 링크

| 항목 | URL |
|---|---|
| 원본 저장소 | https://github.com/volcengine/OpenViking |
| 현재 저장소(포크) | https://github.com/bmshin94/OpenViking |
| 공식 사이트 | https://www.openviking.ai |
| 공식 문서 | https://docs.openviking.ai |
| 라이브 데모 | https://openviking.ai/studio |
| 블로그 | https://blog.openviking.ai |
| 벤치마크 리포트 | https://blog.openviking.ai/post/openviking-benchmark-results/ |
| 설계 배경 | https://blog.openviking.ai/post/openviking-context-database/ |
| Discord | https://discord.com/invite/eHvx8E9XF3 |
| X (Twitter) | https://x.com/openvikingai |
| Managed SaaS | https://www.volcengine.com/product/openviking-service |
| VikingMem 논문 | https://arxiv.org/abs/2605.29640 |
| Directory-Aware Query 논문 | https://arxiv.org/abs/2606.16903 |
| VikingRAG 논문 | https://arxiv.org/abs/2609.11390 |

### 저장소 내 주요 파일

| 경로 | 내용 |
|---|---|
| `README.md` | 프로젝트 전체 개요 (한국어 없음, en/zh/ja) |
| `examples/ov.conf.example` | 서버 설정 템플릿 404줄 — 모든 옵션 |
| `examples/ovcli.conf.example` | 클라이언트 설정 템플릿 |
| `docs/en/concepts/` | 핵심 개념 17편 |
| `docs/en/getting-started/02-quickstart.md` | 설치 퀵스타트 |
| `docs/en/guides/01-configuration.md` | 제공자 설정 가이드 |
| `docs/en/guides/04-authentication.md` | 인증 설정 (노출 전 필독) |
| `docs/en/api/` | HTTP API 문서 25편 |
| `examples/claude-code-memory-plugin/` | Claude Code 플러그인 |
| `examples/memory-plugin-shared/install.sh` | 7개 하네스 공통 설치 스크립트 |
| `web-studio/` | React 프론트엔드 레퍼런스 |
| `benchmark/` | 벤치마크 재현 스크립트 |
| `deploy/helm/openviking` | 쿠버네티스 Helm 차트 |

---

*이 문서는 OpenViking 저장소(`bmshin94/OpenViking`, 커밋 `212066e`)를 전수조사하여 작성되었습니다.*
*수치와 인용은 저장소 내 실제 파일에서 검증했으며, 벤치마크 수치는 프로젝트 자체 측정값입니다.*
*라이선스·법률 관련 내용은 참고용이며 법률 조언이 아닙니다.*
