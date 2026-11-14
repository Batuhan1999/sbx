#!/bin/sh
# The login and the database, each only if missing, so running it again
# changes nothing.
set -eu
. ./common.sh
db=$(python3 ./connection.py name)
password=$(python3 ./connection.py password)
connection_limit=${INPUT_CONNECTION_LIMIT:-20}
case "$connection_limit" in
  ''|*[!0-9]*) echo 'connection_limit must be an integer from 1 to 10000' >&2; exit 1 ;;
esac
[ "$connection_limit" -ge 1 ] && [ "$connection_limit" -le 10000 ] || {
  echo 'connection_limit must be an integer from 1 to 10000' >&2; exit 1;
}

admin_psql -v db="$db" -v pw="$password" -v template="$INPUT_TEMPLATE" -v owner="$INPUT_OWNER_ROLE" \
  -v createrole="$INPUT_CREATEROLE" -v replication="${INPUT_REPLICATION:-false}" -v connection_limit="$connection_limit" <<'SQL'
select format('create role %I login', :'db') where not exists (select from pg_roles where rolname = :'db') \gexec
select format('alter role %I login %s password %L', :'db',
              case when :'createrole'::boolean then 'createrole' else 'nocreaterole' end, :'pw') \gexec
select format('alter role %I %s', :'db',
              case when :'replication'::boolean then 'replication' else 'noreplication' end) \gexec
select format('alter role %I connection limit %s', :'db', :'connection_limit'::integer) \gexec
select format('alter role %I set tcp_keepalives_idle = 60', :'db') \gexec
select format('alter role %I set tcp_keepalives_interval = 10', :'db') \gexec
select format('alter role %I set tcp_keepalives_count = 3', :'db') \gexec
select format('grant %I to %I', :'owner', :'db')
  where :'owner' <> '' and not pg_has_role(:'db', :'owner', 'member') \gexec
select format('create database %I template %I strategy file_copy owner %I', :'db', :'template', :'db')
  where not exists (select from pg_database where datname = :'db') \gexec
select format('revoke connect on database %I from public', :'db') \gexec
SQL

python3 ./connection.py write
