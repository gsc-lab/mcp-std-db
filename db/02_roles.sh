#!/bin/bash
# MCP 서버 전용 읽기 전용 롤을 만든다.
# Postgres docker init 단계에서는 .sql 안의 환경변수 치환이 불편하므로 셸 스크립트로 작성한다.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  -- LOGIN 권한만 주고, SELECT 외 권한은 부여하지 않는다.
  CREATE ROLE mcp_reader LOGIN PASSWORD '${MCP_READER_PASSWORD}';

  -- 스키마 접근 권한과 모든 테이블/뷰의 SELECT 권한을 부여한다.
  GRANT USAGE  ON SCHEMA student_db TO mcp_reader;
  GRANT SELECT ON ALL TABLES IN SCHEMA student_db TO mcp_reader;

  -- 나중에 추가되는 테이블에도 자동으로 SELECT 권한을 부여한다.
  ALTER DEFAULT PRIVILEGES IN SCHEMA student_db
    GRANT SELECT ON TABLES TO mcp_reader;
EOSQL

echo "[init] mcp_reader role created with SELECT-only privileges."
