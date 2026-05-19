# web/v2_shared_session — 공유 MCP 세션 (FastAPI lifespan)

v1 과 기능은 동일. 차이는 **MCP 세션을 앱 시작 시 1회만 열고 모든 요청이 재사용**.
FastAPI 의 `lifespan` 컨텍스트 매니저로 자연스럽게 처리되어 v1 대비 라우트
응답 latency 가 크게 감소한다. 실서비스에서 채택하는 가장 단순한 패턴.

## v1 → v2 변화 (`diff -r web/v1_per_request web/v2_shared_session`)

코드 변화는 *세 곳에 집중*:

1. **lifespan 함수 추가** — 앱 시작 시 1회 세션 생성, 앱 종료 시 정리:
   ```python
   @asynccontextmanager
   async def lifespan(app: FastAPI):
       async with stdio_client(server_params()) as (read, write):
           async with ClientSession(read, write) as session:
               await session.initialize()
               app.state.mcp_session = session
               app.state.anthropic = AsyncAnthropic()
               yield   # 앱이 살아 있는 동안 여기서 대기
               # 종료 시 async with 들이 자동 정리
   ```
2. **`FastAPI(lifespan=lifespan)`** — 앱 생성 시 lifespan 등록
3. **라우트가 세션을 `request.app.state` 에서 가져옴** — `async with stdio_client(...)` 가 라우트 안에 없음

비즈니스 로직 (`run_chat`, `run_chat_with_prompt`, `discover_*`) 는 세션을 외부에서
받도록 시그니처만 바뀌었고 본문은 v1 과 동일.

## 학습 포인트

- **FastAPI `lifespan`** — 앱 라이프타임 자원(DB 연결, 외부 클라이언트 등) 의 표준 패턴
- **`async with` 안에 `yield`** — 비동기 컨텍스트 매니저의 lifetime 을 앱 라이프타임과 묶는 관용구
- **`app.state`** — 모든 라우트가 공유하는 상태 보관소
- **자동 정리** — 종료 시 `async with` 들이 깔끔하게 닫혀 graceful shutdown 제공

## 비용 비교 (대략)

| | v1 (요청별 spawn) | v2 (공유 세션) |
|---|---|---|
| MCP 서버 spawn | 매 요청 200~700ms | 0 (앱 시작 시 1회) |
| DB 연결 | 매 요청 새로 | 1회, 재사용 |
| ClientSession 초기화 | 매 요청 | 1회 |
| Anthropic 클라이언트 | 매 요청 새로 | 1회, 재사용 |
| **라우트당 인프라 비용** | **~300~900ms** | **~0ms** |

LLM 응답 시간(보통 1~5초) 외의 순수 인프라 비용.

## 실행

```bash
docker compose up -d                  # DB
# .env 의 ANTHROPIC_API_KEY 확인
python web/v2_shared_session/app.py
# 브라우저: http://localhost:5000
```

서버 콘솔에서 다음 출력이 보이면 정상:
```
[i] MCP 세션 초기화 중...
[i] MCP 세션 초기화 완료 — 모든 요청이 재사용
```

Ctrl+C 종료 시:
```
[i] MCP 세션 정리 중...
[i] MCP 세션 정리 완료
```

## 화면에서 보이는 차이

통신 로그의 첫 줄:
- v1: `** MCP 서버 시작 (요청별 spawn): .../server/main.py`
- v2: `** 기존 MCP 세션 재사용 (앱 시작 시 1회 초기화)`

이 한 줄이 아키텍처 차이를 보여주는 가장 명확한 흔적.

## 알려진 제약 (학습용)

- **세션 자동 복구 없음** — MCP 서버가 죽으면 앱 재시작 필요
- **단일 세션** — 진짜 고동시성이 필요하면 세션 풀로 발전 (학습 단계 밖)
- **자동 reloader 와 충돌 가능** — `uvicorn --reload` 사용 시 파일 변경마다 세션이 재초기화됨

## 더 발전된 패턴 (학습 단계 밖)

| 단계 | 무엇 |
|------|------|
| **v2 (현재)** | 영구 세션 1개 |
| 후속 1 | 세션 풀 (N 개, 라운드로빈 / 큐) |
| 후속 2 | HTTP transport 로 MCP 서버 분리 (별도 컨테이너) |
| 후속 3 | 자동 복구 / health check / graceful restart |
