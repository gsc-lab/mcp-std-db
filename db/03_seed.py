"""
가상 학사 정보 시드 스크립트.

사용법:
    python db/03_seed.py                  # 기본 50명
    python db/03_seed.py --students 100
    python db/03_seed.py --students 30 --seed 7

특징:
- 기존 데이터를 TRUNCATE 후 새로 채우므로 idempotent (반복 실행 안전)
- --seed 인자로 같은 결과 재현 가능
- GPA / 상태 / 진행중 비율을 의도적으로 분포시켜 LLM 질문이 의미 있게 답변되게 함
"""
import argparse
import os
import random
import sys

import psycopg
from dotenv import load_dotenv

load_dotenv()

# ── 정적 마스터 데이터 ─────────────────────────────────────
DEPARTMENTS = [
    ("GSC", "글로벌시스템융합과", "글로벌융합대학"),
    ("NUR", "간호학과",           "보건의료대학"),
    ("SWF", "사회복지과",         "사회복지대학"),
    ("ME",  "기계계열",           "공학계열"),
    ("SPR", "스포츠재활과",       "보건복지대학"),
]

INSTRUCTORS_BY_DEPT = {
    "GSC": [("정영철", 2011), ("전상표", 2002), ("후카 가요코", 2015)],
    "NUR": [("정명희", 2010), ("백주연", 2014), ("유시연", 2019)],
    "SWF": [("정무원", 2012), ("장용주", 2017)],
    "ME":  [("이대섭", 2008), ("안상욱", 2013), ("박재필", 2016)],
    "SPR": [("김대한", 2011), ("이양선", 2018)],
}

COURSES_BY_DEPT = {
    "GSC": [
        ("GSC101", "일본어문법",     2.0),
        ("GSC201", "실무일본어",     3.0),
        ("GSC301", "딥러닝응용",     4.0),
        ("GSC401", "캡스톤디자인",   3.0),
    ],
    "NUR": [
        ("NUR101", "해부생리학",     3.0),
        ("NUR201", "기본간호학",     3.0),
        ("NUR301", "성인간호학",     3.0),
        ("NUR302", "정신간호학",     3.0),
        ("NUR401", "임상실습",       4.0),
    ],
    "SWF": [
        ("SWF101", "사회복지개론",   3.0),
        ("SWF201", "사회복지정책론", 3.0),
        ("SWF301", "사회복지실천론", 3.0),
        ("SWF401", "노인복지론",     3.0),
    ],
    "ME": [
        ("ME101",  "공업수학",       3.0),
        ("ME201",  "정역학",         3.0),
        ("ME301",  "동역학",         3.0),
        ("ME302",  "열역학",         3.0),
        ("ME401",  "기계설계",       3.0),
    ],
    "SPR": [
        ("SPR101", "운동해부학",     3.0),
        ("SPR201", "운동생리학",     3.0),
        ("SPR301", "스포츠손상학",   3.0),
        ("SPR401", "재활운동지도",   3.0),
    ],
}

SEMESTERS = [(2023, "FALL"), (2024, "SPRING"), (2024, "FALL"), (2025, "SPRING")]

# 한국식 이름 풀
SURNAMES = list("김이박최정강조윤장임한오서신권황송")
GIVEN_NAMES = [
    "민준", "서연", "도윤", "지우", "예준", "수아", "주원", "지은", "하준", "지유",
    "지호", "수빈", "건우", "하은", "준서", "윤서", "현우", "채원", "선우", "하린",
    "시우", "예린", "지훈", "유나", "성민", "다은", "재민", "소율", "서준", "은우",
]

LETTER_GRADE_TO_POINT = {
    "A+": 4.5, "A": 4.0, "A-": 3.7,
    "B+": 3.3, "B": 3.0, "B-": 2.7,
    "C+": 2.3, "C": 2.0, "C-": 1.7,
    "D+": 1.3, "D": 1.0, "D-": 0.7,
    "F": 0.0, "P": None, "NP": None, "W": None,
}

# (가중치, 등급 풀) — 학생을 GPA 클래스에 분배
GPA_CLASSES = [
    (0.10, ["A+", "A+", "A", "A", "A-", "B+"]),       # top
    (0.50, ["A", "A-", "B+", "B+", "B", "B-"]),       # high
    (0.30, ["B+", "B", "B-", "C+", "C", "C-"]),       # mid
    (0.10, ["C", "C-", "D+", "D", "D-", "F"]),        # low
]


def pickName(used: set[str]) -> str:
    for _ in range(200):
        n = random.choice(SURNAMES) + random.choice(GIVEN_NAMES)
        if n not in used:
            used.add(n)
            return n
    n = f"{random.choice(SURNAMES)}{random.choice(GIVEN_NAMES)}{len(used) + 1}"
    used.add(n)
    return n


def pickGradePool() -> list[str]:
    roll = random.random()
    acc = 0.0
    for weight, pool in GPA_CLASSES:
        acc += weight
        if roll <= acc:
            return pool
    return GPA_CLASSES[-1][1]


def pickStatus() -> str:
    r = random.random()
    if r < 0.80:
        return "enrolled"
    if r < 0.90:
        return "leave"
    if r < 0.98:
        return "graduated"
    return "dropped"


