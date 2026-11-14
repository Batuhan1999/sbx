#!/bin/sh
# After the sandbox paused: end its login's idle sessions, which a paused
# app's connection pool would otherwise hold open on the server. Sessions in
# a transaction stay.
set -eu
. ./common.sh
db=$(python3 ./connection.py name)
admin_psql -v db="$db" <<'SQL'
set statement_timeout = '5s';
select count(*) from (select pg_terminate_backend(pid) from pg_stat_activity
  where datname = :'db' and usename = :'db' and backend_type = 'client backend'
    and state = 'idle' and xact_start is null and pid <> pg_backend_pid()) closed;
SQL
