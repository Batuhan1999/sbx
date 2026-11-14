"""Amp's project caches: when a thread pauses with a snapshot key, a scrubbed
filesystem snapshot of its sandbox, which later orbs of the same repo start
from.

Export never publishes a workspace's runtime identity: it snapshots the
source privately, scrubs an offline clone of it with the repo's
.sbx/snapshot hook and what every contribution names (scrub_paths), and
publishes only the clone."""
from __future__ import annotations

import hashlib
import os
import shlex
import sqlite3

from e2b import Sandbox

from ... import core, extensions, platform, recipes, state
from ...template_files import sbx_guest
from .common import JobError, orb_lock

OFFLINE = r'''set -eu
sudo iptables -w -I OUTPUT 1 -o lo -j ACCEPT
sudo iptables -w -I OUTPUT 2 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
sudo iptables -w -P OUTPUT DROP
sudo ip6tables -w -P OUTPUT DROP'''

SCRUB = f'''set -eu
sudo swapoff -a
sudo rm -f {sbx_guest.SWAPFILE}
sudo rm -rf {platform.RUN}
mkdir -p "$HOME/.ssh"
find "$HOME/.ssh" -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +
sudo rm -f /etc/ssh/ssh_host_* {sbx_guest.IDENTITY}
rm -f {sbx_guest.SETUP_LOG} {sbx_guest.SUPERVISOR_LOG} {sbx_guest.log_path("*")}
rm -f "$HOME/.bash_history" "$HOME/.zsh_history"'''
TRIM = "sync\nsudo fstrim /\nsync"


def scrub(info) -> str:
    paths = extensions.scrub_paths(core.target_of(info))
    # $HOME stays expandable; the rest of each path is quoted.
    quoted = " ".join('"$HOME"' + shlex.quote(p[len("$HOME"):]) if p.startswith("$HOME") else shlex.quote(p)
                      for p in paths)
    return SCRUB + (f"\nrm -rf {quoted}" if quoted else "") + "\n" + TRIM


def export(name: str, label: str) -> str:
    info = core.find(name)
    if info is None:
        raise core.SandboxError("the workspace disappeared before snapshot export")
    recipe = core.app_of(info)
    hook = recipe.dir / "snapshot"
    if not hook.is_file() or not os.access(hook, os.X_OK):
        raise core.SandboxError("project snapshots require an executable .sbx/snapshot cleanup hook; "
                                "keep app-specific credentials and writable data out of the reusable cache")
    staging = checkpoint = published = None
    try:
        # This private checkpoint is temporary, like a pause snapshot. Never
        # publish it: it still belongs to the source workspace's identity.
        checkpoint = Sandbox.create_snapshot(info.sandbox_id, name=label + "-private", mode="filesystem")
        actual = Sandbox.get_info(info.sandbox_id)
        if not isinstance(actual.memory_mb, int) or actual.memory_mb <= 0:
            raise core.SandboxError("cannot determine the snapshot clone's actual memory allocation")
        with core.reserve_capacity(actual.memory_mb):
            staging = Sandbox.create(checkpoint.snapshot_id, timeout=240,
                                     metadata={"sbx_cache_build": label}, envs={},
                                     network={"deny_out": ["0.0.0.0/0"]},
                                     lifecycle={"on_timeout": "kill"})
        # The runtime's CIDR interface is IPv4-only. Block every new guest
        # connection too, including internal ranges and IPv6, before the hook.
        staging.commands.run(OFFLINE, timeout=30)
        staging.commands.run(f"sudo rm -rf {platform.RUN} && sudo install -d -m 700 -o user -g user {platform.RUN} "
                             f"&& : > {platform.RUN}/env", timeout=30)
        clone = core.Attached(name, staging, recipe)
        clone.ship_recipe()
        # The operator's reviewed recipe runs only in the disposable clone,
        # without service env and without network access. Never reset the source.
        clone.run(f"cd {shlex.quote(recipe.workdir)} && {platform.APP_SCRIPTS}/snapshot", timeout=90)
        staging.commands.run(scrub(info), timeout=30)
        published = Sandbox.create_snapshot(staging.sandbox_id, name=label, mode="filesystem")
        return published.snapshot_id
    finally:
        failures = []
        try:
            if staging is not None:
                Sandbox.kill(staging.sandbox_id)
        except Exception as error:
            failures.append(error)
        try:
            if checkpoint is not None:
                Sandbox.delete_snapshot(checkpoint.snapshot_id)
        except Exception as error:
            failures.append(error)
        if failures:
            if published is not None:
                try:
                    Sandbox.delete_snapshot(published.snapshot_id)
                except Exception:
                    pass
            state.event(name, "snapshot cleanup deferred", label, internal=True)
            raise core.SandboxError("temporary snapshot cleanup did not finish; retry the snapshot job")


def repo_of(key: str) -> str:
    if not key:
        return ""
    with state.db() as conn:
        row = conn.execute("select repo from amp_snapshots where key = ?", (key,)).fetchone()
    return row[0] if row else ""


def cached(key: str, repo: str) -> str | None:
    """The project cache a new orb of the repo starts from, or None (the repo's template)."""
    if not key:
        return None
    with state.db() as conn:
        row = conn.execute("select repo, snapshot_id from amp_snapshots where key = ?", (key,)).fetchone()
    if row and row[0] != repo:
        raise JobError("the project snapshot belongs to a different repository")
    return row[1] if row else None


def save(name: str, key: str, repo: str):
    with orb_lock("snapshot:" + key):
        existing = repo_of(key)
        if existing:
            if existing != repo:
                raise JobError("the project snapshot belongs to a different repository")
            return  # A repeated pause must not replace an already published cache.
        label = "sbx-amp-project-" + hashlib.sha256(key.encode()).hexdigest()[:24]
        hook = recipes.load(repo).dir / "snapshot"
        if not hook.is_file() or not os.access(hook, os.X_OK):
            raise JobError("add an executable .sbx/snapshot hook to remove app-specific credentials and local "
                           "writable data from the temporary cache copy; see docs/amp-orbs.md")
        source = (core.find(name).metadata or {}).get("template", "")
        snapshot_id = export(name, label)
        try:
            with state.db() as conn:
                conn.execute("insert into amp_snapshots (key, repo, snapshot_id, source_template) values (?, ?, ?, ?)",
                             (key, repo, snapshot_id, source))
        except Exception:
            Sandbox.delete_snapshot(snapshot_id)
            raise


def records():
    with state.db() as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("select * from amp_snapshots order by created_at")]


def delete(key: str):
    with orb_lock("snapshot:" + key), state.db() as conn:
        row = conn.execute("select snapshot_id from amp_snapshots where key = ?", (key,)).fetchone()
        if row:
            Sandbox.delete_snapshot(row[0])
            conn.execute("delete from amp_snapshots where key = ?", (key,))
