"""When templates build: the scheduler's rounds (stale templates on every
enabled runtime, the moved repos' at night) and builds started in a process
of their own for short-lived commands. What a build is and does is
template_builds'."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import threading
import time

from . import runtime, templates
from . import template_builds as builds


def start_detached(kind: str, keys=None, *, if_stale: bool = True) -> tuple[str, bool]:
    """Build keys (default: the base, then the default template) on the
    runtime of kind in a process of their own, as `python -m
    sbx_control.template_builds KEY --runtime KIND --if-stale` one after
    the other, so a short-lived command (sbx-admin runtime add, sbx-admin
    jobs run) can start them and return: a transient systemd unit (one per
    runtime: while it runs, another is not started), or a detached process
    on a host without systemd. A key may be the command's arguments as a
    list (["key", "owner/app"]); if_stale False builds even a current one.
    Returns (where to follow them, whether this call started them);
    RuntimeError when systemd refused the unit."""
    from . import paths
    keys = keys or ["base", "default"]
    stale = " --if-stale" if if_stale else ""
    steps = " && ".join(f"{shlex.quote(sys.executable)} -m sbx_control.template_builds "
                        f"{shlex.join(key if isinstance(key, (list, tuple)) else [key])} "
                        f"--runtime {shlex.quote(kind)}{stale}" for key in keys)
    cwd = os.getcwd()
    unit = f"sbx-template-builds-{templates.slug(kind)}"
    where = f"{unit}.service (journalctl -u {unit})"
    try:
        active = subprocess.run(["systemctl", "is-active", "--quiet", f"{unit}.service"], capture_output=True,
                                timeout=10)
        if active.returncode == 0:
            return where, False
        made = subprocess.run(["systemd-run", "--no-block", "--collect", f"--unit={unit}",
                               f"--working-directory={cwd}", "sh", "-c", steps],
                              capture_output=True, text=True, timeout=30)
    except FileNotFoundError:   # no systemd here at all
        log = paths.state_dir() / f"{unit}.log"
        with open(log, "ab") as out:
            subprocess.Popen(["sh", "-c", steps], cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        return str(log), True
    if made.returncode != 0:
        raise RuntimeError(f"systemd-run did not start {unit}: {(made.stderr or made.stdout).strip()[-300:]}")
    return where, True


def _build_quietly(key: str, kind: str | None = None) -> bool:
    """False when the host had no room to start it (builds.NoRoom)."""
    try:
        builds.build_now(key, kind)
    except builds.NoRoom as error:  # nothing started; tried again next round
        print(f"template build {key}{builds._on(kind)}: not now: {error}", flush=True)
        return False
    except Exception as error:
        print(f"template build {key}{builds._on(kind)}: {error!r}", flush=True)
    return True


def schedule(stop: threading.Event):
    """Until stop: every builds.CHECK_SECONDS the stale templates on every enabled
    runtime, and at NIGHTLY_REBUILD_HOUR the moved repos', one at a time,
    for the disk's sake."""
    night = None
    wait = builds.first_wait()   # soon, when a restart cut a build off
    while not stop.wait(wait):
        wait = builds.CHECK_SECONDS
        try:
            for kind in runtime.enabled():
                try:
                    builds.forget_missing(kind)   # one listing; what is gone is stale below
                except Exception as error:
                    print(f"template check{builds._on(kind)}: {error!r}", flush=True)
                for key in builds.stale(kind):
                    if stop.is_set():
                        return
                    _build_quietly(key, kind)
            today = time.strftime("%Y-%m-%d")
            if time.localtime().tm_hour == builds.NIGHTLY_REBUILD_HOUR and night != today:
                night = today
                for repo in builds.moved():
                    builds._unmark(repo)
                    started = [_build_quietly(repo, kind) for kind in runtime.enabled() if repo in builds.keys(kind)]
                    if not all(started):
                        builds.mark_moved(repo)   # no room tonight: the next night again
            builds.prune()
        except Exception as error:  # one bad round must not stop the scheduler
            print(f"template scheduler: {error!r}", flush=True)


def start_template_builds(kind: str) -> None:
    """A runtime just added starts its base and default templates now, not
    on the scheduler's next round; in a process of their own, so this
    command returns."""
    title = runtime.title(kind)
    try:
        where, started = start_detached(kind)
    except Exception as error:
        print(f"could not start the template builds on {title} ({error}); the scheduler starts them within "
              f"minutes, or: sbx-admin jobs run template@{kind}:{templates.BASE}", file=sys.stderr)
        return
    if not started:
        print(f"the {templates.BASE} and {templates.DEFAULT} builds on {title} are already running ({where}); "
              f"`sbx-admin runtime verify {kind}` works once they're built")
        return
    print(f"building {templates.BASE} and {templates.DEFAULT} on {title} now (about 10 minutes the first time; "
          f"{where}); `sbx-admin runtime verify {kind}` works once they're built")
