"""Inspect systemd timers, template builds, and lifecycle maintenance jobs."""
from __future__ import annotations

import datetime
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

from . import core, infisical_api, keeper, runtime, settings, template_builds, template_scheduler, templates, updates

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
    kind: str = "keeper"   # timer: a systemd unit; keeper: a loop in sbx-control; manual: run by hand
    enabled: Callable[[], bool] = lambda: True
    runnable: bool = False


def _every(seconds: int) -> str:
    return "every minute" if seconds == 60 else f"every {seconds // 60} minutes"


def _s():
    return settings.current()


JOBS = (
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
    Job("release-check", "updates", "Release check",
        lambda: f"Looks for a newer release of {_s().updates.repository} on GitHub, which admins can install from "
                "the web app (settings updates: check, repository, prereleases).",
        lambda: f"every {updates.CHECK_SECONDS // 3600} hours", enabled=lambda: _s().updates.check),
)
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
    for job in defined():
        info = (_systemd(job.id) if job.kind == "timer" else {"available": True} if job.kind == "manual"
                else {"available": True, "uptime_s": int(time.time() - _started)})
        if not job.enabled() and not (job.kind == "timer" and info.get("available")):
            continue
        out.append({"id": job.id, "group": job.group, "title": job.title, "text": job.text(), "kind": job.kind,
                    "runnable": job.runnable, "schedule": job.schedule(), **info})
    several = len(runtime.enabled()) > 1
    for kind in runtime.enabled():
        prefix = "template:" if kind == runtime.default() else f"template@{kind}:"
        on = f" · {runtime.title(kind)}" if several else ""
        for repo, build in sorted(template_builds.builds(kind).items()):
            out.append({"id": f"{prefix}{repo}", "group": "templates", "title": f"Template build · {repo}{on}",
                        "kind": "build", "text": build.get("error") or "", "status": build.get("status"),
                        "last": build.get("finished_at") or build.get("started_at"),
                        "available": True, "runnable": build.get("status") != "building"})
    return out


def defined() -> tuple:
    """The platform's jobs, then the runtime's upkeep (runtime.jobs)."""
    return (*JOBS, *runtime.jobs())


def run_now(job_id: str, *, detached: bool = False) -> str:
    """Start a job now. detached (sbx-admin jobs run, a command that returns
    at once): a template build runs in a process of its own (template_builds.
    start_detached), not in this one, which would end it when it returns."""
    if job_id in {job.id for job in defined() if job.kind == "timer"}:
        try:
            subprocess.run(["systemctl", "start", "--no-block", f"{job_id}.service"], check=True, capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(f"could not start {job_id}: {error}")
        return f"{job_id} started"
    if job_id == "services-reconciler":
        threading.Thread(target=keeper.reconcile_services, daemon=True).start()
        return "reconciler started"
    if job_id.startswith(("template:", "template@")):
        head, _, repo = job_id.partition(":")
        kind = head.partition("@")[2] or None   # template@<kind>:<key> on another runtime than the default
        if kind and kind not in runtime.enabled():
            raise RuntimeError(f"no runtime {kind} is enabled here")
        if not (repo == templates.BASE or templates.is_default(repo) or settings.trust(repo)):
            raise RuntimeError(f"{repo} is not served here")
        if detached:
            kind = kind or runtime.default()
            if why := template_builds.base_not_ready(repo, kind):
                raise RuntimeError(why)
            where, started = template_scheduler.start_detached(kind, [["key", repo]], if_stale=False)
            if not started:
                raise RuntimeError(f"a template build runs on {kind} already; follow it: {where}, then run this again")
            return f"building {repo} on {kind} in {where}; sbx-admin jobs shows its status"
        try:
            started = template_builds.start(repo, kind)
        except template_builds.NoRoom as error:
            raise RuntimeError(str(error)) from None
        if not started:
            raise RuntimeError(f"{repo}'s template is already building")
        return f"building {repo}"
    raise RuntimeError(f"{job_id} is not something to run")
