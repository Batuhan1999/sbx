# Sourced by the scripts: psql as the admin, stopping at the first error.
admin_psql() { psql "$PG_ADMIN_URL" -X -q -v ON_ERROR_STOP=1 "$@"; }
