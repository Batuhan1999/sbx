"""Inspect systemd timers, template builds, and lifecycle maintenance jobs."""
from __future__ import annotations

import datetime
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import core, embed_storage, infisical_api, keeper, settings, template_builds, templates

@dataclass(frozen=True)
class Job:
    """One scheduled job: a systemd timer on the host, or a loop of the
    keeper's or the server's. schedule and enabled read the owning module's
    settings and constants when the list is made."""
    id: str
    group: str
    title: str
    text: Callable[[], str]
    schedule: Callable[[], str]
    kind: str = "keeper"                       # timer: a systemd unit; keeper: a loop in sbx-control
    enabled: Callable[[], bool] = lambda: True
    runnable: bool = False


def _every(seconds: int) -> str:
    return "every minute" if seconds == 60 else f"every {seconds // 60} minutes"


def _s():
    return settings.current()


JOBS = (
    Job("e2b-storage-gc", "storage", "Template storage cleanup",
        lambda: "Deletes template layers no build or live sandbox needs.", lambda: "hourly", "timer", runnable=True),
    Job("sbx-backup", "secrets", "Backup",
        lambda: "A backup bundle of the installation, whatever the secret store (settings backup: every_hours, to, keep).",
        lambda: f"every {_s().backup.every_hours} hours", "timer", enabled=lambda: bool(_s().backup.every_hours),
        runnable=True),
    Job("sbx-tls-renew", "certificate", "Certificate renewal",
        lambda: "certbot keeps the wildcard certificate fresh; renews within 30 days of expiry.", lambda: "daily",
        "timer", runnable=True),
    Job("pause-after-idle", "lifecycle", "Pause after idle",
        lambda: f"A sandbox nobody uses for {core.TIMEOUT_SECONDS // 3600} hours is paused (snapshot to disk); "
                "opening a preview resumes it.", lambda: f"keeper, {_every(keeper.INTERVAL_SECONDS)}"),
    Job("delete-after-paused", "lifecycle", "Delete after paused",
        lambda: f"A sandbox paused longer than paused_days ({_s().sandboxes.paused_days:g}) is deleted with its "
                "services; 0 keeps them.", lambda: f"keeper, {_every(keeper.INTERVAL_SECONDS)}"),
    Job("services-reconciler", "lifecycle", "Services reconciler",
        lambda: "What provisioners made for sandboxes that are gone is deleted (their list scripts).",
        lambda: _every(keeper.RECONCILE_SECONDS), runnable=True),
    Job("stale-template-rebuild", "templates", "Stale template rebuild",
        lambda: "A template whose inputs changed (the platform layer, a connector, a version, .sbx/) rebuilds, "
                "one at a time.", lambda: _every(template_builds.CHECK_SECONDS)),
    Job("nightly-template-rebuild", "templates", "Nightly template rebuild",
        lambda: f"Repos whose default branch moved without touching .sbx/ rebuild at "
                f"{template_builds.NIGHTLY_REBUILD_HOUR:02}:00 host time.",
        lambda: f"{template_builds.NIGHTLY_REBUILD_HOUR:02}:00 daily"),
    Job("secret-store-sync", "secrets", "Secret store sync",
        lambda: "Values edited in Infisical itself reach the sandboxes that use them.",
        lambda: f"{_every(infisical_api.POLL_SECONDS)}, and its webhook", enabled=lambda: _s().store.mode == "infisical"),
)
TIMERS = {job.id for job in JOBS if job.kind == "timer"}
_started = time.time()


def _systemd(unit: str) -> dict:
    try:
        out = subprocess.run(["systemctl", "show", f"{unit}.timer", "-p", "LastTriggerUSec,NextElapseUSecRealtime,ActiveState,Result"],
                             capture_output=True, text=True, timeout=5).stdout
        service = subprocess.run(["systemctl", "show", f"{unit}.service", "-p", "Result,ExecMainExitTimestamp,ActiveState"],
                                 capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {"available": False}
    props = dict(line.split("=", 1) for line in (out + service).splitlines() if "=" in line)
    if not props or props.get("ActiveState") in (None, "inactive") and "LastTriggerUSec" not in props:
        return {"available": False}
    return {"available": True, "enabled": props.get("ActiveState") == "active",
            "last": _when(props.get("LastTriggerUSec") or props.get("ExecMainExitTimestamp")),
            "next": _when(props.get("NextElapseUSecRealtime")), "result": props.get("Result", "")}


def _when(text: str | None):
    if not text or text in ("0", "n/a", ""):
        return None
    try:  # systemd prints "Wed 2026-10-29 02:00:00 UTC"
        parts = text.split()
        return datetime.datetime.strptime(" ".join(parts[1:3]), "%Y-%m-%d %H:%M:%S").isoformat() + "Z"
    except (ValueError, IndexError):
        return text


def jobs() -> list:
    """Every job, classified; timers from systemd, the keeper's from what
    sbx-control knows."""
    out = []
    for job in JOBS:
        info = _systemd(job.id) if job.kind == "timer" else {"available": True, "uptime_s": int(time.time() - _started)}
        if not job.enabled() and not (job.kind == "timer" and info.get("available")):
            continue
        out.append({"id": job.id, "group": job.group, "title": job.title, "text": job.text(), "kind": job.kind,
                    "runnable": job.runnable, "schedule": job.schedule(), **info})
    for repo, build in sorted(template_builds.builds().items()):
        out.append({"id": f"template:{repo}", "group": "templates", "title": f"Template build · {repo}", "kind": "build",
                    "text": build.get("error") or "", "status": build.get("status"), "last": build.get("finished_at") or build.get("started_at"),
                    "available": True, "runnable": build.get("status") != "building"})
    usage = embed_storage.USAGE
    out.append({"id": "compact", "group": "storage", "title": "Snapshot compaction", "kind": "manual", "available": True, "runnable": False,
                "text": "A paused sandbox's chain of pause snapshots folded into one build: `e2b-compact-snapshot <sandbox id> --apply` on the host."
                        + (f" Usage per sandbox in {usage}." if usage.exists() else "")})
    return out


def run_now(job_id: str) -> str:
    if job_id in TIMERS:
        try:
            subprocess.run(["systemctl", "start", "--no-block", f"{job_id}.service"], check=True, capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"could not start {job_id}: {error}")
        return f"{job_id} started"
    if job_id == "services-reconciler":
        threading.Thread(target=keeper.reconcile_services, daemon=True).start()
        return "reconciler started"
    if job_id.startswith("template:"):
        repo = job_id.split(":", 1)[1]
        if not (repo == templates.BASE or templates.is_default(repo) or settings.trust(repo)):
            raise RuntimeError(f"{repo} is not served here")
        if not template_builds.start(repo):
            raise RuntimeError(f"{repo}'s template is already building")
        return f"building {repo}"
    raise RuntimeError(f"{job_id} is not something to run")
