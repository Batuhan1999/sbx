"""Boat's plans, machines and the settings an admin sets for them: what
sbx may ask of a Boat account (Limits) and the machine class a size needs."""
from __future__ import annotations

import re

from ..base import LimitRefused, LimitSpec, Preset, SettingField, limit_settings

KIND = "boat"
API_KEY = "BOAT_API_KEY"
API_URL_KEY = "BOAT_API_URL"
ORG_KEY = "BOAT_ORG"
DEFAULT_API_URL = "https://boat.dev/api/v1"

# The machine classes (https://docs.boat.dev/machines.md, read 2026-11-17):
# name, vCPUs, memory and disk in MiB (Boat's GB as GiB). A sandbox gets the
# smallest one its size fits.
MACHINES = (("small", 2, 4 * 1024, 51 * 1024), ("default", 4, 8 * 1024, 51 * 1024),
            ("large", 8, 16 * 1024, 111 * 1024), ("xlarge", 16, 32 * 1024, 251 * 1024))
MONTH = 30 * 86400   # the longest auto-stop on a paid plan

# The limits: the runtime.env key that sets each, its default (the $20
# plan's, PRESETS) and the web app's slider.
LIMITS = (
    LimitSpec("timeout_seconds", "BOAT_MAX_TIMEOUT", MONTH, "Longest run", "seconds", 300, MONTH, 300),
    LimitSpec("cpu", "BOAT_MAX_CPU", 8, "vCPUs", "", 2, 16, 2),
    LimitSpec("memory_mb", "BOAT_MAX_MEMORY_MB", 16 * 1024, "Memory", "MiB", 4096, 32768, 4096),
    LimitSpec("disk_mb", "BOAT_MAX_DISK_MB", 111 * 1024, "Disk", "MiB", 51 * 1024, 251 * 1024, 1024),
    LimitSpec("sandboxes", "BOAT_MAX_SANDBOXES", 100, "Sandboxes at once", "", 1, 2000, 1),
)
SETTINGS = (
    SettingField(API_KEY, "API key", secret=True, help="From Boat's dashboard: API keys, with the scope full-sandbox (boat_…).",
                 help_url="https://boat.dev/dashboard"),
    SettingField(API_URL_KEY, "API URL", default=DEFAULT_API_URL, optional=True, kind="url", rekey=True, hidden=True,
                 help="Only for another Boat API."),
    SettingField(ORG_KEY, "Organization", optional=True, help="Bill an organization's wallet instead of yours."),
    *limit_settings(LIMITS),
)
# Boat's plans as limits, from https://docs.boat.dev/pricing.md,
# https://docs.boat.dev/limits.md and https://docs.boat.dev/machines.md (read
# 2026-11-17). Every paid plan: auto-stop up to 30 days. $20: 100 sandboxes
# at once, sizes up to large (8 vCPUs, 16 GB, 111 GB). $100: 300 at once and
# xlarge (16 vCPUs, 32 GB, 251 GB). $500: 1,000. $2000: 2,000. The trial:
# 2 at once, auto-stop at most 2 hours, the default size (4 vCPUs, 8 GB), and
# no saved snapshots: Boat answered a save on a trial account (2026-11-17)
# "Saved snapshots are not available during the free trial. End the trial
# (first payment) to unlock them." sbx's templates are saved snapshots, so a
# trial account is accepted (sandboxes from templates built once it pays)
# and a template build on it is refused before it starts a builder.
_PLAN_SOURCE = "https://docs.boat.dev/pricing.md"
SOURCE = "your Boat plan"   # what sets the limits, as messages name it


# Saved snapshots past the plan's free ones are paid from the wallet, daily
# (NamedSnapshotAllowance, read 2026-11-19); a save that has neither fails.
SNAPSHOTS_FULL = ("Boat's free saved snapshots are used up: your plan keeps {free}, and all are taken. sbx's "
                  "templates are saved snapshots, and old ones from other servers on this Boat account count too. "
                  "Delete the ones you don't need in Boat's dashboard (Snapshots), or add money to your Boat wallet "
                  "(an extra one costs ${price:.2f} a month), then build again.")


TRIAL_NOTE = "Trial: templates need the first payment (Boat saves no snapshots during the trial)."


def _plan(name, title, sandboxes, largest, timeout=None, note=""):
    _, cpu, memory, disk = next(m for m in MACHINES if m[0] == largest)
    return Preset(name, title, {"timeout_seconds": timeout or MONTH, "cpu": cpu, "memory_mb": memory,
                                "disk_mb": disk, "sandboxes": sandboxes}, _PLAN_SOURCE, note=note).bound(LIMITS)


PRESETS = (_plan("trial", "Trial", 2, "default", timeout=2 * 3600, note=TRIAL_NOTE), _plan("20", "$20", 100, "large"),
           _plan("100", "$100", 300, "xlarge"),
           _plan("500", "$500", 1000, "xlarge"), _plan("2000", "$2000", 2000, "xlarge"))
_TRIAL_WHY = ("Boat's free trial saves no snapshots (\"Saved snapshots are not available during the free "
              "trial\"), and sbx's templates are saved snapshots: make the first payment in Boat's dashboard "
              "(Billing), then ")
TRIAL_REFUSAL = _TRIAL_WHY + "build again."
# A trial key is refused when it is saved (Boat.refused_plans): the setup builds templates, which it can't.
TRIAL_KEY_REFUSAL = _TRIAL_WHY + "save the key again."


def preset_of(plan_key: str) -> str | None:
    """The preset Boat's plan key names (limits' sandboxPlanKey: "trial", "box_20", "20"), None for one sbx
    does not know: its number is the paid plan's price."""
    key = (plan_key or "").strip().lower()
    if "trial" in key:
        return "trial"
    digits = re.findall(r"\d+", key)
    return next((p.name for p in PRESETS if digits and p.name == digits[-1]), None)
KEY_WARN_DAYS = 14
PLAN_CACHE_SECONDS = 300


def machine(cpu: int | None, memory_mb: int | None, disk_mb: int | None) -> str:
    """The smallest machine class a size fits; LimitRefused when none does."""
    for name, most_cpu, most_memory, most_disk in MACHINES:
        if (cpu or 0) <= most_cpu and (memory_mb or 0) <= most_memory and (disk_mb or 0) <= most_disk:
            return name
    raise LimitRefused(f"Boat has no machine with {cpu} vCPUs, {memory_mb} MiB and {disk_mb} MiB of disk; its largest "
                       "(xlarge) has 16 vCPUs, 32 GB and 251 GB", field="cpu", asked=cpu, allowed=16)