def buildDsn() -> str:
    return (
        f"host={os.getenv('POSTGRES_HOST', 'localhost')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'student_db')} "
        f"user={os.getenv('POSTGRES_USER', 'postgres')} "
        f"password={os.getenv('POSTGRES_PASSWORD', 'postgres')}"
    )


def main():
    parser = argparse.ArgumentParser(description="가상 학사 정보 시드")
    parser.add_argument("--students", type=int, default=50, help="학생 수 (기본 50)")
    parser.add_argument("--seed", type=int, default=42, help="random seed")
    args = parser.parse_args()

    random.seed(args.seed)

    print(f"[seed] connecting to {os.getenv('POSTGRES_HOST', 'localhost')}:"
          f"{os.getenv('POSTGRES_PORT', '5432')}/{os.getenv('POSTGRES_DB', 'student_db')}")

    with psycopg.connect(buildDsn()) as conn:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO student_db, public")

            # 0) 기존 데이터 비우기
            cur.execute(
                "TRUNCATE enrollments, courses, students, instructors, departments "
                "RESTART IDENTITY CASCADE"
            )

            # 1) 학과
            depts: dict[str, int] = {}
            for code, name, college in DEPARTMENTS:
                cur.execute(
                    "INSERT INTO departments (code, name, college) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (code, name, college),
                )
                depts[code] = cur.fetchone()[0]

            # 2) 교수
            instructors: dict[str, list[int]] = {dc: [] for dc in depts}
            empCounter = 1
            for deptCode, profList in INSTRUCTORS_BY_DEPT.items():
                for name, hiredYear in profList:
                    empNo = f"P2024{empCounter:03d}"
                    empCounter += 1
                    cur.execute(
                        "INSERT INTO instructors "
                        "(employee_no, name, email, department_id, hired_year) "
                        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                        (empNo, name, f"{empNo.lower()}@univ.kr", depts[deptCode], hiredYear),
                    )
                    instructors[deptCode].append(cur.fetchone()[0])

            # 3) 강의
            courses: list[tuple[int, str]] = []  # (course_id, dept_code)
            for deptCode, courseList in COURSES_BY_DEPT.items():
                for code, title, credits in courseList:
                    cur.execute(
                        "INSERT INTO courses (code, title, credits, department_id) "
                        "VALUES (%s, %s, %s, %s) RETURNING id",
                        (code, title, credits, depts[deptCode]),
                    )
                    courses.append((cur.fetchone()[0], deptCode))

            # 4) 학생
            usedNames: set[str] = set()
            students: list[tuple[int, str]] = []  # (student_id, dept_code)
            studentCounter = 1
            deptCodes = list(depts.keys())
            for i in range(args.students):
                deptCode = deptCodes[i % len(deptCodes)]
                admYear = random.choice([2021, 2022, 2023, 2024])
                studentNo = f"{admYear}{studentCounter:04d}"
                studentCounter += 1
                name = pickName(usedNames)
                cur.execute(
                    "INSERT INTO students "
                    "(student_no, name, email, department_id, admission_year, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (studentNo, name, f"{studentNo}@univ.kr",
                     depts[deptCode], admYear, pickStatus()),
                )
                students.append((cur.fetchone()[0], deptCode))

            # 5) 수강 + 성적
            for studentId, deptCode in students:
                gradePool = pickGradePool()
                myCourses = [c for c in courses if c[1] == deptCode]
                otherCourses = [c for c in courses if c[1] != deptCode]
                pool = myCourses + random.sample(otherCourses, k=min(3, len(otherCourses)))

                # 학생당 1~3 학기 수강
                takenSemesters = random.sample(SEMESTERS, k=random.randint(1, 3))
                for year, sem in takenSemesters:
                    perSem = min(random.randint(3, 5), len(pool))
                    selected = random.sample(pool, k=perSem)
                    for courseId, courseDept in selected:
                        # 마지막 학기는 30% 확률로 진행 중 (grade NULL)
                        isLast = (year, sem) == SEMESTERS[-1]
                        inProgress = isLast and random.random() < 0.3

                        if inProgress:
                            grade, gp = None, None
                        else:
                            grade = random.choice(gradePool)
                            gp = LETTER_GRADE_TO_POINT[grade]

                        instId = random.choice(instructors[courseDept]) \
                                 if instructors[courseDept] else None

                        cur.execute(
                            "INSERT INTO enrollments "
                            "(student_id, course_id, instructor_id, year, semester, grade, grade_point) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                            "ON CONFLICT (student_id, course_id, year, semester) DO NOTHING",
                            (studentId, courseId, instId, year, sem, grade, gp),
                        )

            # 요약
            cur.execute("SELECT COUNT(*) FROM departments");      dCnt = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM instructors");      iCnt = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM courses");          cCnt = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM students");         sCnt = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM enrollments");      eCnt = cur.fetchone()[0]
            cur.execute("SELECT ROUND(AVG(gpa)::numeric, 2) FROM student_gpa WHERE gpa IS NOT NULL")
            avgGpa = cur.fetchone()[0]

        conn.commit()

    print("\n[seed] 완료")
    print(f"  학과       : {dCnt}")
    print(f"  교수       : {iCnt}")
    print(f"  강의       : {cCnt}")
    print(f"  학생       : {sCnt}")
    print(f"  수강 기록  : {eCnt}")
    print(f"  전체 평균  : GPA {avgGpa}")


if __name__ == "__main__":
    sys.exit(main())
