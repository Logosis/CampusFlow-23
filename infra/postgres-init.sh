#!/bin/sh
set -eu
for module in d m1 m2 m3; do
  case "$module" in
    d) password="$D_DB_PASSWORD" ;;
    m1) password="$M1_DB_PASSWORD" ;;
    m2) password="$M2_DB_PASSWORD" ;;
    m3) password="$M3_DB_PASSWORD" ;;
  esac
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -v role="campusflow_$module" -v password="$password" <<'SQL'
CREATE ROLE :"role" LOGIN PASSWORD :'password';
CREATE DATABASE :"role" OWNER :"role";
REVOKE CONNECT ON DATABASE :"role" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"role" TO :"role";
SQL
done
