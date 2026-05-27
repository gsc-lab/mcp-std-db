# web/ — student-mcp 의 웹 형태 (FastAPI + Vanilla HTML/JS)

`agent/` 가 CLI 학습용이라면, `web/` 은 학생이 *직접 만든 Web Agent* 로 MCP 의
3대 기본 요소 (Tool / Resource / Prompt) 가 UI 에 어떻게 매핑되는지 학습한다.

프론트는 vanilla HTML/CSS/JS (프레임워크 없음), 백엔드는 FastAPI.
MCP 와 LLM 의 상호작용에 집중하기 위해 웹 학습 부담은 최소.

## 학습 단계

| 단계 | 폴더 | 무엇 |
|------|------|------|
| v1 | [v1_per_request/](v1_per_request) | 요청별 MCP 세션 — 매 요청마다 spawn (학습용 단순화) |
| v2 | [v2_shared_session/](v2_shared_session) | 공유 MCP 세션 — lifespan 으로 앱 시작 시 1회만 (실무 패턴) |
| v3 | [v3_conversation/](v3_conversation) | 대화 메모리 — Redis 로 이전 대화 기억 (진짜 챗봇) |

- v1 → v2: **아키텍처** 변화 (세션 생성 위치). 기능 동일.
- v2 → v3: **기능** 추가 (대화 메모리). 매 요청 독립 → 맥락 유지.

```bash
diff -r web/v1_per_request web/v2_shared_session   # lifespan 도입
diff -r web/v2_shared_session web/v3_conversation  # Redis 메모리 레이어
```

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

## UI 구성 (v1, v2 동일)

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
| `web/v1_per_request` | 02 의 웹 버전 (요청별 spawn) |
| `web/v2_shared_session` | v1 의 실무 패턴 발전 (lifespan 공유 세션) |
