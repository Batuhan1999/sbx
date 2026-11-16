"""Back up and restore the installation: sbx.db (settings, metadata,
references and the local store's sealed values), the runtime's credentials
(sealed), what the secret stores this host runs keep (SecretStore.
backup_parts: the local Infisical's database and keys) and the operator's
files. Keep the bundle private and its recovery key apart. VMs, templates
and application databases are not included.

  python -m sbx_control.backup scheduled
      a bundle into the backup settings' directory, keeping the newest
      `keep` scheduled ones (sbx-backup.timer, every_hours)"""
from __future__ import annotations

import io
import hashlib
from contextlib import closing
import json
import pathlib
import sqlite3
import shutil
import tarfile
import sys
import tempfile
import time

from . import extensions, lifecycle, paths, secret_store, settings, vault

ETC_DIRS = ("apps", "catalog")
SEALED = ".sealed"
SCHEDULED = "scheduled-sbx-"


def bundle(out: pathlib.Path) -> dict:
    s = settings.current()
    local = secret_store.local()
    f = local.fernet()
    manifest = {"format": 1, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "domain": s.domain.name,
                "key_fingerprint": local.fingerprint(), "parts": ["sbx.db"]}
    with tempfile.TemporaryDirectory(prefix="sbx-backup-", dir=out.parent) as directory:
        archive = pathlib.Path(directory) / "bundle.tgz"
        archive.touch(mode=0o600)
        _bundle(archive, pathlib.Path(directory) / "sbx.db", manifest, f)
        archive.replace(out)
    return manifest


def _bundle(out, tmp, manifest, f):
    with tarfile.open(out, "w:gz") as tar:
        def part(name: str, data: bytes, sealed: bool):
            name, data = (name + SEALED, f.encrypt(data)) if sealed else (name, data)
            _add(tar, name, data)
            manifest["parts"].append(name)
        with closing(sqlite3.connect(paths.database())) as source, closing(sqlite3.connect(tmp)) as dest:
            source.backup(dest)
        _add(tar, "sbx.db", tmp.read_bytes())
        tmp.unlink()
        if paths.runtime_env().exists():
            part("runtime.env", paths.runtime_env().read_bytes(), True)
        for kind in secret_store.TYPES:
            for name, (data, sealed) in getattr(kind, "backup_parts", dict)().items():
                part(name, data, sealed)
        for name in ETC_DIRS:
            folder = paths.etc() / name
            if folder.is_dir():
                tar.add(folder, arcname=f"etc/{name}", filter=_regular_file)
                manifest["parts"].append(f"etc/{name}")
        _add(tar, "manifest.json", json.dumps(manifest, indent=2).encode())


def _add(tar: tarfile.TarFile, name: str, data: bytes):
    info = tarfile.TarInfo(name)
    info.size, info.mtime, info.mode = len(data), int(time.time()), 0o600
    tar.addfile(info, io.BytesIO(data))


def _regular_file(member: tarfile.TarInfo):
    if not (member.isfile() or member.isdir()):
        raise ValueError("backup configuration must contain regular files, not links")
    return member


def restore(path: pathlib.Path, key: str) -> dict:
    """On a freshly installed host. The caller (sbx-admin) stops sbx-control
    before and starts it after, then requests the certificate. Sandboxes
    don't travel: their rows are marked gone. Idempotent."""
    from cryptography.fernet import Fernet
    f = Fernet(key.encode())
    with tarfile.open(path, "r:gz") as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())
        if hashlib.sha256(key.encode()).hexdigest()[:12] != manifest["key_fingerprint"]:
            raise SystemExit("that is not the key this bundle was made under")
        if manifest.get("format") != 1:
            raise ValueError("unsupported backup format")
        for member in tar.getmembers():
            name = pathlib.PurePosixPath(member.name)
            if not name.parts or name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
                raise ValueError("backup contains an unsupported path or link")
            if name.parts[0] == "etc" and (len(name.parts) < 2 or name.parts[1] not in ETC_DIRS):
                raise ValueError("backup contains an unknown configuration directory")
        # Stage and decrypt before replacing the installed key or database.
        with tempfile.TemporaryDirectory(prefix="sbx-restore-") as directory:
            stage = pathlib.Path(directory)
            tar.extractall(stage, filter="data")
            with closing(sqlite3.connect(stage / "sbx.db")) as conn, conn:
                if conn.execute("pragma quick_check").fetchone()[0] != "ok":
                    raise ValueError("backup database is corrupt")
                lifecycle.abandon_all(conn, "the installation moved to another host")
                extensions.on_restore(conn)
            database = (stage / "sbx.db").read_bytes()
            parts = {name.removesuffix(SEALED): f.decrypt((stage / name).read_bytes()) if name.endswith(SEALED)
                     else (stage / name).read_bytes()
                     for name in manifest["parts"] if name != "sbx.db" and not name.startswith("etc/")}
            _private(paths.secret_key(), key.encode())
            for suffix in ("", "-wal", "-shm"):
                pathlib.Path(str(paths.database()) + suffix).unlink(missing_ok=True)
            _private(paths.database(), database)
            for kind in secret_store.TYPES:
                getattr(kind, "restore_parts", lambda parts: None)(parts)
            # This host's own runtime credentials win, unless they point into the store.
            if "runtime.env" in parts and (not paths.runtime_env().exists() or vault.runtime_sealed()):
                _private(paths.runtime_env(), parts["runtime.env"])
            restored = stage / "etc"
            if restored.is_dir():
                for folder in restored.iterdir():
                    target = paths.etc() / folder.name
                    shutil.rmtree(target, ignore_errors=True)
                    shutil.copytree(folder, target)
    settings.forget()
    if not paths.runtime_env().exists():
        print("note: /etc/sbx/runtime.env is missing; install.sh writes this host's runtime credentials", flush=True)
    return manifest


def _private(path: pathlib.Path, data: bytes):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.touch(mode=0o600)
    path.chmod(0o600)
    path.write_bytes(data)


def finish_restore():
    """What the stores take back once the restored database is migrated."""
    for kind in secret_store.TYPES:
        getattr(kind, "finish_restore", lambda: None)()


def scheduled() -> pathlib.Path:
    """A bundle into the backup settings' directory; the oldest scheduled
    ones beyond `keep` go, bundles made by hand stay."""
    b = settings.current().backup
    folder = pathlib.Path(b.to) if b.to else paths.state_dir() / "backups"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    out = folder / time.strftime(f"{SCHEDULED}%Y%m%d-%H%M%S.tar.gz", time.gmtime())
    bundle(out)
    for old in sorted(folder.glob(f"{SCHEDULED}*.tar.gz"))[:-b.keep]:
        old.unlink()
    return out


if __name__ == "__main__":
    if sys.argv[1:] != ["scheduled"]:
        raise SystemExit("usage: python -m sbx_control.backup scheduled")
    print(scheduled())
