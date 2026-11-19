"""Lizard's sizes and plans and the settings an admin sets for them: what
sbx may ask of a Lizard account (Limits) and the size a sandbox needs.

Lizard has no vCPU or memory options: a sandbox is one of its sizes, and one
started from a snapshot runs at the size of the sandbox the snapshot was
taken from ("size is ignored when restoring a snapshot", lizard-sdk
docs/sandboxes.md). sbx's templates are snapshots there, so the base
template is built at the size the limits name and every sandbox of the
installation runs at it: the vCPU, memory and disk limits are the
installation's size, not a ceiling per sandbox."""
from __future__ import annotations

from ..base import LimitRefused, LimitSpec, Preset, SettingField, limit_settings

KIND = "lizard"
API_KEY = "LIZARD_API_KEY"
API_URL_KEY = "LIZARD_API_URL"
PROJECT_KEY = "LIZARD_PROJECT"
REGION_KEY = "LIZARD_REGION"
PREVIEW_DOMAIN_KEY = "LIZARD_PREVIEW_DOMAIN"
BASE_TEMPLATE_KEY = "LIZARD_BASE_TEMPLATE"
DEFAULT_API_URL = "https://lizard.build"   # lizard-sdk config.py DEFAULT_API_URL
DEFAULT_REGION = "us-east-1"               # lizard-sdk config.py DEFAULT_SANDBOX_REGION
DEFAULT_BASE_TEMPLATE = "base"
SOURCE = "your Lizard plan"   # what sets the limits, as messages name it

# The sizes (https://lizard.build/docs/platform/limits/#sandbox-sizes, read
# 2026-11-19): name, vCPUs, memory and disk in MiB (Lizard's GB as GiB). Nano
# (half a core, 1 GB) is left out: sbx counts whole vCPUs.
SIZES = (("mini", 1, 2 * 1024, 10 * 1024), ("small", 2, 4 * 1024, 20 * 1024), ("medium", 4, 8 * 1024, 40 * 1024),
         ("large", 8, 16 * 1024, 80 * 1024), ("big", 16, 32 * 1024, 120 * 1024))
# The longest lifetime Lizard's API takes is 2,147,483,647 ms; here in whole 5 minutes.
LONGEST = 2_147_400
TRIAL_SECONDS = 3600

# The limits: the runtime.env key that sets each, its default and the web
# app's slider. The default size is Small (2 vCPUs, 4 GiB, 20 GiB): sbx's
# default sandbox size, and the largest the trial runs.
LIMITS = (
    LimitSpec("timeout_seconds", "LIZARD_MAX_TIMEOUT", LONGEST, "Longest run", "seconds", 300, LONGEST, 300),
    LimitSpec("cpu", "LIZARD_MAX_CPU", 2, "vCPUs", "", 1, 16, 1),
    LimitSpec("memory_mb", "LIZARD_MAX_MEMORY_MB", 4 * 1024, "Memory", "MiB", 2048, 32768, 2048),
    LimitSpec("disk_mb", "LIZARD_MAX_DISK_MB", 20 * 1024, "Disk", "MiB", 10 * 1024, 120 * 1024, 10 * 1024),
    LimitSpec("sandboxes", "LIZARD_MAX_SANDBOXES", 20, "Sandboxes at once", "", 1, 600, 1),
)
SETTINGS = (
    SettingField(API_KEY, "API key", secret=True, help="From lizard.build: API keys (liz_…)."),
    SettingField(PROJECT_KEY, "Project", optional=True,
                 help="The Lizard project sandboxes are billed to: its ID, slug or name. Needed when the key "
                      "sees more than one."),
    SettingField(REGION_KEY, "Region", default=DEFAULT_REGION, optional=True,
                 help="Where sandboxes run and previews are served from; the trial runs in US regions only."),
    SettingField(API_URL_KEY, "API URL", default=DEFAULT_API_URL, optional=True, kind="url", rekey=True, hidden=True,
                 help="Only for another Lizard API."),
    SettingField(PREVIEW_DOMAIN_KEY, "Preview domain", optional=True, kind="hostname", hidden=True,
                 help="Where Lizard serves exposed ports; default sandbox.<region>.onlizard.com."),
    SettingField(BASE_TEMPLATE_KEY, "Base template", default=DEFAULT_BASE_TEMPLATE, optional=True, hidden=True,
                 help="The Lizard template sbx's base template is built on."),
    *limit_settings(LIMITS),
)
# Lizard's plans as limits, from https://lizard.build/docs/platform/limits/
# (read 2026-11-19). Running at once is counted in vCPU there: 40 on Pro,
# 220 on Pro 5x, 1,200 on Pro 20x; the trial runs 1 Small, 2 Mini and 2 Nano.
# sbx counts sandboxes, so a plan's number is its vCPU over the preset size's
# (Small: 2), and stays adjustable with the size. Lifetime: the trial deletes
# a sandbox 1 hour after it starts at the latest; paid plans take up to
# 2,147,483,647 ms.
_PLAN_SOURCE = "https://lizard.build/docs/platform/limits/"
TRIAL_NOTE = ("Trial: Lizard deletes every sandbox an hour after it starts, paused or not, so nothing kept in one "
              "survives. Use it to try the connection only.")
_SIZE_NOTE = ("Every sandbox runs at the size set here (Lizard sizes a sandbox by its snapshot). Lizard counts "
              "running vCPUs ({vcpu} on this plan): sandboxes at once is that over the size's vCPUs.")


def _plan(name, title, vcpu, size_name, timeout=LONGEST, note=""):
    _, cpu, memory, disk = next(s for s in SIZES if s[0] == size_name)
    return Preset(name, title, {"timeout_seconds": timeout, "cpu": cpu, "memory_mb": memory, "disk_mb": disk,
                                "sandboxes": max(1, vcpu // cpu)}, _PLAN_SOURCE,
                  adjustable=() if name == "trial" else ("cpu", "memory_mb", "disk_mb", "sandboxes"),
                  note=note or _SIZE_NOTE.format(vcpu=vcpu)).bound(LIMITS)


PRESETS = (_plan("trial", "Trial", 2, "small", timeout=TRIAL_SECONDS, note=TRIAL_NOTE),
           _plan("pro", "Pro", 40, "small"), _plan("pro-5x", "Pro 5x", 220, "small"),
           _plan("pro-20x", "Pro 20x", 1200, "small"))


def size(cpu: int | None, memory_mb: int | None, disk_mb: int | None) -> str:
    """The smallest Lizard size that holds these; LimitRefused when none does."""
    for name, most_cpu, most_memory, most_disk in SIZES:
        if (cpu or 0) <= most_cpu and (memory_mb or 0) <= most_memory and (disk_mb or 0) <= most_disk:
            return name
    raise LimitRefused(f"Lizard has no size with {cpu} vCPUs, {memory_mb} MiB and {disk_mb} MiB of disk; its largest "
                       "(big) has 16 vCPUs, 32 GB and 120 GB", field="cpu", asked=cpu, allowed=16)
