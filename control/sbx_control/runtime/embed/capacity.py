"""Room for sandboxes on this host: Embed backs each VM's memory with
hugepages, which are host-global."""
import contextlib
import fcntl
import os
import pathlib

from ... import paths


def room(memory_mb: int) -> dict:
    """Room for sandboxes of memory_mb. Linux counts already-reserved pages
    in HugePages_Free until a VM faults them in, so Free alone can overstate
    room."""
    pages = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("HugePages_") or line.startswith("Hugepagesize"):
                    key, value = line.split(":")
                    pages[key] = int(value.split()[0])
    except OSError:
        return {"known": False}
    page_kb = pages.get("Hugepagesize", 2048)
    total = pages.get("HugePages_Total", 0)
    free = pages.get("HugePages_Free", 0)
    reserved = pages.get("HugePages_Rsvd", 0)
    allocatable = max(0, free - reserved)
    need = memory_mb * 1024 // page_kb
    if not total or not need:
        return {"known": False}
    physical_mb = 0
    try:
        with open("/proc/meminfo") as source:
            physical_mb = next(int(line.split()[1]) // 1024 for line in source if line.startswith("MemTotal:"))
    except (OSError, StopIteration):
        pass
    return {"known": True, "memory_mb": memory_mb, "slots_total": total // need,
            "slots_free": allocatable // need,
            "physical_memory_mb": physical_mb, "host_reserve_mb": physical_mb - total * page_kb // 1024,
            "free_mb": allocatable * page_kb // 1024,
            "hugepages_free_mb": free * page_kb // 1024,
            "reserved_mb": reserved * page_kb // 1024,
            "total_mb": total * page_kb // 1024}


def hugepages_mb() -> int | None:
    """All of the hugepages that back sandbox memory; None when there are none."""
    return room(1024).get("total_mb") or None


@contextlib.contextmanager
def placing():
    """Held until the runtime has reserved a new sandbox's hugepages, so two
    creations can't both take the last slot."""
    lock_path = pathlib.Path(os.environ.get("SBX_CAPACITY_LOCK") or paths.state_dir() / "sandbox-capacity.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
