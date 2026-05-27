# web/v3_conversation — 대화 메모리 (Redis)

v2 (공유 MCP 세션) 위에 *대화 기억* 을 얹은 단계.
v1/v2 는 매 요청이 독립이라 "방금 답변에서 가장 낮은 학과는?" 같은 후속 질문을
못 알아들었다. v3 는 Redis 에 대화를 저장해 *여러 요청에 걸쳐 맥락을 유지* — 진짜 챗봇.

## v2 → v3 변화

### Backend (`app.py`)
- lifespan 에서 Redis 연결도 함께 생성 (`app.state.redis`)
- `load_session` / `save_session` — Redis get/set (`conv:{session_id}`, TTL 24h)
- `run_chat` 가 `prior_history` 를 받아 messages 앞에 붙임 → 이전 맥락 유지
- `/api/ask` 의 question 분기가 메모리 사용 (load → 실행 → save)
- `/api/reset` 라우트 추가 (대화 초기화)

### Frontend
- `session_id` 를 localStorage 에 보관, 요청 본문에 동봉
- 답변을 단일 영역이 아닌 *대화 스레드* 에 누적 (메모리 동작의 시각적 증거)
- "새 대화" 버튼 → `/api/reset` + 스레드 비우기 + 새 session_id

## 메모리 설계 (간결)

| 항목 | 선택 | 이유 |
|------|------|------|
| 저장소 | Redis | 대화 메모리의 실무 표준 — 빠름, TTL 내장 |
| key | `conv:{session_id}` | 세션별 분리 |
| TTL | 24시간 | 비활성 세션 자동 만료 (배치 청소 불필요) |
| 영속성 | `--appendonly yes` (AOF) | 재시작해도 대화 유지 |
| 저장 내용 | **가시 대화만** (질문 + 최종 답변 텍스트) | 도구 상호작용은 그 요청 한정 → 트리밍 안전 |
| 크기 관리 | sliding window (최근 20 turn) | 단순. tool 짝 깨질 일 없음 (텍스트만 저장하므로) |

### 왜 "가시 대화만" 저장하나

한 요청 안의 `tool_use` / `tool_result` 는 *그 답을 만들기 위한 작업 메모리*.
영구 대화에는 **사용자가 본 질문과 답변** 만 남기면 충분하다.

- 장점: 저장 크기 작음, 트리밍 시 tool_use/tool_result 짝 깨질 위험 없음
- 한계: 첨부 자료의 *원본* 은 다음 turn 에 안 남음 (답변은 남으므로 후속 질문 대부분 OK)

이건 ChatGPT 류 서비스도 쓰는 실무 패턴 — *내부 스크래치패드 ≠ 영구 대화*.

## prompt 호출은 메모리 미적용

📋 prompt 호출은 *독립 작업 트리거* 라 대화 메모리에 포함하지 않는다.
(일반 질문만 기억. prompt 는 매번 새 작업.)

## 실행

```bash
docker compose up -d        # postgres + adminer + redis
# .env 의 ANTHROPIC_API_KEY, REDIS_HOST/PORT 확인
python web/v3_conversation/app.py
# 브라우저: http://localhost:5000
```

## 데모 — 메모리 동작 확인

```
1) "GSC 학과의 평균 GPA 와 상위 3명"     → 답변
2) "그 학과 학생들의 공통점은?"          → "그 학과" = GSC 로 인식 (메모리!)
3) "방금 1등 학생의 수강 내역 보여줘"     → 1등 학생을 기억하고 조회
4) [새 대화] 클릭                        → 맥락 초기화
5) "그 학과가 어디였지?"                 → 모름 (대화 비워짐)
```

v2 에서 같은 시나리오를 하면 2번부터 "어느 학과요?" 라고 되묻는다 — 그 차이가 메모리.

## 통신 로그에서 보이는 차이

```
** 기존 MCP 세션 재사용 / 이전 대화 2 turn 복원   ← v3 신호
```

`이전 대화 N turn 복원` 이 메모리가 동작한다는 흔적.

## 알려진 한계 (학습용)

- **sliding window** — 20 turn 초과 시 오래된 대화 소실. 더 정교하려면 *요약 압축*.
- **단일 사용자 기준 session_id** — 인증 없음. 실서비스는 user_id + 다중 conversation.
- **첨부 원본 미보존** — 가시 대화만 저장하므로 첨부 자료 원본은 다음 turn 에 없음.

## 더 발전된 패턴 (학습 단계 밖)

| 단계 | 무엇 |
|------|------|
| **v3 (현재)** | Redis + sliding window |
| 후속 1 | 토큰 기반 트리밍 (turn 수가 아닌 토큰 예산) |
| 후속 2 | 오래된 대화 요약 압축 (LLM 으로) |
| 후속 3 | Redis(hot) + Postgres(영구 아카이브) hot/cold 분리 |
| 후속 4 | user_id 인증 + 다중 대화 스레드 |
