"""Boat's plans, machines and the settings an admin sets for them: what
sbx may ask of a Boat account (Limits) and the machine class a size needs."""
from __future__ import annotations

from ..base import LimitRefused, Limits, Preset, SettingField

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

# The limits, by Limits field: the runtime.env key and its default, the $20
# plan's (PRESETS).
LIMITS = {"timeout_seconds": ("BOAT_MAX_TIMEOUT", MONTH), "cpu": ("BOAT_MAX_CPU", 8),
          "memory_mb": ("BOAT_MAX_MEMORY_MB", 16 * 1024), "disk_mb": ("BOAT_MAX_DISK_MB", 111 * 1024),
          "sandboxes": ("BOAT_MAX_SANDBOXES", 100)}
LIMIT_FORM = {"timeout_seconds": ("Longest run", "seconds", 300, MONTH, 300),
              "cpu": ("vCPUs", "", 2, 16, 2),
              "memory_mb": ("Memory", "MiB", 4096, 32768, 4096),
              "disk_mb": ("Disk", "MiB", 51 * 1024, 251 * 1024, 1024),
              "sandboxes": ("Sandboxes at once", "", 1, 2000, 1)}
SETTINGS = (
    SettingField(API_KEY, "API key", secret=True, help="From Boat's dashboard: API keys (boat_…)."),
    SettingField(API_URL_KEY, "API URL", default=DEFAULT_API_URL, optional=True, rekey=True,
                 help="Only for another Boat API."),
    SettingField(ORG_KEY, "Organization", optional=True, help="Bill an organization's wallet instead of yours."),
    *(SettingField(key, LIMIT_FORM[field][0], default=str(default), kind="int", limit=field, unit=LIMIT_FORM[field][1],
                   min=LIMIT_FORM[field][2], max=LIMIT_FORM[field][3], step=LIMIT_FORM[field][4])
      for field, (key, default) in LIMITS.items()),
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


TRIAL_NOTE = "Trial: templates need the first payment (Boat saves no snapshots during the trial)."


def _plan(name, title, sandboxes, largest, timeout=None, note=""):
    _, cpu, memory, disk = next(m for m in MACHINES if m[0] == largest)
    return Preset(name, title, {"BOAT_MAX_TIMEOUT": str(timeout or MONTH), "BOAT_MAX_CPU": str(cpu),
                                "BOAT_MAX_MEMORY_MB": str(memory), "BOAT_MAX_DISK_MB": str(disk),
                                "BOAT_MAX_SANDBOXES": str(sandboxes)}, _PLAN_SOURCE, note=note)


PRESETS = (_plan("trial", "Trial", 2, "default", timeout=2 * 3600, note=TRIAL_NOTE), _plan("20", "$20", 100, "large"),
           _plan("100", "$100", 300, "xlarge"),
           _plan("500", "$500", 1000, "xlarge"), _plan("2000", "$2000", 2000, "xlarge"))
TRIAL_REFUSAL = ("Boat's free trial saves no snapshots (\"Saved snapshots are not available during the free "
                 "trial\"), and sbx's templates are saved snapshots: make the first payment in Boat's dashboard "
                 "(Billing), then build again.")
KEY_WARN_DAYS = 14
PLAN_CACHE_SECONDS = 300


def limits_from(secrets: dict) -> Limits:
    found = {}
    for field, (key, default) in LIMITS.items():
        value = secrets.get(key) or default
        try:
            found[field] = int(value)
        except (TypeError, ValueError):
            raise SystemExit(f"{key}={value} in /etc/sbx/runtime.env is not a whole number") from None
    try:
        return Limits(**found, source="your Boat plan", keys={f: key for f, (key, _) in LIMITS.items()})
    except ValueError as error:
        raise SystemExit(f"{LIMITS['timeout_seconds'][0]} in /etc/sbx/runtime.env: {error}") from None


def machine(cpu: int | None, memory_mb: int | None, disk_mb: int | None) -> str:
    """The smallest machine class a size fits; LimitRefused when none does."""
    for name, most_cpu, most_memory, most_disk in MACHINES:
        if (cpu or 0) <= most_cpu and (memory_mb or 0) <= most_memory and (disk_mb or 0) <= most_disk:
            return name
    raise LimitRefused(f"Boat has no machine with {cpu} vCPUs, {memory_mb} MiB and {disk_mb} MiB of disk; its largest "
                       "(xlarge) has 16 vCPUs, 32 GB and 251 GB", field="cpu", asked=cpu, allowed=16)
