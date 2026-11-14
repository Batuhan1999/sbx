"""The local SecretStore: values sealed with Fernet under the recovery key
(paths.secret_key()) in sbx.db's store_values; a sealed payload is the
token itself, kept in the owner's column."""
from __future__ import annotations

import base64
import binascii
import hashlib

from .. import paths, state

FERNET_VERSION = 0x80   # a Fernet token's first byte


class Local:
    name = "local"
    watch = None

    def fernet(self):
        from cryptography.fernet import Fernet
        key = paths.secret_key()
        if not key.exists():
            key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            key.write_bytes(Fernet.generate_key())
            key.chmod(0o600)
        return Fernet(key.read_bytes())

    def fingerprint(self) -> str:
        return hashlib.sha256(paths.secret_key().read_bytes()).hexdigest()[:12]

    def encrypt(self, text: str) -> str:
        return self.fernet().encrypt(text.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self.fernet().decrypt(token.encode()).decode()

    def get(self, namespace: str, key: str) -> str | None:
        with state.db() as conn:
            row = conn.execute("select value from store_values where namespace = ? and key = ?",
                               (namespace, key)).fetchone()
        return self.decrypt(row[0]) if row else None

    def put(self, namespace: str, key: str, value: str):
        with state.db() as conn:
            conn.execute("insert into store_values (namespace, key, value) values (?, ?, ?) on conflict "
                         "(namespace, key) do update set value = excluded.value, updated_at = current_timestamp",
                         (namespace, key, self.encrypt(value)))

    def delete(self, namespace: str, key: str) -> bool:
        with state.db() as conn:
            return conn.execute("delete from store_values where namespace = ? and key = ?",
                                (namespace, key)).rowcount > 0

    def list(self, namespace: str) -> dict[str, str]:
        with state.db() as conn:
            rows = conn.execute("select key, value from store_values where namespace = ? order by key",
                                (namespace,)).fetchall()
        return {key: self.decrypt(value) for key, value in rows}

    def entries(self) -> list[tuple[str, str, str]]:
        """(namespace, key, value) of everything it holds."""
        with state.db() as conn:
            rows = conn.execute("select namespace, key, value from store_values order by namespace, key").fetchall()
        return [(namespace, key, self.decrypt(value)) for namespace, key, value in rows]

    def seal(self, text: str, key: str) -> str:
        return self.encrypt(text)

    def unseal(self, reference: str) -> str:
        return self.decrypt(reference)

    def discard(self, reference: str):
        """Nothing to do: the payload is the reference."""

    @staticmethod
    def owns(reference) -> bool:
        """A Fernet token: what seal() returns."""
        if not isinstance(reference, str) or len(reference) < 8:
            return False
        try:
            return base64.urlsafe_b64decode(reference[:8])[0] == FERNET_VERSION
        except (binascii.Error, ValueError):
            return False
