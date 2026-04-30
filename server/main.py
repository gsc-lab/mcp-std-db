"""
Stage 2 — student-mcp 서버.

FastMCP를 사용해 Postgres(student_db) 위에 5개의 read-only 도구를 노출.

핵심 보안 패턴:
  DB 접속을 반드시 `mcp_reader` 롤로 한다 (SELECT only).
  LLM이 잘못된 SQL을 만들어내도 INSERT/UPDATE/DELETE는 DB 레벨에서 거부된다.
  → buildDsn() 에서 MCP_READER_USER 사용. POSTGRES_USER는 절대 쓰지 않는다.

실행:
  # (a) 개발 inspector — 브라우저로 도구 호출 테스트
  mcp dev server/main.py

  # (b) stdio 서버로 직접 실행 — Claude Desktop 등 클라이언트가 spawn
  python server/main.py
"""
from __future__ import annotations

import os
from typing import Any

import psycopg
from psycopg.rows import dict_row
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

mcp = FastMCP("student-mcp")


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


# ── 도구 1: 학생 검색 ─────────────────────────────────────────
@mcp.tool()
def search_students(
    name: str = "",
    department_code: str = "",
    status: str = "",
    limit: int = 20,
) -> list[dict]:
    """학생을 이름/학과코드/상태로 검색.

    Args:
        name: 이름 부분 일치 (빈 값이면 무시)
        department_code: 학과 코드 — 'GSC', 'NUR', 'SWF', 'ME', 'SPR'
        status: 'enrolled' | 'leave' | 'graduated' | 'dropped' (빈 값이면 무시)
        limit: 최대 결과 수 (기본 20, 최대 200)
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
    return queryRows(sql, tuple(params))


# ── 도구 2: 학생 상세 + 수강 내역 ─────────────────────────────
@mcp.tool()
def get_student_detail(student_no: str) -> dict:
    """학번으로 학생 1명의 기본정보 + GPA + 수강 내역 전체."""
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
        return {"error": f"학번 {student_no} 학생을 찾을 수 없습니다."}

    enrollments = queryRows(
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
    return {**base[0], "enrollments": enrollments}


# ── 도구 3: 강의 목록 ─────────────────────────────────────────
@mcp.tool()
def list_courses(department_code: str = "") -> list[dict]:
    """학과별(또는 전체) 강의 목록.

    Args:
        department_code: 학과 코드. 빈 값이면 전체 학과 강의.
    """
    if department_code:
        return queryRows(
            """
            SELECT c.code, c.title, c.credits::float8 AS credits,
                   d.code AS department_code
            FROM courses c
            JOIN departments d ON d.id = c.department_id
            WHERE d.code = %s
            ORDER BY c.code
            """,
            (department_code,),
        )
    return queryRows(
        """
        SELECT c.code, c.title, c.credits::float8 AS credits,
               d.code AS department_code
        FROM courses c
        JOIN departments d ON d.id = c.department_id
        ORDER BY d.code, c.code
        """
    )


# ── 도구 4: 학과별 통계 ───────────────────────────────────────
@mcp.tool()
def department_stats() -> list[dict]:
    """학과별 학생 수와 평균 GPA, 단과대학."""
    return queryRows(
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


# ── 도구 5: 상위 GPA 학생 ─────────────────────────────────────
@mcp.tool()
def top_students(department_code: str = "", limit: int = 5) -> list[dict]:
    """상위 GPA 학생 랭킹.

    Args:
        department_code: 학과 코드. 빈 값이면 전체에서 상위.
        limit: 최대 결과 수 (기본 5, 최대 50)
    """
    where = "WHERE g.gpa IS NOT NULL"
    params: list[Any] = []
    if department_code:
        where += " AND d.code = %s"
        params.append(department_code)
    params.append(min(max(limit, 1), 50))

    return queryRows(
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


if __name__ == "__main__":
    mcp.run()
