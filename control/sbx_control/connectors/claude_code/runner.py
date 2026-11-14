"""Claude Code cloud sessions' orders and the runner in a session's sandbox.

The orchestrator hands every queued session's work order to sbx-control
through its spawn-runner hook; `claude self-hosted-runner --capacity 1` in
the session's sandbox registers with the work order and runs the session on
its creator's subscription.
"""
import hashlib
import json
import os
import re
import shlex
import threading

from pydantic import BaseModel, Field

from ... import addressing, core, platform, recipes, resources, settings
from ...state import db
from .sessions import RUNNER_HEALTH_PORT

# The runner looks for a pre-warmed clone at <base-dir>/<owner>/<repo>; the
# sandbox's checkout is bind-mounted there, so the session edits the tree
# the app runs from.
BASE_DIR = f"{platform.HOME}/work"
HOST_CONFIG = "/opt/claude-runner/host-config"
WORK_ORDER = f"{platform.RUN}/work-order"
PID = f"{platform.RUN}/runner.pid"
RUNNER_LOG = f"{platform.HOME}/.cache/claude-runner.log"
NO_OWNER = "claude-cloud"

# An order this process no longer provisions (a restart cut it off) reads as
# failed; Anthropic offers the session again with a new order.
ORDER_STATES = {
    "received": {"provisioning", "failed"},
    "provisioning": {"runner_started", "failed"},
    "runner_started": set(),
    "failed": set(),
}
_provisioning: set = set()  # orders this process is working on


def sources(state: str) -> list:
    return [s for s, targets in ORDER_STATES.items() if state in targets]


def advance(order_id: str, status: str, error=None, sandbox=None):
    with db() as conn:
        conn.execute("update runner_orders set status = ?, error = ?, sandbox = coalesce(?, sandbox), "
                     "updated_at = current_timestamp where order_id = ? and status in (select value from json_each(?))",
                     (status, error, sandbox, order_id, json.dumps(sources(status))))


def status(order_id: str, stored: str, pid) -> str:
    if stored in ("received", "provisioning") and not (pid == os.getpid() and order_id in _provisioning):
        return "failed"
    return stored


def receive(order_id: str, session_id: str, refused: str | None) -> tuple[str, bool]:
    """(the order's status, whether it is new): recorded once, redelivered as it stands."""
    with db() as conn:
        added = conn.execute("insert into runner_orders (order_id, session_id, status, error, pid) values (?, ?, ?, ?, ?) "
                             "on conflict do nothing returning order_id",
                             (order_id, session_id, "failed" if refused else "received", refused, os.getpid())).fetchone()
        if refused:
            return "failed", bool(added)
        if added:
            _provisioning.add(order_id)
            return "received", True
        stored, pid = conn.execute("select status, pid from runner_orders where order_id = ?", (order_id,)).fetchone()
    return status(order_id, stored, pid), False


def done(order_id: str):
    _provisioning.discard(order_id)


_session_locks: dict = {}


def session_lock(session_id: str) -> threading.Lock:
    """One order per session at a time, and no pause while one starts its runner."""
    return _session_locks.setdefault(session_id, threading.Lock())


class Order(BaseModel):
    """What the spawn-runner hook sends: the CLAUDE_RUNNER_* variables and
    the work-order JWT, which must stay out of logs and the database."""
    order_id: str = Field(min_length=1, max_length=200)
    session_id: str = ""
    session_uuid: str = ""
    account_email: str = Field("", repr=False)
    primary_repo_url: str = ""
    primary_repo_revision: str = ""
    work_order: str = Field(min_length=1, repr=False)


def rejection(order: Order):
    """Why the order can never be served here, or None."""
    if not re.fullmatch(r"[0-9a-fA-F]{8}(-?[0-9a-fA-F]{4}){3}-?[0-9a-fA-F]{12}", order.session_uuid):
        return "the order has no session UUID"
    repo = recipes.repo_of(order.primary_repo_url)
    if order.primary_repo_url and not settings.trust(repo):
        return f"this environment serves {', '.join(settings.current().repos)} only; start the session on one of those"
    return None


def sandbox_name(order: Order) -> str:
    """Sessions' UUIDs can share their first characters, so the name hashes
    all of it. A session finds its sandbox by its id, not this name."""
    return f"s-{hashlib.sha256(order.session_uuid.replace('-', '').lower().encode()).hexdigest()[:8]}"


def owner(order: Order) -> str:
    return order.account_email.strip().lower() or NO_OWNER


def revision(order: Order) -> str:
    return order.primary_repo_revision or recipes.default_branch(recipes.repo_of(order.primary_repo_url))


