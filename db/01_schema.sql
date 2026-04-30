-- ============================================================
-- 학사 정보 스키마 (student_db)
-- ============================================================

CREATE SCHEMA student_db;
SET search_path TO student_db, public;

-- 한글 이름 LIKE 검색을 빠르게 (선택)
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ── ENUM 타입 ───────────────────────────────────────────────
CREATE TYPE enrollment_status AS ENUM ('enrolled', 'leave', 'graduated', 'dropped');
CREATE TYPE instructor_status AS ENUM ('active', 'leave', 'retired');
CREATE TYPE semester_type     AS ENUM ('SPRING', 'SUMMER', 'FALL', 'WINTER');
CREATE TYPE letter_grade      AS ENUM
  ('A+','A','A-','B+','B','B-','C+','C','C-','D+','D','D-','F','P','NP','W');

-- ── 1. 학과 ─────────────────────────────────────────────────
CREATE TABLE departments (
  id          BIGSERIAL    PRIMARY KEY,
  code        VARCHAR(8)   UNIQUE NOT NULL,           -- 'CS', 'EE'
  name        VARCHAR(100) NOT NULL,                  -- '컴퓨터공학과'
  college     VARCHAR(100) NOT NULL,                  -- '공과대학'
  created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- ── 2. 교수 ─────────────────────────────────────────────────
CREATE TABLE instructors (
  id            BIGSERIAL    PRIMARY KEY,
  employee_no   VARCHAR(10)  UNIQUE NOT NULL,         -- 'P2024001'
  name          VARCHAR(50)  NOT NULL,
  email         VARCHAR(255) UNIQUE NOT NULL,
  department_id BIGINT       NOT NULL REFERENCES departments(id),
  hired_year    SMALLINT     NOT NULL CHECK (hired_year BETWEEN 1980 AND 2100),
  status        instructor_status NOT NULL DEFAULT 'active',
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_instructors_dept ON instructors(department_id);

-- ── 3. 학생 ─────────────────────────────────────────────────
CREATE TABLE students (
  id              BIGSERIAL    PRIMARY KEY,
  student_no      VARCHAR(10)  UNIQUE NOT NULL,       -- 학번 '20240001'
  name            VARCHAR(50)  NOT NULL,
  email           VARCHAR(255) UNIQUE NOT NULL,
  department_id   BIGINT       NOT NULL REFERENCES departments(id),
  admission_year  SMALLINT     NOT NULL CHECK (admission_year BETWEEN 2000 AND 2100),
  status          enrollment_status NOT NULL DEFAULT 'enrolled',
  created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_students_dept       ON students(department_id);
CREATE INDEX idx_students_status     ON students(status);
CREATE INDEX idx_students_name_trgm  ON students USING gin (name gin_trgm_ops);

-- ── 4. 강의 ─────────────────────────────────────────────────
CREATE TABLE courses (
  id            BIGSERIAL    PRIMARY KEY,
  code          VARCHAR(20)  UNIQUE NOT NULL,         -- 'CS101'
  title         VARCHAR(200) NOT NULL,
  credits       NUMERIC(2,1) NOT NULL CHECK (credits > 0),
  department_id BIGINT       NOT NULL REFERENCES departments(id),
  description   TEXT,
  created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);
CREATE INDEX idx_courses_dept ON courses(department_id);

-- ── 5. 수강 + 성적 ──────────────────────────────────────────
CREATE TABLE enrollments (
  id            BIGSERIAL     PRIMARY KEY,
  student_id    BIGINT        NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  course_id     BIGINT        NOT NULL REFERENCES courses(id)  ON DELETE RESTRICT,
  instructor_id BIGINT                 REFERENCES instructors(id),
  year          SMALLINT      NOT NULL CHECK (year BETWEEN 2000 AND 2100),
  semester      semester_type NOT NULL,
  grade         letter_grade,                          -- 진행 중이면 NULL
  grade_point   NUMERIC(2,1),                          -- 4.5 만점 환산
  enrolled_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),
  UNIQUE (student_id, course_id, year, semester)       -- 동일 학기 중복수강 차단
);
CREATE INDEX idx_enrollments_student    ON enrollments(student_id);
CREATE INDEX idx_enrollments_course     ON enrollments(course_id);
CREATE INDEX idx_enrollments_instructor ON enrollments(instructor_id);
CREATE INDEX idx_enrollments_term       ON enrollments(year, semester);

-- ── 뷰: 학생별 GPA ──────────────────────────────────────────
CREATE VIEW student_gpa AS
SELECT
  s.id          AS student_id,
  s.student_no,
  s.name,
  s.department_id,
  ROUND(AVG(e.grade_point) FILTER (WHERE e.grade_point IS NOT NULL)::numeric, 2) AS gpa,
  COUNT(*)       FILTER (WHERE e.grade IS NOT NULL)        AS completed_count,
  SUM(c.credits) FILTER (WHERE e.grade IS NOT NULL)        AS earned_credits
FROM students s
LEFT JOIN enrollments e ON e.student_id = s.id
LEFT JOIN courses     c ON c.id = e.course_id
GROUP BY s.id;

-- ── updated_at 자동 갱신 ────────────────────────────────────
CREATE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_students_touch
  BEFORE UPDATE ON students
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
