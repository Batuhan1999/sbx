"""Embed on the settings page: its status and checks. Read-only: install.sh
made it and wrote its credentials to /etc/sbx/runtime.env (vault.runtime)."""
from __future__ import annotations

import sqlite3
import urllib.request

from ... import vault
from ...connections.base import Check, Field, Status
from . import storage

KIND, TITLE = "runtime", "Sandbox runtime"


def status() -> Status:
    env = vault.runtime()
    facts: dict = {}
    if not env.get("E2B_API_KEY"):
        return Status(KIND, TITLE, "not_connected", "no runtime.env: run install.sh", facts, (), ("test",))
    running = _running()
    facts["running"] = running
    from ... import core
    room = core.capacity(kind="embed")
    if room.get("known"):
        facts["slots"] = f"{room['slots_total'] - room['slots_free']} of {room['slots_total']} in use ({room['memory_mb'] // 1024} GB each)"
    disk = storage.disk()
    if disk:
        facts["disk_percent"], facts["disk_free_gb"] = disk["percent"], disk["free_gb"]
    full = room.get("known") and room["slots_free"] < 1
    state = "attention" if facts.get("disk_percent", 0) >= 85 or full else "connected"
    summary = f"E2B Embed · {running} running" + (" · full" if full else "") + (f" · disk {facts['disk_percent']}%" if "disk_percent" in facts else "")
    return Status(KIND, TITLE, state, summary, facts, (Field("api", "API", value=env.get("E2B_API_URL", ""), kind="url"),), ("test",))


def test() -> list[Check]:
    env = vault.runtime()
    url = env.get("E2B_API_URL", "http://127.0.0.1:3000")
    checks = []
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=5) as r:
            checks.append(Check("Embed API", r.status == 200, f"{url} answers"))
    except Exception as error:
        checks.append(Check("Embed API", False, f"{url}: {error}", "systemctl status e2b-embed"))
    disk = storage.disk()
    if disk:
        checks.append(Check("disk", disk["percent"] < 85, f"{disk['percent']}% used, {disk['free_gb']} GB free",
                            "delete unused sandboxes; e2b-compact-snapshot for long chains"))
    return checks


def _running() -> int:
    from ... import lifecycle
    try:
        return lifecycle.running_count()
    except sqlite3.Error:
        return 0