def instructions(sandbox: core.Attached, u: dict, slug: str, allocation: dict | None = None) -> str:
    """The platform's part of the session's CLAUDE.md; the app's .sbx/AGENTS.md
    follows it."""
    a = sandbox.app
    processes = sandbox.restart_command()
    restart = " and ".join(filter(None, [
        f"`{processes}`" if processes else "",
        f"`.sbx/start` in the checkout (or {platform.APP_SCRIPTS}/start)" if (a.dir / "start").exists() else ""]))
    restart = restart or "start it the way the repo's README says"
    previews = "\n".join(f"  - {label}: {url}" for label, url in u.items())
    logs = ", ".join(sandbox.logs) or "wherever the app's start script writes them"
    who = {"team": "anyone signed in", "owner": "the session's owner only"}.get(settings.current().sandboxes.detected_previews)
    detected = (f"\n- Any other server you start that listens on 0.0.0.0 (`--host 0.0.0.0`; localhost alone is not\n"
                f"  reachable) is at {addressing.detected_pattern(slug)} within seconds, for {who}.") if who and addressing.domain() else ""
    if not addressing.domain():
        detected = ("\n- Public previews use HTTPS on this server's IP with an assigned port; no SSH tunnel is needed.\n"
                    "  Find declared and detected preview URLs in the sandbox API or its page. Listen on 0.0.0.0.\n"
                    "  Cookies share an IP across ports: use the app's cookie-prefix setting with SBX_NAME\n"
                    f"  to keep development logins separate. Never use the reserved {platform.SESSION_COOKIE} cookie name.")
    resource_note = "Check `nproc`, `free -m`, `df -h /`, and `df -i /` before increasing build concurrency."
    if allocation:
        resource_note = resources.agent_brief(allocation)
    return f"""# Dev sandbox

This session runs in a dev sandbox of {a.repo}. The checkout you work in is the same tree as
{a.workdir}, from which the app is already running (in the first minutes it may still be starting).
The sandbox is yours alone; services provisioned for it (such as a database) are its own copies.

- People open the app behind the team's login. Share these links, never localhost:
{previews}
  They are also in $SBX_URL and $SBX_URL_<PREVIEW>, and as JSON in $SBX_URLS.{detected}
- Logs: {logs}.
- Restart the app: {restart}.
- Resources: {resource_note}
"""


def start(sandbox: core.Attached, u: dict, work_order: str, slug: str, connector):
    """The runner gets the sandbox's shell env minus every model credential,
    so sessions use only the token the runner gives them, never a
    developer's from `sbx open`."""
    name, a = sandbox.name, sandbox.app
    clone = f"{BASE_DIR}/{a.repo}"
    sandbox.ship_recipe()
    sandbox.write(WORK_ORDER, work_order)
    try:
        allocation = resources.snapshot(name, live=True)
    except Exception:
        allocation = None
    sandbox.write("/tmp/claude-runner.md", instructions(sandbox, u, slug, allocation))
    flags = ["--environment-secret-file", WORK_ORDER, "--capacity", "1", "--base-dir", BASE_DIR, "--configure-git",
             "--release-idle-session-min", str(connector.release_idle_session_min),
             "--kill-session-after-min", str(connector.kill_session_after_min), "--push-outcome-on-release",
             "--log-file", RUNNER_LOG, "--health-port", str(RUNNER_HEALTH_PORT), "--client-label", f"sbx-{name}"]
    exports = {
        "SELF_HOSTED_RUNNER_HOST_CONFIG_DIR": HOST_CONFIG,
        "DISABLE_AUTOUPDATER": "1",
        "SBX_PREVIEW_URL": u.get("web", ""),
        "SBX_URLS": json.dumps(u),
    }
    started = sandbox.run(f"""
chmod 600 {WORK_ORDER}
notes={a.workdir}/.sbx/AGENTS.md; [ -f "$notes" ] || notes={platform.APP_SCRIPTS}/AGENTS.md
[ ! -f "$notes" ] || {{ echo; cat "$notes"; }} >> /tmp/claude-runner.md
sudo install -D -m 644 /tmp/claude-runner.md {HOST_CONFIG}/CLAUDE.md && rm /tmp/claude-runner.md
mkdir -p {clone} {BASE_DIR} $(dirname {RUNNER_LOG})
mountpoint -q {clone} || sudo mount --bind {a.workdir} {clone}
# By its process group, setsid made it; this script's own text names the runner.
old=$(cat {PID} 2>/dev/null)
if [ -n "$old" ] && kill -- -"$old" 2>/dev/null; then
  for i in $(seq 20); do kill -0 "$old" 2>/dev/null || break; sleep 0.5; done
  kill -9 -- -"$old" 2>/dev/null
fi
cd {BASE_DIR}
(unset {' '.join(sorted(set(settings.current().sandboxes.agent_hidden)))} CLAUDE_CODE_OAUTH_TOKEN
 {''.join(f'export {k}={shlex.quote(v)}; ' for k, v in exports.items())}
 # GIT_SSH_COMMAND: the sandbox's env reaches its repo over SSH (GitHub's deploy key).
 setsid nohup claude self-hosted-runner {shlex.join(flags)} ${{GIT_SSH_COMMAND:+--git-ssh-rewrite github.com}} \
   > {RUNNER_LOG}.out 2>&1 < /dev/null &
 echo $! > {PID})
sleep 3
kill -0 $(cat {PID}) 2>/dev/null && echo started || tail -5 {RUNNER_LOG}.out
""", timeout=180).stdout.strip()
    if not started.endswith("started"):
        raise core.SandboxError(f"the runner exited at once: {started[-500:]}")
