"""sbx-control's state: one SQLite file on the host (paths.database()).
Sandboxes themselves live in the runtime.

Every module that keeps rows declares its tables as a Schema; migrate()
creates and upgrades all of them at every start (the server's lifespan,
host CLIs, the installer)."""
import contextlib
import importlib
import json
import re
import secrets
import sqlite3
import sys
from dataclasses import dataclass
from typing import Callable

from . import paths


@dataclass(frozen=True)
class Schema:
    """What one module keeps in sbx.db."""
    tables: str = ""                                  # create table if not exists ...; index too
    columns: tuple[tuple[str, str, str], ...] = ()    # (table, column, definition) added after a release
    dropped_columns: tuple[tuple[str, str], ...] = ()
    dropped_tables: tuple[str, ...] = ()
    upgrade: Callable | None = None                   # upgrade(conn): data carried over, idempotent
    sealed: tuple[tuple[str, str], ...] = ()          # (table, column): references into the secret store


# The modules that keep rows, in the order their upgrades run. Named, not
# imported, so this module depends on none of them.
OWNERS = ("sbx_control.state", "sbx_control.settings", "sbx_control.secret_store", "sbx_control.vault",
          "sbx_control.auth", "sbx_control.sandboxes", "sbx_control.lifecycle", "sbx_control.variables",
          "sbx_control.catalog", "sbx_control.catalog.attachments", "sbx_control.catalog.resources",
          "sbx_control.hosted_services", "sbx_control.ip_previews", "sbx_control.template_builds",
          "sbx_control.setup_pr", "sbx_control.extensions", "sbx_control.delivery", "sbx_control.runtimes",
          "sbx_control.endpoints")
EVENT_DAYS = 30

SCHEMA = Schema(tables="""
-- Small facts, each owned by one module through a Setting.
create table if not exists settings (key text primary key, value text not null);
create table if not exists events (
    id integer primary key, at text not null default current_timestamp,
    sandbox text not null, kind text not null, detail text not null default '',
    internal integer not null default 0);
create index if not exists events_by_sandbox on events (sandbox, id);
""")


@contextlib.contextmanager
def db():
    """A connection per use, in autocommit; the API, the gate and the keeper
    run on many threads."""
    conn = sqlite3.connect(paths.database(), timeout=10, isolation_level=None)
    try:
        conn.execute("pragma busy_timeout = 10000")
        yield conn
    finally:
        conn.close()


def schemas() -> list[Schema]:
    return [importlib.import_module(name).SCHEMA for name in OWNERS]


def sealed_columns() -> list[tuple[str, str]]:
    """Every (table, column) whose values are references into the secret store."""
    return [pair for schema in schemas() for pair in schema.sealed]


def columns(conn, table) -> list:
    return [row[1] for row in conn.execute(f"pragma table_info({table})")]


def tables(conn) -> set:
    return {row[0] for row in conn.execute("select name from sqlite_master where type = 'table'")}


def _add_columns(conn, found, table=None):
    for schema in found:
        for name, column, definition in schema.columns:
            if (table is None or name == table) and name in tables(conn) and column not in columns(conn, name):
                conn.execute(f"alter table {name} add column {column} {definition}")


def drop_column(conn, table, column):
    """SQLite drops a column in place unless a constraint names it (unique):
    then the table is made again from its schema and its rows copied over."""
    if table not in tables(conn) or column not in columns(conn, table):
        return
    try:
        conn.execute(f"alter table {table} drop column {column}")
        return
    except sqlite3.OperationalError:
        pass
    found = schemas()
    statement = next(match.group(0) for schema in found
                     for match in [re.search(rf"create table if not exists {table} \(.*?\);", schema.tables, re.S)]
                     if match)
    kept = [c for c in columns(conn, table) if c != column]
    conn.execute(f"alter table {table} rename to {table}_retired")
    conn.execute(statement)
    _add_columns(conn, found, table)
    common = [c for c in kept if c in columns(conn, table)]
    conn.execute(f"insert into {table} ({', '.join(common)}) select {', '.join(common)} from {table}_retired")
    conn.execute(f"drop table {table}_retired")


