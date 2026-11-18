"""Where every secret value lives. SQLite keeps metadata and references;
the values are in the installation's SecretStore, chosen from the store
settings:

  local      sealed in sbx.db with the recovery key (paths.secret_key())
  infisical  an Infisical project (the installer runs one on this host)

An owner keeps a sealed payload's reference in its own column, declared in
state.Schema.sealed. A reference names the store that holds it.

The store's own credentials (Infisical's machine identity and webhook key)
are always local: BOOTSTRAP."""
from __future__ import annotations

import contextlib
import json

from .. import settings, state
from .base import BOOTSTRAP, OBJECTS, SecretStore, StoreError, Watch, parse_scope, scope_of
from .infisical import Infisical
from .local import Local

__all__ = ["BOOTSTRAP", "OBJECTS", "SecretStore", "StoreError", "Watch", "Infisical", "Local",
           "parse_scope", "scope_of"]

TYPES = (Local, Infisical)
LOCAL = Local()
_infisical: dict = {}


def local() -> Local:
    return LOCAL


def infisical():
    """The Infisical store of the store settings (whatever the mode: a
    reference to it may outlive a change of mode)."""
    s = settings.current().store
    if not s.project:
        raise StoreError("This installation references Infisical, which is not configured; reconnect it")
    identity = (s.port, s.project, s.environment)
    if identity not in _infisical:
        _infisical.clear()
        _infisical[identity] = Infisical(s.url, s.project, s.environment,
                                         lambda: (LOCAL.get(BOOTSTRAP, "INFISICAL_CLIENT_ID"),
                                                  LOCAL.get(BOOTSTRAP, "INFISICAL_CLIENT_SECRET")))
    return _infisical[identity]


def store() -> SecretStore:
    """The store new values go to."""
    return infisical() if settings.current().store.mode == "infisical" else LOCAL


def is_reference(value) -> bool:
    """A reference into a store other than the local one (Local's tokens
    are their own payload)."""
    return Infisical.owns(value)


def _holder(reference: str) -> SecretStore:
    return infisical() if Infisical.owns(reference) else LOCAL


def seal(value, *, key: str) -> str:
    """A JSON value as a reference for the owner's column. key names the
    payload in the store (stable for one owner's row: a retry overwrites)."""
    return store().seal(json.dumps(value, sort_keys=True), key)


def unseal(reference: str):
    return json.loads(_holder(reference).unseal(reference))


def discard(reference: str | None):
    if reference:
        _holder(reference).discard(reference)


def delete_rows(conn, table: str, where: str, params=()) -> int:
    """Delete an owner's rows, discarding the payloads of its sealed columns
    first; a payload that cannot be discarded keeps its row for a retry."""
    sealed = [column for name, column in state.sealed_columns() if name == table]
    if sealed:
        for row in conn.execute(f"select {', '.join(sealed)} from {table} where {where}", params).fetchall():
            for reference in row:
                discard(reference)
    return conn.execute(f"delete from {table} where {where}", params).rowcount


_AFTER_ROLLBACKS = state.Setting("store_discard_after_rollbacks", encoded=True)


def discard_after_rollbacks(conn, reference: str | None) -> None:
    """For an upgrade's own changes to sbx.db: the payload stays while an
    upgrade backup's copy of sbx.db names it, because rollback.sh restores that
    copy and the release before reads it. discard_released() discards it later."""
    if is_reference(reference):
        _AFTER_ROLLBACKS.set(sorted({*_AFTER_ROLLBACKS.get([], conn=conn), reference}), conn=conn)


def _backed_up_references() -> set | None:
    """Every store reference the upgrade backups' copies of sbx.db hold; None
    when a copy could not be read (nothing may be discarded then)."""
    import sqlite3
    from .. import paths
    found = set()
    for copy in (paths.state_dir() / "upgrade-backups").glob("*/sbx.db"):
        try:
            with contextlib.closing(sqlite3.connect(f"file:{copy}?mode=ro&immutable=1", uri=True)) as conn:
                present = state.tables(conn)
                for table, column in state.sealed_columns():
                    if table in present:
                        found |= {r[0] for r in conn.execute(f"select {column} from {table} where {column} is not null")}
        except sqlite3.Error:
            return None
    return found


def discard_released() -> None:
    """Discards what discard_after_rollbacks() kept once no upgrade backup names it."""
    queued = _AFTER_ROLLBACKS.get([])
    named = _backed_up_references() if queued else None
    if named is None:
        return
    kept = []
    for reference in queued:
        if reference in named:
            kept.append(reference)
            continue
        try:
            discard(reference)
        except StoreError as error:   # the store did not answer: tried again on the next start
            print(f"secret store: a payload kept for rollbacks was not discarded: {error}", flush=True)
            kept.append(reference)
    _AFTER_ROLLBACKS.set(kept)


def references() -> list[str]:
    """Every reference sbx.db holds."""
    found = []
    with state.db() as conn:
        present = state.tables(conn)
        for table, column in state.sealed_columns():
            if table in present:
                found += [r[0] for r in conn.execute(f"select {column} from {table} where {column} is not null "
                                                     f"and {column} != ''")]
    return found


def moves(before, after) -> bool:
    """Whether a store settings change points away from the Infisical in use.
    Whoever keeps values there refuses it (a settings.check on "store")."""
    where = ("mode", "port", "project", "environment")
    return before.mode == "infisical" and any(getattr(before, f) != getattr(after, f) for f in where)


@settings.check("store")
def check_location(before, after):
    held = references() + _AFTER_ROLLBACKS.get([])   # what a rollback would bring back too
    if moves(before, after) and any(Infisical.owns(r) for r in held):
        raise ValueError("This Infisical store holds sealed values; restore or move them before changing it")


def migrate(target: SecretStore | None = None) -> int:
    """Move every value the local store holds into the chosen store
    (sbx-admin store enable)."""
    target = target or store()
    if target is LOCAL:
        return 0
    moved = 0
    for namespace, key, value in LOCAL.entries():
        if namespace == BOOTSTRAP:
            continue
        found = target.get(namespace, key)
        if found is not None and found != value:
            raise StoreError(f"{namespace}: a different value of {key} is in {target.name} already; both were kept")
        target.put(namespace, key, value)
        if target.get(namespace, key) != value:
            raise StoreError(f"{target.name} did not keep {key}; its local value was kept")
        LOCAL.delete(namespace, key)
        moved += 1
    with state.db() as conn:
        present = state.tables(conn)
        work = [(table, column, conn.execute(f"select rowid, {column} from {table} where {column} is not null "
                                             f"and {column} != ''").fetchall())
                for table, column in state.sealed_columns() if table in present]
    for table, column, rows in work:
        for rowid, reference in rows:
            if not LOCAL.owns(reference):
                continue
            text = LOCAL.unseal(reference)
            new = target.seal(text, f"{table}.{column}/{rowid}")
            if target.unseal(new) != text:
                raise StoreError(f"{target.name} did not keep a payload of {table}; its local copy was kept")
            with state.db() as conn:
                moved += conn.execute(f"update {table} set {column} = ? where rowid = ? and {column} = ?",
                                      (new, rowid, reference)).rowcount
    return moved


SCHEMA = state.Schema(tables="""
-- The local store's values (Local): sealed with the recovery key.
create table if not exists store_values (
    namespace text not null, key text not null, value text not null,
    updated_at text not null default current_timestamp, primary key (namespace, key));
""")
