#!/usr/bin/env python3
"""Delete template layers no build needs any more, on the Embed host.

Embed keeps every template build, every step layer of every build and every
pause snapshot on this host's disk, and nothing deletes them: every build of a
large app adds its whole size to /var/lib/e2b/storage/templates and the same
again to the orchestrator's copy in /orchestrator/build. This keeps the newest build of
every template (pause snapshots are templates too), the build every live
sandbox runs from, builds still running, and every layer those reference
through their headers, and deletes the rest with their build-cache index
entries. Run hourly (e2b-storage-gc.timer): rebuilding a large app on every push fills a disk in days.

Prints what it would delete; --delete deletes it.
"""
import json
import pathlib
import shutil
import sys
import time
import uuid

# The storage library is in sbx_control, beside embed/ in a release and under
# control/ in the repository.
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "control")]
from sbx_control import embed_storage as storage  # noqa: E402
from sbx_control.embed_storage import STORAGE, query  # noqa: E402

# Leaves running builds alone: their layers are not in any header yet.
MIN_AGE_HOURS = 2


def live() -> list[list[str]]:
    """The builds live sandboxes run from: the api keeps each sandbox in Redis
    while it runs. A sandbox started from a build since replaced reads that
    build's layers until it ends."""
    return [[json.loads(storage.compose("redis", "redis-cli", "get", key) or "{}").get("buildID", "")]
            for key in storage.running()]


def retained_reference_roots() -> list[list[str]]:
    """The builds templates and snapshots point at now, each with whether it
    is recent ("t"/"f"), from Embed's database (reference_roots)."""
    finished = dict(query("select id, created_at > now() - interval '%d hours' from env_builds "
                          "where status in ('uploaded', 'success')" % MIN_AGE_HOURS))
    assignments = query("select a.env_id, a.build_id, a.tag from env_build_assignments a "
                        "join envs e on e.id = a.env_id where e.deleted_at is null "
                        "order by a.created_at desc, a.build_id desc")
    snapshots = query("select s.build_id from snapshot_templates s join envs e on e.id = s.env_id "
                      "where e.deleted_at is null")
    return reference_roots(assignments, [row[0] for row in snapshots], finished)


def reference_roots(assignments: list[list[str]], snapshots: list[str], finished: dict[str, str]) -> list[list[str]]:
    """assignments: (env, build, tag) of live environments, newest first;
    snapshots: the builds live snapshot templates point at; finished: each
    finished build and whether it is recent. Assignment history is
    append-only, so only each tag's newest finished build counts. A snapshot
    build can keep its source's env_id after the source environment is
    deleted; its snapshot reference is the root."""
    current: dict[tuple[str, str], str] = {}
    for env, build, tag in assignments:
        if build in finished:
            current.setdefault((env, tag), build)
    keep = set(current.values()) | set(snapshots)
    return [[build, finished[build]] for build in sorted(keep) if build in finished]


def roots() -> tuple[set[str], set[str]]:
    """The builds to keep with all they reference, and the recent ones among
    them: a pause snapshot is "success" a minute or two before its headers
    are on disk, and until then nothing shows it needs its parents."""
    # Template builds end "uploaded", pause snapshots "success". A deleted
    # template or sandbox only gets envs.deleted_at.
    newest = query("select distinct on (b.env_id) b.id, b.created_at > now() - interval '%d hours' "
                   "from env_builds b join envs e on e.id = b.env_id where e.deleted_at is null "
                   "and b.status in ('uploaded', 'success') order by b.env_id, b.created_at desc" % MIN_AGE_HOURS)
    building = query("select id from env_builds where status not in ('uploaded', 'success', 'failed')")
    referenced = retained_reference_roots()
    recent = {row[0] for row in newest + referenced if row[1] == "t"}
    return {row[0] for row in newest + referenced + building + live()}, recent


def referenced(build: str, layers: dict[bytes, str]) -> tuple[set[str], set[str]]:
    """(the builds its blocks are read from, its base builds). A base build
    is kept but not what it reads from: after a disk boot or a compaction
    the base still maps to the chain before it, which no block reads any
    more."""
    mapped, bases = set(), set()
    for header in (STORAGE / build).glob("*.header"):
        data = header.read_bytes()
        try:
            meta, mappings = storage.read_header(data)
        except storage.UnknownHeader:  # keep all it names
            mapped |= {name for raw, name in layers.items() if raw in data}
            continue
        bases.add(meta[5])
        mapped |= {mapping[2] for mapping in mappings}
    return {layers[raw] for raw in mapped if raw in layers}, {layers[raw] for raw in bases if raw in layers}


def write_usage():
    """Per sandbox id, the size and count of its pause snapshots, for sbx-control's sandbox page."""
    usage = {}
    for sandbox, build in query("select s.sandbox_id, b.id from snapshots s join env_builds b on b.env_id = s.env_id "
                                "join envs e on e.id = s.env_id where e.deleted_at is null"):
        if (STORAGE / build).is_dir():
            entry = usage.setdefault(sandbox, {"gb": 0.0, "snapshots": 0})
            entry["gb"] = round(entry["gb"] + storage.size_mb(STORAGE / build) / 1024, 1)
            entry["snapshots"] += 1
    storage.write_usage(usage)


def main():
    delete = "--delete" in sys.argv[1:]
    builds = {p.name for p in STORAGE.iterdir() if p.is_dir()}
    layers = {uuid.UUID(name).bytes: name for name in builds}
    kept, recent = roots()
    # metadata.json is written with the headers (a snapshot of a sandbox
    # booted from its disk has no memfile.header).
    unwritten = sorted(b for b in recent if not (STORAGE / b / "metadata.json").exists())
    if unwritten:
        print(f"builds {', '.join(unwritten)} are still being written; deleting nothing this time")
        return
    keep, bases, todo = set(), set(), [b for b in kept if b in builds]
    while todo:
        build = todo.pop()
        if build not in keep:
            keep.add(build)
            mapped, base = referenced(build, layers)
            todo += mapped - keep
            bases |= base
    keep |= bases
    cutoff = time.time() - MIN_AGE_HOURS * 3600
    doomed = sorted(b for b in builds - keep if (STORAGE / b).stat().st_mtime < cutoff)

    stale_index = []
    for entry in storage.INDEX.glob("*/index/*"):
        try:
            target = json.loads(entry.read_text())["template"]["build_id"]
        except (ValueError, KeyError):
            continue
        if target in doomed or target not in builds:
            stale_index.append(entry)
    copies = [c for c in storage.LOCAL_COPIES.iterdir() if c.name[:36] in doomed]

    freed = sum(storage.size_mb(STORAGE / b) for b in doomed) + sum(storage.size_mb(c) if c.is_dir() else c.stat().st_size // 2**20 for c in copies)
    print(f"builds {len(builds)}, keep {len(keep)}, delete {len(doomed)} "
          f"({len(copies)} local copies, {len(stale_index)} cache entries), frees {freed // 1024} GB")
    if not delete:
        return
    for entry in stale_index:
        entry.unlink()
    for build in doomed:
        shutil.rmtree(STORAGE / build)
    for copy in copies:
        shutil.rmtree(copy) if copy.is_dir() else copy.unlink()
    print(f"deleted {', '.join(doomed) or 'nothing'}")
    write_usage()


if __name__ == "__main__":
    main()
