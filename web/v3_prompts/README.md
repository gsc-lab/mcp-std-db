# web/v3_prompts — Tool 기반 chat + Resource 첨부 + Prompt 호출

v2 위에 서버 측 prompt 를 호출하는 UI 를 추가한 단계.
Claude Desktop 의 슬래시 메뉴와 동등한 흐름.

## v2 → v3 변화

### Backend (`app.py`)
- 새 라우트 `GET /api/prompts` — `list_prompts()` 결과(name, description, arguments) 반환
- `/api/ask` 본문에 새 필드 `prompt: {name, args}` 추가 — question 과 상호 배타
- 분기 처리:
  - `prompt` 가 있으면 → `prompts/get` 호출 → PromptMessage 묶음을 첫 대화 상태로
  - 없으면 → v2 의 question + attach 흐름 그대로
- 새 헬퍼 `prompt_message_to_anthropic` — PromptMessage → Anthropic message
- 다중 호출 루프는 `_run_multi_turn` 으로 추출해 두 흐름이 공유
- 다중 호출 루프 자체는 v1/v2 와 동일

### Frontend
- "📋" Prompt 버튼 추가 — 클릭 시 prompt 모달 표시
- prompt 모달: 각 prompt 마다 카드 1개
  - name + description
  - 인자가 있으면 입력 칸 (placeholder = 인자명)
  - 카드 자체의 [보내기] 버튼 — 클릭 시 모달 닫고 `/api/ask` 호출
- prompt 와 question 은 상호 배타 (카드 [보내기] 사용 시 textarea/chips 무시)

## 학습 포인트

| 진입점 | 누가 결정 | 통신 로그의 특징 |
|--------|----------|------------------|
| 자연어 질문 (메인 [보내기]) | 사용자가 질문 작성 | `tools/call` 만 |
| 자료 첨부 (📎 + 메인) | 사용자가 자료 선택 | `resources/read (사용자 첨부)` + `tools/call` |
| Prompt 호출 (📋 카드) | 서버가 메시지 묶음 제공 | `prompts/get` + PromptMessage 수신 |

같은 데이터를 세 가지 방식으로 분석할 수 있다 — *결정 권한이 어디 있느냐* 에
따라 신뢰성/재현성이 어떻게 달라지는지 비교 학습에 유용.

## 실행

```bash
docker compose up -d                  # DB
# .env 에 ANTHROPIC_API_KEY 설정 확인
python web/v3_prompts/app.py
# 브라우저: http://localhost:5000
```

## 예시 — 세 가지 방식 비교

1. **자연어**:
   ```
   "20220017 학생의 학사 경고 가능성 평가해줘" + [보내기]
   ```
   → LLM 이 search_students 등으로 학생을 찾고 추론.

2. **자료 첨부**:
   ```
   📎 → students://20220017 첨부 → "학사 경고 평가" + [보내기]
   ```
   → 사용자가 자료를 선택, LLM 은 그 자료를 받아 답변.

3. **Prompt 호출**:
   ```
   📋 → analyze_student_risk 카드 → student_no=20220017 → [보내기]
   ```
   → 서버가 자료 + 지시문을 한 묶음으로 제공.

세 방식의 답변 품질·일관성·재현성을 비교하면 prompt 가 실무에 쓰이는 이유가 보인다.
