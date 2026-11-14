#!/bin/sh
# Every sandbox database and login, one name per line; a login without its
# database is what a create cut short left.
set -eu
. ./common.sh
admin_psql -A -t -c "select datname from pg_database where datname like 'sbx\_%'
                     union select rolname from pg_roles where rolname like 'sbx\_%' order by 1"
