# web/v1_per_request — 요청별 MCP 세션 (FastAPI)

FastAPI + Vanilla HTML/JS 로 만든 Web Agent. **매 `/api/ask` 요청마다 MCP 서버를
새로 spawn** 하는 학습용 단순화 버전. 흐름이 가려지지 않아 학생이 한 요청의
라이프사이클 전체를 통신 로그에서 본다.

## 기능

세 가지 진입점이 한 페이지에:

| 진입점 | UI | 백엔드 |
|--------|----|----|
| 자연어 질문 | 텍스트박스 + 메인 [보내기] | `run_chat(question, [])` |
| 자료 첨부 + 질문 | 📎 모달 + 칩 + [보내기] | `run_chat(question, attach_uris)` |
| Prompt 호출 | 📋 모달 + 카드 [보내기] | `run_chat_with_prompt(name, args)` |

## 학습 포인트

- **FastAPI 의 async def 라우트** — Flask 의 `asyncio.run(...)` 같은 우회 없이 그대로 `await`
- **MCP 클라이언트 lifetime** — `async with stdio_client(...) as ...` 가 라우트 처리 중 살아 있고, 응답 후 자동 정리
- **세 가지 primitive 의 UI 매핑**:
  - Tool — LLM 이 자동 호출 (사용자 UI 없음)
  - Resource — 사용자가 모달에서 선택해 첨부
  - Prompt — 사용자가 모달에서 카드 선택 + 인자 입력
- **통신 로그의 형태** — 메인 [보내기] vs 📎 첨부 vs 📋 호출 시 와이어가 어떻게 다른지

## 실행

```bash
docker compose up -d                  # DB
# .env 에 ANTHROPIC_API_KEY 설정
pip install -r requirements.txt
python web/v1_per_request/app.py
# 브라우저: http://localhost:5000
```

## API

| 메서드 | 경로 | 본문 / 응답 |
|--------|------|------|
| GET | `/` | index.html |
| GET | `/api/health` | 헬스체크 |
| GET | `/api/resources` | `{static: [...], templates: [...]}` |
| GET | `/api/prompts` | `{prompts: [...]}` |
| POST | `/api/ask` | `{question, attach}` 또는 `{prompt: {name, args}}` → `{answer, rounds, events}` |

## 한계 (학습용 단순화)

매 요청마다 MCP 서버 spawn:
- 요청당 ~200~700ms 오버헤드 (Python 인터프리터 + 의존성 로딩 + DB 연결)
- 동시 사용자 늘면 시스템 부담

실무에선 lifespan 으로 세션을 한 번만 열어 재사용하거나, 세션 풀 / HTTP transport
등으로 발전시킨다. 단 그건 *웹/서버 엔지니어링* 영역이라 이 수업 범위 밖 — 여기선
"MCP Agent 를 웹으로 감싸면 이렇다" 까지만. MCP/Agent/LLM 의 본질은 `agent/` CLI 참조.
