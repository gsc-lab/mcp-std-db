"""
Stage 2 — student-mcp 서버 (옵션 B: Tools + Resources 하이브리드).

설계 의도:
  - DB 접근은 직접 SQL 유지 (학습 surface — ORM 도입 안 함, CLAUDE.md 결정)
  - Tools: 동적 검색/집계 — LLM이 대화 흐름 보고 호출 시점/인자 판단
  - Resources: ID 단일 조회 / 정적 마스터 — URI로 식별되는 자료

핵심 보안 패턴:
  반드시 mcp_reader 롤(SELECT only)로 DB 접속. POSTGRES_USER 절대 사용 X.
  → buildDsn() 의 user 라인이 보안의 절반. 02_roles.sh 의 GRANT 정책이 나머지 절반.

실행:
  mcp dev server/main.py        # 브라우저 inspector
  python server/main.py         # stdio 서버 (Claude Desktop 등이 spawn)
"""
import json
import os
from dataclasses import asdict, dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.prompts.base import UserMessage
from mcp.types import EmbeddedResource, TextResourceContents

load_dotenv()

mcp = FastMCP("student-mcp")


# ════════════════════════════════════════════════════════════
# DB 헬퍼
# ════════════════════════════════════════════════════════════

def buildDsn() -> str:
    return (
        f"host={os.getenv('POSTGRES_HOST', 'localhost')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'student_db')} "
        f"user={os.getenv('MCP_READER_USER', 'mcp_reader')} "
        f"password={os.getenv('MCP_READER_PASSWORD', '')}"
    )


