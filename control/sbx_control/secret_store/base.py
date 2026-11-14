"""What every secret store is (SecretStore, Watch), and the names they share."""
from __future__ import annotations

from typing import Protocol

OBJECTS = "objects"      # the namespace sealed payloads live in
BOOTSTRAP = "bootstrap"  # what reaching the store needs, always local


# People's variables (variables.py) live in a namespace per scope: the
# installation (""), a repo, or one person's own in a repo. A store lays them
# out by these parts (Infisical: a folder per repo).

def scope_of(repo: str | None, person: str | None = None) -> str:
    if not repo:
        return ""
    return f"repo:{repo.lower()}" + (f"|user:{person.lower()}" if person else "")


def parse_scope(namespace: str) -> tuple[str, str] | None:
    """(repo, person or "") of a scope_of() namespace; None for any other."""
    if not namespace.startswith("repo:"):
        return None
    repo, _, person = namespace.removeprefix("repo:").partition("|user:")
    return repo, person


class StoreError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Watch(Protocol):
    """A store edited outside sbx (Infisical's UI): what changed since the
    last acknowledged look at a namespace."""

    def changes(self, namespace: str) -> tuple[list[str], object]: ...

    def acknowledge(self, namespace: str, snapshot) -> None: ...


class SecretStore(Protocol):
    name: str
    watch: Watch | None

    def get(self, namespace: str, key: str) -> str | None: ...

    def put(self, namespace: str, key: str, value: str) -> None: ...

    def delete(self, namespace: str, key: str) -> bool: ...

    def list(self, namespace: str) -> dict[str, str]: ...

    def seal(self, text: str, key: str) -> str: ...

    def unseal(self, reference: str) -> str: ...

    def discard(self, reference: str) -> None: ...

    def owns(self, reference: str) -> bool: ...

    # Optional, static, for a store that may run on this host (Infisical):
    # what a backup bundle carries of it, name -> (bytes, sealed), and taking
    # it back on a fresh host: restore_parts(parts) while sbx-control is
    # stopped (the database may be older than this release), finish_restore()
    # once it is migrated. backup.py calls them on every secret_store.TYPES.
