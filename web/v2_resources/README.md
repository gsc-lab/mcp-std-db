# web/v2_resources — Tool 기반 chat + Resource 사용자 첨부

v1 위에 자료 첨부 UI 를 추가한 단계. Claude Desktop 의 "+" → 커넥터 → 자료 선택
흐름을 vanilla JS + Flask 로 재현한다.

## v1 → v2 변화 (`diff -r` 으로 비교 가능)

### Backend (`app.py`)
- 새 라우트 `GET /api/resources` — 정적 Resources + Resource Templates 목록 반환
- `POST /api/ask` 본문에 `attach: [uri, ...]` 배열 추가
- `run_agent_for_web(question)` → `run_agent_for_web(question, attach_uris)` — 인자 추가
- 다중 호출 루프 진입 전에 각 attach URI 를 `resources/read` 로 사전 조회
- 첫 user 메시지 = `[<resource uri="...">블록들] + [질문 텍스트]`
- 다중 호출 루프 자체는 v1 과 동일

### Frontend
- "📎" 첨부 버튼 추가 — 클릭 시 자료 모달 표시
- 모달: 정적 자료(클릭 1번에 첨부) + 템플릿 자료(인자 입력 + 첨부 버튼)
- 첨부된 URI 가 칩으로 입력란 위에 표시 — ✕ 로 개별 제거
- 보낼 때 `attach: [...]` 를 함께 전송
- 통신 로그에 `resources/read (사용자 첨부)` 가 LLM 의 Tool 호출과 별개로 표시

## 학습 포인트

- **Tool 과 Resource 의 결정 권한이 다르다** — Resource 는 사용자가 모달에서 선택,
  Tool 은 LLM 이 대화 중간에 호출. 코드에서도 `tools_for_claude` 에는 Tool 만 들어가고,
  Resource 는 첫 user 메시지의 content 블록으로 별도 전달된다.
- **첨부는 루프 진입 전** — 통신 로그에서 `resources/read` 가 `turn 1` 보다 앞에 나옴.
- **출처 유지** — 첨부 자료가 `<resource uri="...">` 마커로 감싸져 메시지에 포함되므로,
  LLM 이 답변할 때 출처를 인용할 수 있다.

## 실행

```bash
docker compose up -d                  # DB
# .env 에 ANTHROPIC_API_KEY 설정 확인
python web/v2_resources/app.py
# 브라우저: http://localhost:5000
```

## 예시

1. **첨부 없이 전송**:
   - "GSC 학과 평균 GPA" → Tool 만으로 답변
   - 통신 로그: `tools/call` 만 등장
2. **자료 1개 첨부**:
   - 📎 → 템플릿 자료 `students://{student_no}` 에 `20220017` 입력 → 첨부
   - 질문: "이 학생의 학사 경고 가능성"
   - 통신 로그: `resources/read (사용자 첨부)` 가 루프 시작 전에 등장, turn 1 에서 곧장 답변
3. **자료 2개 첨부**:
   - 📎 → `students://20220017` + `courses://GSC` 둘 다 첨부
   - 질문: "이 학생이 GSC 강의를 듣고 있나?"
   - 통신 로그: `resources/read` 두 번, 그 후 LLM 이 둘을 비교해 답변

## 다음 단계 — v3_prompts

서버 측 prompt 를 호출하는 모달 UI 추가. 사용자는 "📎 자료" 가 아니라
"📋 작업 흐름" 을 선택 → `prompts/get` 결과를 대화 시작 상태로 사용.
