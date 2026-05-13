# student-mcp

가상 학사 정보 DB(Postgres) → MCP 서버 → Claude Agent로 이어지는 학습 프로젝트.
**Stage 1 — DB 스키마 + 시드**.

## 폴더 구조

```
student-mcp/
├── docker-compose.yml      # postgres:16
├── .env.example            # → .env로 복사 후 사용
├── requirements.txt        # psycopg, python-dotenv
└── db/
    ├── 01_schema.sql       # 테이블/ENUM/뷰/인덱스 (init 시 자동 적용)
    ├── 02_roles.sh         # mcp_reader read-only 롤 생성 (init 시 자동 적용)
    └── 03_seed.py          # 가상 데이터 채우기 (수동 실행)
```

## 도메인

| 테이블 | 의미 | 주요 컬럼 |
|--------|------|----------|
| `departments` | 학과 | code, name, college |
| `instructors` | 교수 | employee_no, name, department_id, status |
| `students` | 학생 | student_no, name, department_id, admission_year, status |
| `courses` | 강의 | code, title, credits, department_id |
| `enrollments` | 수강+성적 | student_id, course_id, instructor_id, year, semester, grade |
| `student_gpa` (뷰) | 학생별 GPA | gpa, completed_count, earned_credits |

## 보안 패턴 — 두 개의 DB 롤

| 롤 | 권한 | 용도 |
|----|------|------|
| `postgres` | 모든 권한 | 시드/마이그레이션 (`03_seed.py`) |
| `mcp_reader` | **SELECT only** | MCP 서버가 LLM에게 노출하는 접속 (Stage 2) |

LLM이 잘못된 SQL을 만들어도 `mcp_reader`로는 DELETE/UPDATE/INSERT가 DB 레벨에서 거부됩니다.

## 실행

### 1. 환경변수 설정
```bash
cp .env.example .env
# 필요시 비밀번호 등 수정
```

### 2. Postgres + Adminer 띄우기
```bash
docker compose up -d
docker compose logs postgres   # init 스크립트 정상 실행 확인
```
첫 부팅 때 `01_schema.sql` → `02_roles.sh` 순으로 자동 실행됩니다.

DB 테이블을 브라우저로 확인: **http://localhost:8080**
- 시스템: `PostgreSQL`, 서버: `postgres`, 사용자/비밀번호: `.env` 의 값, 데이터베이스: `student_db`
- 시드(다음 단계) 까지 끝나면 `student_db` 스키마에서 5개 테이블과 `student_gpa` 뷰가 보입니다.

### 3. Python 의존성 설치
```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate

pip install -r requirements.txt
```

### 4. 시드 실행
```bash
python db/03_seed.py                 # 50명 (기본)
python db/03_seed.py --students 100  # 100명
python db/03_seed.py --seed 7        # random seed 고정
```

다시 돌리면 `TRUNCATE` 후 새로 채우므로 idempotent.

## 검증 쿼리

학과별 학생 수와 평균 GPA.

**bash / zsh** (`\` 줄 연결):
```bash
docker compose exec postgres psql -U postgres -d student_db -c \
  "SET search_path TO student_db; \
   SELECT d.name, COUNT(*) AS students, ROUND(AVG(g.gpa)::numeric, 2) AS avg_gpa \
   FROM students s \
   JOIN departments d ON d.id = s.department_id \
   LEFT JOIN student_gpa g ON g.student_id = s.id \
   GROUP BY d.name \
   ORDER BY avg_gpa DESC NULLS LAST;"
```

**PowerShell** (한 줄 — `\` 가 리터럴이라 줄 연결 안 됨):
```powershell
docker compose exec postgres psql -U postgres -d student_db -c "SET search_path TO student_db; SELECT d.name, COUNT(*) AS students, ROUND(AVG(g.gpa)::numeric, 2) AS avg_gpa FROM students s JOIN departments d ON d.id = s.department_id LEFT JOIN student_gpa g ON g.student_id = s.id GROUP BY d.name ORDER BY avg_gpa DESC NULLS LAST;"
```

read-only 롤도 확인 (SELECT는 통과, DELETE는 거부되어야 정상).

**bash / zsh:**
```bash
docker compose exec postgres psql -U mcp_reader -d student_db -c \
  "SELECT COUNT(*) FROM student_db.students;"
# → 카운트 정상 출력

docker compose exec postgres psql -U mcp_reader -d student_db -c \
  "DELETE FROM student_db.students;"
# → ERROR: permission denied
```

**PowerShell:**
```powershell
docker compose exec postgres psql -U mcp_reader -d student_db -c "SELECT COUNT(*) FROM student_db.students;"
# → 카운트 정상 출력

docker compose exec postgres psql -U mcp_reader -d student_db -c "DELETE FROM student_db.students;"
# → ERROR: permission denied
```

## 다음 단계 (Stage 2)

`server/main.py` — FastMCP 서버에서 `mcp_reader` 계정으로 접속, 학생 검색/조회/통계 도구를 노출.

`agent/run.py` — Anthropic SDK + MCP 클라이언트로 Claude가 직접 도구를 호출하는 루프.
