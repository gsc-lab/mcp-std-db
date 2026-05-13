# web/ — student-mcp 의 웹 형태 (Flask + Vanilla HTML/JS)

`agent/` 가 CLI 학습용이라면, `web/` 은 실제 배포 가능한 웹앱 형태로 단계별로 발전한다.
프론트는 vanilla HTML/CSS/JS (프레임워크 없음), 백엔드는 Flask 만.
MCP 와 LLM 의 상호작용에 집중하기 위해 웹 학습 부담은 최소화한다.

## 학습 단계

| 단계 | 폴더 | 추가되는 것 |
|------|------|-------------|
| v1 | [v1_tools/](v1_tools) | Tool 기반 chat — `agent/02_multi_turn.py` 의 웹 버전 |
| v2 | [v2_resources/](v2_resources) | 자료 첨부 버튼 (사용자가 Resource URI 명시 첨부) |
| v3 | [v3_prompts/](v3_prompts) | 슬래시/카드 UI (서버 prompt 호출 + 인자 폼) |

각 단계는 독립 폴더. 단계 간 변화를 보려면:
```bash
diff -r web/v1_tools web/v2_resources
diff -r web/v2_resources web/v3_prompts
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

## 폴더를 분리하는 이유

각 단계가 독립 완결. v1 만으로도 동작 가능, v2/v3 는 단순히 추가가 아니라 같은 앱을
점진적으로 확장한 결과. 폴더가 분리돼 있으면 `diff` 한 줄로 "이 단계에서 무엇이
추가됐는가" 가 명확히 보인다. 단점은 코드 중복 — 학습 자료라 변화 가시화 가치 우선.

## CLI 단계와의 대응

| 웹 단계 | 대응 CLI (agent/) | 차이 |
|---------|------------------|------|
| v1_tools | 02_multi_turn.py | 입출력 채널 (stdin/stdout → HTTP + 브라우저) |
| v2_resources | (CLI 없음 — 자료 첨부는 UI 가 자연스러움) | "첨부" 가 CLI 플래그가 아닌 UI 버튼 |
| v3_prompts | (CLI 없음 — prompt 호출도 UI 가 자연스러움) | "호출" 이 슬래시 메뉴 / 카드 |