def queryRows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with psycopg.connect(buildDsn(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO student_db, public")
            cur.execute(sql, params)
            return cur.fetchall()


def jsonDump(value: Any) -> str:
    """dataclass(es) → JSON 문자열 (한글 그대로 유지). Resource 반환 직렬화용."""
    if isinstance(value, list):
        out = [asdict(v) if hasattr(v, "__dataclass_fields__") else v for v in value]
    elif hasattr(value, "__dataclass_fields__"):
        out = asdict(value)
    else:
        out = value
    return json.dumps(out, ensure_ascii=False, indent=2)


# ════════════════════════════════════════════════════════════
# 결과 모델 (dataclass) — SQL 쿼리 결과의 모양을 한 곳에 명시
# ════════════════════════════════════════════════════════════

@dataclass
class DepartmentRow:
    code: str
    name: str
    college: str


@dataclass
class DepartmentStat:
    code: str
    name: str
    college: str
    student_count: int
    avg_gpa: float | None  # 학생/성적 0건 학과면 None


@dataclass
class StudentSummary:
    student_no: str
    name: str
    department_code: str
    department_name: str
    admission_year: int
    status: str


@dataclass
class EnrollmentRow:
    course_code: str
    course_title: str
    credits: float
    year: int
    semester: str
    grade: str | None         # 진행 중이면 None
    grade_point: float | None
    instructor: str | None


@dataclass
class StudentDetail:
    student_no: str
    name: str
    email: str
    department_code: str
    department_name: str
    admission_year: int
    status: str
    gpa: float | None
    completed_count: int | None
    earned_credits: float | None
    enrollments: list[EnrollmentRow]


@dataclass
class CourseRow:
    code: str
    title: str
    credits: float
    department_code: str


@dataclass
class TopStudentRow:
    student_no: str
    name: str
    department_code: str
    gpa: float
    completed_count: int
    earned_credits: float


# ════════════════════════════════════════════════════════════
# Tools — 동적 검색/집계 (LLM이 호출 시점/인자 결정)
# ════════════════════════════════════════════════════════════

@mcp.tool()
def search_students(
    name: str = "",
    department_code: str = "",
    status: str = "",
    limit: int = 20,
) -> list[StudentSummary]:
    """학생을 이름/학과/상태로 검색.

    Args:
        name: 이름 부분 일치 (빈 값이면 무시, ILIKE %name%)
        department_code: 'GSC' | 'NUR' | 'SWF' | 'ME' | 'SPR' (빈 값이면 무시)
        status: 'enrolled' | 'leave' | 'graduated' | 'dropped' (빈 값이면 무시)
        limit: 1 ~ 200 (기본 20)
    """
    conditions: list[str] = []
    params: list[Any] = []

    if name:
        conditions.append("s.name ILIKE %s")
        params.append(f"%{name}%")
    if department_code:
        conditions.append("d.code = %s")
        params.append(department_code)
    if status:
        conditions.append("s.status = %s")
        params.append(status)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT s.student_no, s.name,
               d.code AS department_code, d.name AS department_name,
               s.admission_year, s.status::text AS status
        FROM students s
        JOIN departments d ON d.id = s.department_id
        {where}
        ORDER BY s.student_no
        LIMIT %s
    """
    params.append(min(max(limit, 1), 200))
    rows = queryRows(sql, tuple(params))
    return [StudentSummary(**r) for r in rows]


@mcp.tool()
def top_students(department_code: str = "", limit: int = 5) -> list[TopStudentRow]:
    """상위 GPA 학생 랭킹.

    Args:
        department_code: 학과 코드 (빈 값이면 전체에서 상위)
        limit: 1 ~ 50 (기본 5)
    """
    where = "WHERE g.gpa IS NOT NULL"
    params: list[Any] = []
    if department_code:
        where += " AND d.code = %s"
        params.append(department_code)
    params.append(min(max(limit, 1), 50))

    rows = queryRows(
        f"""
        SELECT s.student_no, s.name,
               d.code AS department_code,
               g.gpa::float8 AS gpa,
               g.completed_count,
               g.earned_credits::float8 AS earned_credits
        FROM student_gpa g
        JOIN students s ON s.id = g.student_id
        JOIN departments d ON d.id = s.department_id
        {where}
        ORDER BY g.gpa DESC
        LIMIT %s
        """,
        tuple(params),
    )
    return [TopStudentRow(**r) for r in rows]


@mcp.tool()
def department_stats() -> list[DepartmentStat]:
    """학과별 학생 수와 평균 GPA, 단과대학."""
    rows = queryRows(
        """
        SELECT d.code, d.name, d.college,
               COUNT(s.id) AS student_count,
               ROUND(AVG(g.gpa)::numeric, 2)::float8 AS avg_gpa
        FROM departments d
        LEFT JOIN students s ON s.department_id = d.id
        LEFT JOIN student_gpa g ON g.student_id = s.id
        GROUP BY d.code, d.name, d.college
        ORDER BY avg_gpa DESC NULLS LAST
        """
    )
    return [DepartmentStat(**r) for r in rows]


# ════════════════════════════════════════════════════════════
# Resources — URI로 식별되는 자료
#   departments://all          정적 마스터
#   courses://{dept}           학과별 강의
#   students://{student_no}    학생 1명 상세
# ════════════════════════════════════════════════════════════

@mcp.resource("departments://all", mime_type="application/json")
def res_departments() -> str:
    """학과 마스터 — 5개 학과 (정적 자료)."""
    rows = queryRows(
        """
        SELECT code, name, college
        FROM departments
        ORDER BY code
        """
    )
    return jsonDump([DepartmentRow(**r) for r in rows])


@mcp.resource("courses://{department_code}", mime_type="application/json")
def res_courses(department_code: str) -> str:
    """학과별 강의 목록. URI 예: courses://GSC"""
    rows = queryRows(
        """
        SELECT c.code, c.title,
               c.credits::float8 AS credits,
               d.code AS department_code
        FROM courses c
        JOIN departments d ON d.id = c.department_id
        WHERE d.code = %s
        ORDER BY c.code
        """,
        (department_code,),
    )
    return jsonDump([CourseRow(**r) for r in rows])


@mcp.resource("students://{student_no}", mime_type="application/json")
def res_student_detail(student_no: str) -> str:
    """학번으로 학생 1명의 기본정보 + GPA + 수강 내역. URI 예: students://20240001"""
    base = queryRows(
        """
        SELECT s.student_no, s.name, s.email,
               d.code AS department_code, d.name AS department_name,
               s.admission_year, s.status::text AS status,
               g.gpa::float8 AS gpa,
               g.completed_count,
               g.earned_credits::float8 AS earned_credits
        FROM students s
        JOIN departments d ON d.id = s.department_id
        LEFT JOIN student_gpa g ON g.student_id = s.id
        WHERE s.student_no = %s
        """,
        (student_no,),
    )
    if not base:
        return jsonDump({"error": f"학번 {student_no} 학생을 찾을 수 없습니다."})

    enrollRows = queryRows(
        """
        SELECT c.code AS course_code, c.title AS course_title,
               c.credits::float8 AS credits,
               e.year, e.semester::text AS semester,
               e.grade::text AS grade,
               e.grade_point::float8 AS grade_point,
               i.name AS instructor
        FROM enrollments e
        JOIN students s ON s.id = e.student_id
        JOIN courses c ON c.id = e.course_id
        LEFT JOIN instructors i ON i.id = e.instructor_id
        WHERE s.student_no = %s
        ORDER BY e.year DESC, e.semester
        """,
        (student_no,),
    )

    detail = StudentDetail(
        **base[0],
        enrollments=[EnrollmentRow(**r) for r in enrollRows],
    )
    return jsonDump(detail)


# ════════════════════════════════════════════════════════════
# Prompts — 재사용 가능한 작업 흐름 (사용자가 슬래시 메뉴에서 선택)
#
# 설계 패턴 두 가지를 변주:
#   (A) Server-side pre-fetch + EmbeddedResource 박제
#       — 템플릿 Resource(students://{...}, courses://{...}) 처럼 Desktop
#         picker에 안 뜨는 자료를, prompt 안에서 서버가 직접 읽어 첨부.
#       — LLM은 별도 호출 없이 데이터를 손에 쥔 채 시작.
#   (B) Tool 호출 유도 텍스트
#       — prompt가 단순 지시문만 반환. LLM이 적절한 Tool을 알아서 부름.
# ════════════════════════════════════════════════════════════

def embedResource(uri: str, jsonText: str) -> EmbeddedResource:
    """JSON 문자열을 EmbeddedResource(application/json) 로 감싼다."""
    return EmbeddedResource(
        type="resource",
        resource=TextResourceContents(
            uri=uri,
            mimeType="application/json",
            text=jsonText,
        ),
    )


@mcp.prompt()
def analyze_student_risk(student_no: str) -> list[UserMessage]:
    """학번을 받아 학사 경고 가능성을 분석. 학생 상세 자료를 서버가 미리 첨부.

    Args:
        student_no: 학번 (예: 20240001)
    """
    uri = f"students://{student_no}"
    return [
        UserMessage(content=embedResource(uri, res_student_detail(student_no))),
        UserMessage(content=(
            f"위 학생({student_no}) 의 학사 경고 가능성을 평가해줘.\n"
            "- GPA 수준\n"
            "- 누적 이수 학점\n"
            "- 미이수(F/W) 과목 비율\n"
            "세 가지를 종합해 위험도(낮음/보통/높음) 와 근거를 단계별로 제시할 것."
        )),
    ]


@mcp.prompt()
def course_catalog(department_code: str) -> list[UserMessage]:
    """학과 강의 카탈로그를 첨부하고 정리를 요청.

    Args:
        department_code: 학과 코드 (GSC | NUR | SWF | ME | SPR)
    """
    uri = f"courses://{department_code}"
    return [
        UserMessage(content=embedResource(uri, res_courses(department_code))),
        UserMessage(content=(
            f"위 {department_code} 학과의 강의 목록을 다음 기준으로 정리:\n"
            "1) 코드 번호로 학년 추정 (예: 1xx=1학년)\n"
            "2) 학년별로 표로 묶기\n"
            "3) 각 강의의 학점도 함께 표기"
        )),
    ]


@mcp.prompt()
def compare_departments() -> str:
    """학과별 학생 수/평균 GPA 를 비교 분석 (Tool 호출 유도 패턴)."""
    return (
        "department_stats 도구를 호출해 학과별 학생 수와 평균 GPA를 가져온 뒤, "
        "아래 관점으로 비교 분석해줘:\n"
        "1) 평균 GPA 가 가장 높은 학과와 가장 낮은 학과\n"
        "2) 학생 수와 평균 GPA 의 상관관계 (대략적 경향)\n"
        "3) 단과대학(college) 단위로 묶었을 때 보이는 패턴\n"
        "근거가 되는 숫자를 본문에 인용할 것."
    )


if __name__ == "__main__":
    mcp.run()
