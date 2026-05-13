# web/v1_tools — Tool 기반 chat

`agent/02_multi_turn.py` 의 로직을 Flask 라우트 안에 옮긴 첫 단계.
콘솔에 찍히던 통신 로그를 브라우저 패널에 표시한다.

## 학습 목표

- MCP + Anthropic 다중 호출 루프를 Flask 라우트 안에서 실행
- 사용자 입력은 텍스트 한 칸 (자연어 질문)
- 백엔드는 02_multi_turn 과 같은 흐름으로 처리 후 답변 + 통신 로그 반환
- 프론트는 vanilla HTML/CSS/JS (프레임워크 없음)

## 실행

```bash
docker compose up -d                    # DB
# .env 에 ANTHROPIC_API_KEY 추가
pip install -r requirements.txt         # Flask 포함
python web/v1_tools/app.py
# 브라우저: http://localhost:5000
```

## 화면 구성

```
┌──────────────────────────────────────┐
│ student-mcp · web v1                 │
├──────────────────────────────────────┤
│ [답변 영역]                          │
│   Claude 의 최종 답변 + 라운드 수    │
├──────────────────────────────────────┤
│ [질문 입력] [보내기]                 │
├──────────────────────────────────────┤
│ 통신 로그                            │
│  >> 질문: ...                        │
│  << turn 1 — stop_reason=tool_use    │
│  >>   MCP tools/call: ...            │
│  ...                                 │
└──────────────────────────────────────┘
```

## API

| 메서드 | 경로 | 설명 |
|--------|------|------|
| GET | `/` | index.html |
| POST | `/api/ask` | `{question}` → `{answer, rounds, events}` |
| GET | `/api/health` | 헬스체크 |

## 02_multi_turn.py 와의 차이

| 영역 | CLI (02) | Web (v1) |
|------|----------|----------|
| 입력 채널 | `sys.argv` | HTTP POST 본문 |
| 출력 채널 | `print()` → stdout | JSON 응답 + 브라우저 DOM |
| 통신 로그 | `log()` → stdout | events 배열 → JSON → JS DOM |
| 종료 후 | 프로세스 종료 | 다음 요청 대기 |
| MCP 라이프사이클 | 1회 실행-소비-종료 | **요청마다 실행-소비-종료** (학습용 단순화) |

마지막 항목 — 매 요청마다 MCP 서버를 새로 실행합니다. 실서비스에선 비효율적이지만
학습 단계에선 흐름이 명료한 게 우선. 후속 단계에서 세션 재사용으로 발전 가능.

## 다음 단계

- **v2_resources**: 자료 첨부 UI 추가 (Desktop "+" 메뉴와 동등)
- **v3_prompts**: 서버 prompt 트리거 UI 추가 (Desktop 슬래시 메뉴와 동등)