def private():
    """sbx.db and its write-ahead files readable by root only: they hold the
    settings, sealed values and session key."""
    for suffix in ("", "-wal", "-shm"):
        path = paths.database().with_name(paths.database().name + suffix)
        if path.exists() and path.stat().st_mode & 0o077:
            path.chmod(0o600)


def migrate():
    """Safe on an existing database and on every start."""
    paths.database().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths.database().touch(mode=0o600)   # before sqlite makes it under the umask
    private()
    found = schemas()
    with db() as conn:
        conn.execute("pragma journal_mode = wal")
        for schema in found:
            conn.executescript(schema.tables)
        _add_columns(conn, found)
        for schema in found:
            if schema.upgrade:
                schema.upgrade(conn)
        for schema in found:
            for table in schema.dropped_tables:
                conn.execute(f"drop table if exists {table}")
            for table, column in schema.dropped_columns:
                drop_column(conn, table, column)
    private()   # the write-ahead files the migration made


class Setting:
    """One row of the shared key/value table. Each owner names its own keys
    (a prefix for a family of them) and reads them only through this."""

    def __init__(self, key: str, *, encoded: bool = False):
        self.key = key
        self.encoded = encoded       # values are JSON

    def _out(self, value):
        return json.loads(value) if self.encoded and value is not None else value

    def _in(self, value) -> str:
        return json.dumps(value) if self.encoded else str(value)

    def get(self, default=None, conn=None):
        with (contextlib.nullcontext(conn) if conn else db()) as c:
            row = c.execute("select value from settings where key = ?", (self.key,)).fetchone()
        return self._out(row[0]) if row else default

    def set(self, value, conn=None):
        with (contextlib.nullcontext(conn) if conn else db()) as c:
            c.execute("insert into settings (key, value) values (?, ?) on conflict (key) do update set "
                      "value = excluded.value", (self.key, self._in(value)))

    def setdefault(self, value, conn=None):
        """Store value unless one is there; the stored one either way."""
        with (contextlib.nullcontext(conn) if conn else db()) as c:
            c.execute("insert into settings (key, value) values (?, ?) on conflict do nothing", (self.key, self._in(value)))
            return self.get(conn=c)

    def delete(self, conn=None) -> bool:
        with (contextlib.nullcontext(conn) if conn else db()) as c:
            return c.execute("delete from settings where key = ?", (self.key,)).rowcount > 0


SESSION_KEY = Setting("session_key")


def session_key() -> str:
    """The key that signs session cookies and the sandboxes' git credentials."""
    return SESSION_KEY.setdefault(secrets.token_hex(32))


INSTALLATION_ID = Setting("installation_id")


def installation_id() -> str:
    """This installation's id: random, made once, kept in sbx.db (so backups
    and upgrades keep it). Not a secret: it tells this installation's
    sandboxes and templates from another's on a provider account they share."""
    return INSTALLATION_ID.setdefault(secrets.token_hex(8))


def event(sandbox: str, kind: str, detail: str = "", *, internal: bool = False):
    """Record what happened to a sandbox. internal: the server's own
    housekeeping (a retried cleanup, self-repair); nobody acts on it, and
    the web app hides it."""
    print(f"sbx-{sandbox}: {kind}{f': {detail}' if detail else ''}", flush=True)
    try:
        with db() as conn:
            conn.execute("insert into events (sandbox, kind, detail, internal) values (?, ?, ?, ?)",
                         (sandbox, kind, detail[:2000], int(internal)))
            conn.execute("delete from events where at < datetime('now', ?)", (f"-{EVENT_DAYS} days",))
    except sqlite3.Error as error:  # a lost event must not fail what it records
        print(f"event for sbx-{sandbox}: {error}", file=sys.stderr, flush=True)


def events(sandbox: str, limit: int = 60) -> list:
    """(at, kind, detail, internal) of the sandbox's latest events."""
    with db() as conn:
        return [(at, kind, detail, bool(internal)) for at, kind, detail, internal in conn.execute(
            "select at, kind, detail, internal from events where sandbox = ? order by id desc limit ?",
            (sandbox, limit))]
