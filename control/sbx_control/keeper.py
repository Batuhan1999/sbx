"""The keeper: every minute, keep used sandboxes awake and let the rest
pause, delete long-paused ones, deliver waiting env changes, run the
prepares that waited for a busy sandbox, and every 10 minutes reconcile the
services and learn which repos GitHub serves anonymously."""
import shlex
import threading
import time

from . import core, extensions, gate, github_app, lifecycle, platform, routing, runtime, sandboxes, settings, state, views

INTERVAL_SECONDS = 60
RECONCILE_SECONDS = 600
AGENT_ACTIVE_MINUTES = 10


def probe(sandbox_id) -> extensions.Probe:
    """A look inside the sandbox: open SSH sessions, agent transcripts
    written in the last minutes in the directories extensions name
    (activity), and extensions' processes running (busy). Agents append to
    their transcript on every message and tool call; load and CPU say
    nothing, the idle app keeps the load near 1 and an agent waiting on its
    model uses no CPU."""
    paths, busy = extensions.activity_paths(), extensions.busy_processes()
    transcripts = (f"find {shlex.join(paths)} -name '*.jsonl' -mmin -{AGENT_ACTIVE_MINUTES} 2>/dev/null | wc -l"
                   if paths else "echo 0")
    # [x]yz matches xyz but not this command's own text.
    pattern = "|".join(f"[{p[0]}]{p[1:]}" for p in busy)
    processes = f"pgrep -fc {shlex.quote(pattern)} || true" if busy else "echo 0"
    ssh, agents, running = runtime.connect(sandbox_id).commands.run(
        f"ss -Htn state established '( sport = :{platform.SSH_PORT} )' | wc -l; {transcripts}; {processes}",
        timeout=15).stdout.split()
    return extensions.Probe(int(ssh), int(agents), int(running))


def notice_stopped_app(info):
    """A ready sandbox's app whose processes no longer run is no longer
    reported ready: readiness is a checkpoint, so without this a supervisor
    stopped by a full /run left the sandbox app_ready."""
    name = (info.metadata or {}).get("sbx")
    record = lifecycle.get(name) if name else None
    if not record or record["active"] or record["application"] != "healthy":
        return
    try:
        why = core.check_app(info)
    except runtime.NotRunning:
        raise
    except Exception as error:  # the sandbox did not answer this round
        print(f"keeper: checking {name}'s app: {error!r}", flush=True)
        return
    if why and lifecycle.app_stopped(name, info.sandbox_id, why):
        state.event(name, "app stopped", why)


def use_of(info, running: bool) -> extensions.Use:
    """What the keeper knows about the sandbox's use; a paused one is not looked into."""
    return extensions.Use(gate.last_access(info.sandbox_id), gate.resumed_for_preview(info.sandbox_id),
                          (lambda: probe(info.sandbox_id)) if running else extensions.Probe)


_reconciling = threading.Lock()


def reconcile_services():
    if not _reconciling.acquire(blocking=False):
        return
    try:
        core.reconcile_services()
    except Exception as error:
        print(f"reconcile: {error!r}", flush=True)
    finally:
        _reconciling.release()


def learn_repo_access():
    """Whether each known repo works without credentials (readiness reads it)."""
    try:
        github_app.learn_access(r for r in views.known_repos() if settings.trust(r) == "trusted")
    except Exception as error:
        print(f"keeper: repo access: {error!r}", flush=True)


def delete_paused(info):
    policy = extensions.keeper_policy(info, use_of(info, running=False))
    if policy and policy.keep_paused:
        return
    name = (info.metadata or {}).get("sbx")
    try:
        if name:
            with core.sandbox_operation(name):
                current = core.find(name)
                if not current or current.sandbox_id != info.sandbox_id:
                    return
                sandboxes.delete_locked(name)
        else:
            runtime.kill(info.sandbox_id)
        state.event(name or info.sandbox_id, "deleted", f"paused since {info.end_at:%Y-%m-%d %H:%M} UTC, "
                                                       f"over {settings.current().sandboxes.paused_days:g} days")
    except Exception as error:
        print(f"deleting paused {name or info.sandbox_id}: {error!r}", flush=True)


def keep_awake(info):
    """Push its timeout out: it is used. A use that is not a preview's
    request ends what a preview request's resume holds open (gate)."""
    runtime.set_timeout(info.sandbox_id, core.TIMEOUT_SECONDS)
    gate.forget_preview_resume(info.sandbox_id)


def look_at(info, held=frozenset()) -> bool:
    """One sandbox's round; whether it runs. held: the sandboxes a lifecycle
    operation holds (lifecycle.busy), kept awake whatever their use: a setup
    may run longer than a runtime's timeout limit (runtime.limits)."""
    vm = core.runtime_state(info)
    sandboxes.record_transition(info, vm)
    if vm != "running":
        gate.forget_preview_resume(info.sandbox_id)
    if vm == "paused" and settings.current().sandboxes.paused_days and info.end_at \
            and time.time() - info.end_at.timestamp() > settings.current().sandboxes.paused_days * 86400:
        delete_paused(info)
    if vm != "running":
        return False
    notice_stopped_app(info)
    if settings.current().sandboxes.detected_previews != "off":
        routing.detect_ports(info.sandbox_id, core.app_of(info))
    if (info.metadata or {}).get("sbx") in held:
        keep_awake(info)
        return True
    use = use_of(info, running=True)
    policy = extensions.keeper_policy(info, use)
    if policy and policy.decided:
        if policy.keep_awake:
            keep_awake(info)
        return True
    if use.accessed_within(core.TIMEOUT_SECONDS) or use.probe.ssh or use.probe.agents or use.probe.busy:
        keep_awake(info)
    return True


def look_once():
    """A deleted paused preview comes back with the PR's next push."""
    infos = sandboxes.registered(core.sandboxes())
    routing.forget_except(i.sandbox_id for i in infos)
    running = set()
    held = lifecycle.busy()
    for info in infos:
        try:
            if look_at(info, held):
                running.add((info.metadata or {}).get("sbx"))
        except runtime.NotRunning:
            # The runtime paused or deleted it since the listing (its own
            # timeout): nothing failed, the next round sees it as it is.
            gate.forget_preview_resume(info.sandbox_id)
    sandboxes.deliver_waiting(running)
    sandboxes.prepare_waiting({(i.metadata or {}).get("sbx") for i in infos})


def run(stop: threading.Event):
    """The services' reconciler runs on a thread of its own, as its scripts
    may take minutes."""
    reconciled_at = 0.0
    while not stop.wait(INTERVAL_SECONDS):
        if time.time() - reconciled_at >= RECONCILE_SECONDS:
            reconciled_at = time.time()
            threading.Thread(target=reconcile_services, daemon=True).start()
            threading.Thread(target=learn_repo_access, daemon=True).start()
        try:
            look_once()
        except Exception as error:  # one bad round must not stop the keeper
            print(f"keeper: {error!r}", flush=True)
