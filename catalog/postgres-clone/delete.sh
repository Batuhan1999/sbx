#!/bin/sh
# The database (ending its connections) and the login; what doesn't exist
# is fine.
set -eu
. ./common.sh
db=$(python3 ./connection.py name)
admin_psql -v db="$db" <<'SQL'
select format('drop database %I with (force)', :'db') where exists (select from pg_database where datname = :'db') \gexec
select format('drop role %I', :'db') where exists (select from pg_roles where rolname = :'db') \gexec
SQL
