"""Claude Code cloud sessions' orders (runner.py). The spawn-runner hook on
this host posts every order here; the session's sandbox and its runner come
up on a thread, through the public sandbox API."""
import threading

from fastapi import APIRouter, HTTPException, Request

from ... import addressing, auth, core, extensions, recipes, sandboxes, settings, state
from . import runner
from .sessions import KIND, bind, sandbox_of

router = APIRouter()


def check_room(order: runner.Order):
    """NoCapacity when the session's sandbox has no room: none needed for its
    running sandbox, room on its own runtime for its paused one, else room
    for a new one on the runtime it will run on (runtimes.choose)."""
    from ... import runtime, runtimes
    found = sandbox_of(order.session_id)
    info = core.find(found) if found else None
    if info is not None:
        if core.runtime_state(info) == "running":
            return
        memory = getattr(info, "memory_mb", None) or core.app_of(info).size["memory_mb"]
        core.check_capacity(memory, runtime.kind_of(info))
        return
    # The repo creation will use: the order's, else the installation's default (sandboxes.create).
    repo = recipes.repo_of(order.primary_repo_url) or settings.current().default_repo or None
    core.check_capacity(recipes.load(repo).size["memory_mb"] if repo else None, runtimes.of_repo(repo))


@router.post("/internal/runner-orders")
def runner_order(order: runner.Order, request: Request):
    """4xx tells the hook the order can never succeed."""
    auth.require_loopback(request)
    if not order.session_id:
        print(f"runner order {order.order_id}: pre-warming request, nothing to do", flush=True)
        return {"status": "ignored"}
    reason = runner.rejection(order)
    if not reason:
        try:
            check_room(order)
        except core.NoCapacity as error:
            # Retryable for the orchestrator (the hook exits 1), and the message reaches the session.
            print(f"runner order {order.order_id} for {order.session_id}: refused, {error}", flush=True)
            raise HTTPException(503, str(error)) from None
    status, added = runner.receive(order.order_id, order.session_id, reason)
    print(f"runner order {order.order_id} for {order.session_id}: {status}{' (redelivered)' if not added else ''}"
          f"{f', {reason}' if reason else ''}", flush=True)
    if reason:
        raise HTTPException(422, reason)
    if added:
        threading.Thread(target=provision_session, args=(order,), daemon=True).start()
    return {"status": status}


def provision_session(order: runner.Order):
    """A session comes back with a new order after its idle runner released
    it, and gets its sandbox again, resumed if paused."""
    name, fresh = None, False
    try:
        with runner.session_lock(order.session_id):
            runner.advance(order.order_id, "provisioning")
            try:
                sandbox, fresh = session_sandbox(order)
                name = sandbox.name
                slug = core.slug_of(core.find(name))
                runner.start(sandbox, addressing.urls(slug, sandbox.app), order.work_order, slug,
                             extensions.get(KIND).settings())
                runner.advance(order.order_id, "runner_started", sandbox=name)
                state.event(name, "claude runner started", f"session {order.session_id}", internal=True)
            except Exception as error:
                runner.advance(order.order_id, "failed", error=str(error)[:2000])
                print(f"runner order {order.order_id}: failed: {error!r}", flush=True)
                if name:
                    state.event(name, "claude runner failed", f"session {order.session_id}: {error}")
                return
    finally:
        runner.done(order.order_id)
    if fresh:
        sandboxes.restart(name, core.find(name), prepare=True)


def session_sandbox(order: runner.Order) -> tuple[core.Attached, bool]:
    """(the session's sandbox, attached with Claude Code verified in it;
    whether it is new)."""
    found = sandbox_of(order.session_id)
    info = core.find(found) if found else None
    if info:  # its app restarts on a thread when it has to, so the runner need not wait
        return core.attach(found, "for its Claude session", info=info, wait=False, ready=True), False
    name, owner, rev = runner.sandbox_name(order), runner.owner(order), runner.revision(order)
    if found:
        sandboxes.delete(found)
    bind(order.session_id, name)
    try:
        sandboxes.create(sandboxes.CreateSandbox(name=name, branch=rev, public_keys="",
                                                 repo=recipes.repo_of(order.primary_repo_url)),
                         owner, start_app=False, metadata={"connector": KIND})
    except Exception:
        bind(order.session_id, None)
        raise
    sandbox = core.attach(name, "for its Claude session", run_app=False, ready=True)
    sandbox.checkout(rev)
    return sandbox, True
