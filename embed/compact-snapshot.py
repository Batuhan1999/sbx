#!/usr/bin/env python3
"""Fold a paused sandbox's snapshot chain into one build, on the Embed host.

Each pause writes only what changed since the last one, and a header that
maps every block of memory and disk to the build holding it. After many
pauses the newest snapshot reads a few MB from each of dozens of builds,
and each of those stays on disk whole, many times the data the sandbox
actually holds. This copies every block the newest snapshot
reads into a new build whose headers map only to itself, checks the copy
byte for byte, and makes it the sandbox's newest snapshot. The storage GC
then deletes the builds nothing needs any more.

The sandbox must stay paused meanwhile: the database is only changed if
it still is and no pause came in since.

Prints what it would do; --apply does it.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import uuid

# The storage library is in sbx_control, beside embed/ in a release and under
# control/ in the repository.
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "control")]
from sbx_control import embed_storage as storage  # noqa: E402
from sbx_control.embed_storage import FILES, MAPPING, META, NIL, STORAGE, query  # noqa: E402

COPY_CHUNK = 64 * 2**20


def read_header(path: pathlib.Path):
    try:
        return storage.read_header(path.read_bytes())
    except storage.UnknownHeader as error:
        sys.exit(f"{path}: not a header this understands ({error})")


def copy_range(src, dst, offset: int, length: int):
    while length:
        n = os.copy_file_range(src, dst, min(length, COPY_CHUNK), offset)
        if not n:
            sys.exit("short read")
        offset, length = offset + n, length - n


def compact(build: str, new: str, out: pathlib.Path):
    """Writes the new build's files into out; returns the bytes copied."""
    copied = 0
    for name in FILES:
        meta, mappings = read_header(STORAGE / build / f"{name}.header")
        sources, rewritten, at = {}, [], 0
        with open(out / name, "wb") as dst:
            for offset, length, source, source_offset in mappings:
                if uuid.UUID(bytes=source) == NIL:
                    rewritten.append((offset, length, source, source_offset))
                    continue
                if source not in sources:
                    sources[source] = open(STORAGE / str(uuid.UUID(bytes=source)) / name, "rb")
                copy_range(sources[source].fileno(), dst.fileno(), source_offset, length)
                rewritten.append((offset, length, uuid.UUID(new).bytes, at))
                at += length
            dst.flush()
            os.fsync(dst.fileno())
        for f in sources.values():
            f.close()
        version, block, size, generation, _, base = meta
        header = META.pack(version, block, size, generation, uuid.UUID(new).bytes, base) \
            + b"".join(MAPPING.pack(*m) for m in rewritten)
        (out / f"{name}.header").write_bytes(header)
        copied += at
    shutil.copy2(STORAGE / build / "snapfile", out / "snapfile")
    metadata = json.loads((STORAGE / build / "metadata.json").read_text())
    metadata["template"]["build_id"] = new
    (out / "metadata.json").write_text(json.dumps(metadata))
    return copied


def verify(build: str, new: str, out: pathlib.Path):
    """Every block reads the same through the new headers as the old."""
    for name in FILES:
        _, old = read_header(STORAGE / build / f"{name}.header")
        _, rewritten = read_header(out / f"{name}.header")
        if [m[:2] for m in old] != [m[:2] for m in rewritten]:
            sys.exit(f"{name}: mappings differ")
        with open(out / name, "rb") as mine:
            sources = {}
            for (offset, length, source, source_offset), (_, _, target, at) in zip(old, rewritten):
                if uuid.UUID(bytes=source) == NIL:
                    continue
                if uuid.UUID(bytes=target) != uuid.UUID(new):
                    sys.exit(f"{name}: block at {offset} maps elsewhere")
                if source not in sources:
                    sources[source] = open(STORAGE / str(uuid.UUID(bytes=source)) / name, "rb")
                for start in range(0, length, COPY_CHUNK):
                    n = min(COPY_CHUNK, length - start)
                    if os.pread(sources[source].fileno(), n, source_offset + start) != os.pread(mine.fileno(), n, at + start):
                        sys.exit(f"{name}: block at {offset + start} differs")
            for f in sources.values():
                f.close()


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv[1:]
    if len(args) != 1:
        sys.exit("usage: e2b-compact-snapshot <sandbox id> [--apply]")
    sandbox = args[0]
    if storage.running(sandbox):
        sys.exit(f"{sandbox} is running; compact it while it is paused")
    rows = query(f"select s.env_id, a.build_id from snapshots s join env_build_assignments a on a.env_id = s.env_id "
                 f"where s.sandbox_id = '{sandbox}' and a.tag = 'default' order by a.created_at desc limit 1")
    if not rows:
        sys.exit(f"{sandbox} has no snapshot")
    env, build = rows[0]
    layers = set()
    for name in FILES:
        _, mappings = read_header(STORAGE / build / f"{name}.header")
        layers |= {uuid.UUID(bytes=m[2]) for m in mappings} - {NIL}
    size = sum((STORAGE / str(b) / n).stat().st_size for b in layers for n in FILES if (STORAGE / str(b) / n).exists())
    print(f"{sandbox}: newest snapshot {build} reads from {len(layers)} builds holding {size // 2**30} GB")
    if not apply:
        return

    new = str(uuid.uuid4())
    out = STORAGE.parent / f".compact-{new}"  # out of the storage GC's sight until done
    out.mkdir()
    try:
        copied = compact(build, new, out)
        verify(build, new, out)
    except BaseException:
        shutil.rmtree(out)
        raise
    out.rename(STORAGE / new)
    print(f"wrote {new}: {copied // 2**20} MB, checked block for block")

    columns = [r[0] for r in query("select column_name from information_schema.columns "
                                   "where table_name = 'env_builds' and column_name not in "
                                   "('id', 'created_at', 'updated_at', 'finished_at') order by ordinal_position")]
    cols = ", ".join(columns)
    if storage.running(sandbox):
        shutil.rmtree(STORAGE / new)
        sys.exit(f"{sandbox} was resumed meanwhile; nothing changed")
    try:
        query(f"""begin;
            do $$ begin
              if (select build_id from env_build_assignments where env_id = '{env}' and tag = 'default'
                  order by created_at desc limit 1) <> '{build}' then
                raise exception 'a pause came in meanwhile';
              end if;
            end $$;
            insert into env_builds (id, created_at, updated_at, finished_at, {cols})
              select '{new}', now(), now(), now(), {cols} from env_builds where id = '{build}';
            insert into env_build_assignments (env_id, build_id, tag, source) values ('{env}', '{new}', 'default', 'app');
            commit;""")
    except subprocess.CalledProcessError as error:
        shutil.rmtree(STORAGE / new)
        sys.exit(f"nothing changed: {error.stderr.strip()}")
    print(f"{sandbox} now resumes from {new}; the storage GC deletes what it no longer needs")


if __name__ == "__main__":
    main()
