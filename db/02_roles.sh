#!/bin/bash
# MCP 서버 전용 read-only 롤 생성.
# Postgres docker init은 환경변수 치환을 .sql에서 못 해서 셸 스크립트로 작성.
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  -- 롤 생성 (LOGIN = 접속 가능, SELECT 외 권한은 부여하지 않음)
  CREATE ROLE mcp_reader LOGIN PASSWORD '${MCP_READER_PASSWORD}';

  -- 스키마 접근 + 모든 테이블/뷰에 SELECT
  GRANT USAGE  ON SCHEMA student_db TO mcp_reader;
  GRANT SELECT ON ALL TABLES IN SCHEMA student_db TO mcp_reader;

  -- 앞으로 추가될 테이블에도 자동으로 SELECT 부여 (마이그레이션 대비)
  ALTER DEFAULT PRIVILEGES IN SCHEMA student_db
    GRANT SELECT ON TABLES TO mcp_reader;
EOSQL

echo "[init] mcp_reader role created with SELECT-only privileges."
