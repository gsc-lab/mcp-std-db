# web/ — student-mcp 의 웹 형태 (FastAPI + Vanilla HTML/JS)

**이 프로젝트의 본질 학습 (MCP ↔ Agent ↔ LLM 관계) 은 `agent/` CLI 에서 일어난다.**
`web/` 은 그 Agent 를 *웹 서비스로 감싸면 어떻게 되는지* 보여주는 **단일 데모** —
배포 형태 맛보기. FastAPI/세션/Redis 같은 웹 심화는 의도적으로 다루지 않는다
(그건 별도 웹 수업의 영역이고, 여기 쌓으면 MCP/Agent/LLM 학습을 가린다).

프론트는 vanilla HTML/CSS/JS (프레임워크 없음), 백엔드는 FastAPI.

## 단일 예제

| 폴더 | 무엇 |
|------|------|
| [v1_per_request/](v1_per_request) | MCP 의 3대 기본 요소(Tool/Resource/Prompt) 를 한 웹 UI 에 매핑한 완결 예제 |

매 요청마다 MCP 서버를 새로 spawn 하는 *학습용 단순화* 버전.
공유 세션 / 대화 메모리 같은 *서버 엔지니어링* 은 이 수업 범위 밖
(대화 메모리의 *개념* 은 `agent/03_repl.py` 에서 Redis 없이 깔끔하게 학습).

## 공통 사전 조건

1. Docker DB 실행:
   ```bash
   docker compose up -d
   ```
2. `.env` 에 `ANTHROPIC_API_KEY` 설정
3. 의존성 설치:
   ```bash
   pip install -r requirements.txt
   ```

## UI 구성

```
┌──────────────────────────────────────────┐
│ 답변 영역                                  │
├──────────────────────────────────────────┤
│ 📎 첨부된 자료 칩 (선택)                    │
│ [질문 입력란]   [📎] [📋] [보내기]          │
├──────────────────────────────────────────┤
│ 통신 로그                                 │
│  >> MCP / Claude API 메시지 흐름           │
└──────────────────────────────────────────┘
```

세 가지 진입점:

1. **자연어 질문** → 메인 [보내기]
2. **자료 첨부** (Resource) → 📎 모달에서 선택 → 칩으로 표시 → 메인 [보내기]
3. **Prompt 호출** → 📋 모달에서 카드 선택 + 인자 입력 → 카드 [보내기]

## CLI 단계와의 관계

| 단계 | 비교 가능한 곳 |
|------|--------------|
| `agent/00_raw_jsonrpc.py` | JSON-RPC 메시지를 직접 보고 싶을 때 |
| `agent/01_single_turn.py` | 도구 호출 1회 강제 패턴 |
| `agent/02_multi_turn.py` | 다중 호출 루프 (web 의 백엔드 흐름과 사실상 동일) |
| `agent/03_repl.py` | 대화 메모리 (messages 누적, Redis 없이) |
| `web/v1_per_request` | 02 의 웹 버전 + Tool/Resource/Prompt UI |
