"""The Claude Code connector: cloud sessions of the installation's
self-hosted environment (claude.ai), each in a sandbox of its own, and the
pinned `claude` CLI in every template, which `sbx open claude` uses too."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.request

from ... import core, extensions, host, lifecycle, platform, settings, state, vault
from ...connections.base import Check, Field, Refused, Status
from ...contributions import Check as Ready, Contribution, RuntimeLayer, TemplateLayer
from ...state import db
from .sessions import KIND, ORCHESTRATOR_HEALTH_PORT, RUNNER_HEALTH_PORT, session_of

HEALTH = f"http://127.0.0.1:{ORCHESTRATOR_HEALTH_PORT}/healthz"
CLI = "/root/.local/bin/claude"
SESSION_PREVIEW_GRACE_SECONDS = 5 * 60
APPLY = "orchestrator"   # the host effect that restarts its orchestrator (a new secret or version)
SECRET = vault.Key("ENVIRONMENT_SECRET", KIND, applies=(APPLY,))
SCHEMA = """
create table if not exists runner_orders (
    order_id text primary key, session_id text not null, sandbox text,
    status text not null, error text, pid integer,
    created_at text not null default current_timestamp, updated_at text not null default current_timestamp);
create table if not exists claude_sessions (session_id text primary key, sandbox text not null unique);
"""


class Settings(settings.ExtensionSection):
    """orchestrator.sh reads these as claude_<field>."""
    applies = (APPLY,)
    enabled: bool = False
    version: str = "2.1.287"
    hook_concurrency: int = 4
    expected_spawn_seconds: int = 150
    release_idle_session_min: int = 120
    kill_session_after_min: int = 480


class ClaudeCode(extensions.Connector):
    kind = KIND
    title = "Claude Code"
    shown = ("version", "sessions_running")
    section = Settings
    ports = (RUNNER_HEALTH_PORT,)
    activity = (f"{platform.HOME}/.claude/projects",)
    busy = ("self-hosted-runner",)           # a session's runner (runner.start)
    # The developer's subscription token, which `sbx open` writes; shells source it (contribution).
    login = extensions.Login(f"{platform.RUN}/claude-env", "claude", get=("claude", "setup-token"),
                             env="CLAUDE_CODE_OAUTH_TOKEN")

    def migrate(self, conn):
        conn.executescript(SCHEMA)

    def on_restore(self, conn):
        """Orders in flight were for sandboxes the old host had."""
        if "runner_orders" in state.tables(conn):
            conn.execute("update runner_orders set status = 'failed', error = 'the installation moved' "
                         "where status in ('received', 'provisioning')")

    def ready(self) -> Ready:
        version = self.settings().version
        return Ready(f'[ "$(claude --version 2>/dev/null | cut -d" " -f1)" = "{version}" ]')

    def contribution(self, target) -> Contribution:
        """Shells source claude-env (login) when it is there."""
        version = self.settings().version
        return Contribution(
            "claude-code",
            template=TemplateLayer(root=(f"npm install -g --prefix /usr/local @anthropic-ai/claude-code@{version} >/dev/null",)),
            runtime=RuntimeLayer(sources=("claude-env",)), ready=self.ready(),
            # Its transcripts (source, secrets) and any login it kept.
            scrub_paths=("$HOME/.claude", "$HOME/.claude.json"))

    def router(self):
        from . import api
        return api.router

    def keeper_policy(self, info, use: extensions.Use) -> extensions.Policy | None:
        """A session's sandbox pauses as soon as the session ends (its runner
        released it when idle, or it was archived or deleted) and nobody is
        on it; the next order or preview request resumes it."""
        from . import runner
        name = (info.metadata or {}).get("sbx")
        session = session_of(name) if name else None
        if not session or core.runtime_state(info) != "running":
            return None
        lock = runner.session_lock(session)
        if not lock.acquire(blocking=False):
            return extensions.Policy(decided=True)  # an order is starting its runner
        try:
            if use.probe.busy or use.probe.ssh:
                return extensions.Policy(keep_awake=True, decided=True)
            current = lifecycle.get(name)
            if use.accessed_within(SESSION_PREVIEW_GRACE_SECONDS) or use.resumed_for_preview \
                    or (current and current["active"]):
                return extensions.Policy(decided=True)
            started = time.time()
            try:
                core.pause(name, info)
            except Exception as error:
                state.event(name, "pause failed", f"its session ended: {error}")
                raise
            state.event(name, "paused", f"its session ended; took {time.time() - started:.0f}s")
            return extensions.Policy(decided=True)
        finally:
            lock.release()

    def origin(self, info) -> dict:
        session = session_of((info.metadata or {}).get("sbx") or "")
        return {"kind": "session", "label": "Claude Code session", "detail": session or "", "href": ""}

    def host_services(self) -> list[extensions.HostService]:
        s = self.settings()
        here = f"{host.CURRENT}/sbx_control/connectors/claude_code"
        prepare = (f'[ "$({CLI} --version 2>/dev/null | cut -d" " -f1)" = "{s.version}" ] || '
                   f"curl -fsSL https://claude.ai/install.sh | HOME=/root bash -s {s.version} > /tmp/claude-install.log 2>&1")
        return [extensions.HostService(
            "claude-orchestrator", "Claude Code self-hosted runner orchestrator",
            f"WorkingDirectory={host.CURRENT}\nExecStart={here}/orchestrator.sh\nRuntimeDirectory=claude-orchestrator\nRuntimeDirectoryMode=0700\n"
            f"Environment=HOME=/root DISABLE_AUTOUPDATER=1 SBX_CONTROL_PORT={platform.CONTROL_PORT} "
            f"ORCHESTRATOR_HEALTH_PORT={ORCHESTRATOR_HEALTH_PORT}\nRestart=always\nRestartSec=600",
            enabled=s.enabled, apply=APPLY, prepare=prepare)]

    def status(self) -> Status:
        s = self.settings()
        secret = vault.facts(SECRET)
        facts = {"version": s.version}
        if not s.enabled:
            state_, summary = "off", "off"
        elif not secret.set:
            state_, summary = "not_connected", "waiting for the environment secret from claude.ai"
        else:
            state_, summary = "connected", f"connected · version {s.version}"
            facts["sessions_running"] = _sessions_running()
        fields = (Field("environment_secret", "Environment secret", secret=True, set=secret.set,
                        help="From claude.ai: Settings → Self-hosted environments"),
                  Field("enabled", "Enabled", value="true" if s.enabled else "false"),
                  Field("version", "Claude Code version", value=s.version))
        return Status(self.kind, self.title, state_, summary, facts, fields, ("connect", "test", "disconnect"))

    def test(self, values: dict | None = None) -> list[Check]:
        s = self.settings()
        checks = []
        secret_set = vault.facts(SECRET).set or bool((values or {}).get("environment_secret"))
        checks.append(Check("environment secret", secret_set, "set" if secret_set else "not set",
                            "paste it from claude.ai"))
        if shutil.which(CLI) or os.path.exists(CLI):
            out = subprocess.run([CLI, "--version"], capture_output=True, text=True, timeout=20)
            found = out.stdout.split()[0] if out.stdout.split() else ""
            checks.append(Check("claude CLI", found == s.version, f"{found or 'unknown'} on this host (wanted {s.version})",
                                "save the connection again to install the pinned version"))
        else:
            checks.append(Check("claude CLI", False, f"{CLI} is missing", "save the connection again"))
        try:
            with urllib.request.urlopen(HEALTH, timeout=5) as r:
                checks.append(Check("orchestrator", r.status == 200, f"answers on {HEALTH}", ""))
        except Exception as error:
            checks.append(Check("orchestrator", False, f"not answering on {HEALTH}: {error}",
                                "it starts within 10 minutes of the secret being set; journalctl -u claude-orchestrator"))
        return checks

    def connect(self, values: dict, *, by: str) -> Status:
        new = {**self.settings().model_dump()}
        secret = (values.get("environment_secret") or "").strip()
        if "enabled" in values:
            new["enabled"] = str(values["enabled"]).lower() in ("1", "true", "yes", "on")
        elif secret:
            new["enabled"] = True
        if values.get("version"):
            new["version"] = str(values["version"]).strip()
        if secret and len(secret) < 16:
            raise Refused("the environment secret looks too short")
        if secret:  # takes effect at the orchestrator's next start, which the key asks for
            vault.put(SECRET, secret, by=by)
        self.save_settings(new, by=by)
        return self.status()

    def disconnect(self, *, by: str) -> Status:
        vault.delete(SECRET, by=by)
        self.save_settings({**self.settings().model_dump(), "enabled": False}, by=by)
        return self.status()


def _sessions_running() -> int:
    with db() as conn:
        names = [row[0] for row in conn.execute("select sandbox from claude_sessions")]
    return lifecycle.running_count(names)
