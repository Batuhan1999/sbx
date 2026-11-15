"""Embed's storage on this host, as the storage GC (embed/storage-gc.py),
snapshot compaction (embed/compact-snapshot.py) and sbx-control read it:
the build directories, the header format that maps each block of a build's
memory and disk to the build holding it, Embed's Postgres and Redis, and
the per-sandbox snapshot usage the GC writes. Standard library only (and
host.py's layout of Embed's project).

These follow the vendored Embed release (vendor/e2b-embed): its storage
layout, header version 3, its tables and Redis keys."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pathlib
import shutil
import struct
import subprocess
import uuid

from .host import EMBED

ROOT = pathlib.Path("/var/lib/e2b")
STORAGE = ROOT / "storage/templates"           # one directory per build
INDEX = ROOT / "storage/build-cache"
LOCAL_COPIES = pathlib.Path("/orchestrator/build")
USAGE = ROOT / "storage-usage.json"            # sandbox id -> {gb, snapshots}, from the GC
LOCK = ROOT / ".storage.lock"                  # held by whoever writes a build outside Embed (compaction)
COMPOSE = EMBED / "compose.yaml"
FILES = ("memfile", "rootfs.ext4")
HEADER_VERSION = 3
META = struct.Struct("<4Q16s16s")  # version, block size, size, generation, build, base build
MAPPING = struct.Struct("<2Q16sQ")  # offset, length, build, offset in that build's file
NIL = uuid.UUID(int=0)


class UnknownHeader(ValueError):
    pass


def disk() -> dict | None:
    """Where sandboxes live (ROOT, else / before Embed made it): used percent
    and free GB; None when it can't be read."""
    try:
        usage = shutil.disk_usage(ROOT if ROOT.exists() else "/")
    except OSError:
        return None
    return {"percent": round(100 * usage.used / usage.total), "free_gb": usage.free // 2**30}


@contextlib.contextmanager
def lock(wait: bool = True):
    """Yields whether this process holds the storage lock: compaction holds it
    from putting its build in place until the database names it, and the GC
    deletes nothing while another process has it (wait=False)."""
    with open(LOCK, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def free_mb() -> int | None:
    """Free MiB where Embed keeps templates and snapshots; None on a host without them."""
    try:
        return shutil.disk_usage(ROOT).free // 2**20
    except OSError:
        return None


def read_header(data: bytes):
    """(meta, mappings) of a header's bytes."""
    meta = META.unpack_from(data)
    count, rest = divmod(len(data) - META.size, MAPPING.size)
    if rest or meta[0] != HEADER_VERSION:
        raise UnknownHeader(f"header version {meta[0]}")
    return meta, [MAPPING.unpack_from(data, META.size + i * MAPPING.size) for i in range(count)]


def compose(*args: str) -> str:
    return subprocess.run(["docker", "compose", "-f", str(COMPOSE), "exec", "-T", *args],
                          check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout


def query(sql: str) -> list[list[str]]:
    """Rows of Embed's Postgres, tab-separated fields as strings."""
    out = compose("postgres", "psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1",
                  "-AtF", "\t", "-c", sql)
    return [line.split("\t") for line in out.splitlines() if line]


def running(sandbox: str = "*") -> list[str]:
    """The Redis keys of running sandboxes (Embed's api keeps one per sandbox while it runs)."""
    return compose("redis", "redis-cli", "--scan", "--pattern", f"sandbox:storage:*:sandboxes:{sandbox}").split()


def size_mb(path: pathlib.Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) // 2**20


def write_usage(usage: dict):
    temporary = USAGE.with_suffix(".tmp")
    temporary.write_text(json.dumps(usage))
    os.replace(temporary, USAGE)


def usage() -> dict:
    """sandbox id -> {gb, snapshots}, as the last GC run measured; {} before one."""
    try:
        return json.loads(USAGE.read_text())
    except (OSError, ValueError):
        return {}
