"""Env deliveries waiting for a sandbox: which values changed since its
runtime was last written, and whether its app must restart for them. Kept
until applied, also across a pause, a failure and a restart of sbx-control.

A repo's change waits here too while the runtime does not answer: which of
its sandboxes it concerns is known once it does. sandboxes.deliver_env
queues and applies; core applies what waits when a paused sandbox resumes."""
import json

from . import state
from .state import db


def queue(sandbox: str, names, restart: bool):
    """Merge a delivery into what waits for the sandbox; a new revision."""
    with db() as conn:
        conn.execute("begin immediate")
        row = conn.execute("select names, restart from env_pending where sandbox = ?", (sandbox,)).fetchone()
        merged = sorted(set(json.loads(row[0]) if row else []) | set(names))
        conn.execute("insert into env_pending (sandbox, names, restart) values (?, ?, ?) on conflict (sandbox) "
                     "do update set names = excluded.names, restart = excluded.restart, at = current_timestamp, "
                     "revision = env_pending.revision + 1",
                     (sandbox, json.dumps(merged), int(restart or bool(row and row[1]))))
        conn.commit()


def queue_repo(repo: str, names, audiences, person: str | None, restart: bool):
    """Keep a repo's change until its sandboxes can be listed."""
    with db() as conn:
        conn.execute("begin immediate")
        row = conn.execute("select names, audiences, restart from env_pending_repos where repo = ? and person = ?",
                           (repo, person or "")).fetchone()
        merged = sorted(set(json.loads(row[0]) if row else []) | set(names))
        if audiences is None or (row and row[1] is None):
            kinds = None
        else:
            kinds = sorted(set(audiences) | set(json.loads(row[1]) if row else []))
        conn.execute("insert into env_pending_repos (repo, person, names, audiences, restart) values (?, ?, ?, ?, ?) "
                     "on conflict (repo, person) do update set names = excluded.names, audiences = excluded.audiences, "
                     "restart = excluded.restart",
                     (repo, person or "", json.dumps(merged), None if kinds is None else json.dumps(kinds),
                      int(restart or bool(row and row[2]))))
        conn.commit()


def take_repos() -> list:
    """Each kept repo change as (repo, names, audiences, person, restart), removed."""
    with db() as conn:
        conn.execute("begin immediate")
        rows = conn.execute("select repo, names, audiences, person, restart from env_pending_repos").fetchall()
        conn.execute("delete from env_pending_repos")
        conn.commit()
    return [(repo, json.loads(names), None if kinds is None else json.loads(kinds), person or None, bool(restart))
            for repo, names, kinds, person, restart in rows]


def pending(sandbox: str):
    """(names, restart, revision) waiting for the sandbox, or None. It stays
    until acknowledge() names that revision."""
    with db() as conn:
        row = conn.execute("select names, restart, revision from env_pending where sandbox = ?", (sandbox,)).fetchone()
    return (json.loads(row[0]), bool(row[1]), row[2]) if row else None


def acknowledge(sandbox: str, revision: int):
    with db() as conn:
        conn.execute("delete from env_pending where sandbox = ? and revision = ?", (sandbox, revision))


def restart_required(sandbox: str, *, mark: bool = False) -> bool:
    """Whether a delivery wrote values the app has not restarted with yet."""
    with db() as conn:
        if mark:
            conn.execute("update env_pending set restart_required = 1 where sandbox = ?", (sandbox,))
        row = conn.execute("select restart_required from env_pending where sandbox = ?", (sandbox,)).fetchone()
    return bool(row and row[0])


def forget(sandbox: str):
    with db() as conn:
        conn.execute("delete from env_pending where sandbox = ?", (sandbox,))


def waiting() -> set:
    """The sandboxes something waits for."""
    with db() as conn:
        return {row[0] for row in conn.execute("select sandbox from env_pending")}


SCHEMA = state.Schema(tables="""
create table if not exists env_pending (
    sandbox text primary key, names text not null, restart integer not null,
    at text not null default current_timestamp);
create table if not exists env_pending_repos (
    repo text not null, person text not null default '', names text not null, audiences text,
    restart integer not null, primary key (repo, person));
""", columns=(("env_pending", "revision", "integer not null default 1"),
              ("env_pending", "restart_required", "integer not null default 0")))
