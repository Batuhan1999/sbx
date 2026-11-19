"""E2B Embed on this host: Firecracker VMs that E2B's self-hosted stack runs
from /opt/e2b (vendor/e2b-embed). The SDK calls are the shared E2B adapter's
(runtime.e2b), each with Embed's own connection, which configure takes from
runtime.env; this package adds the host side."""
from __future__ import annotations

import os

from ..base import Capabilities, Limits
from ..e2b import ENVD_PORT, Compute
from ..e2b import template as template  # noqa: F401  (the builder steps, as Embed's tests and tools name them)
from . import capacity, storage

CLIENT_PROXY_PORT = 3002    # Embed's client-proxy, which reaches <port>-<sandbox id> hosts
# The <port>-<id> host suffix client-proxy routes when there is no domain.
INTERNAL_HOST_SUFFIX = "e2b.sandboxes.internal"
# What install.sh asks of a host before it enables Embed there.
MIN_MEMORY_GB, MIN_DISK_GB = 15, 60
# Embed's PAUSE_ADMISSION_DISK_HEADROOM_MIB (vendor/e2b-embed compose.yaml): a
# pause needs room on disk for the sandbox's whole memory plus this.
PAUSE_DISK_HEADROOM_MB = 1024


class Embed(Compute):
    kind = "embed"
    title = "E2B Embed"
    host = True
    capabilities = Capabilities(snapshots=True, private_networks=True, egress_rules=True, image_import=True)
    disk_path = str(storage.ROOT)
    ingress = f"http://127.0.0.1:{CLIENT_PROXY_PORT}"
    ingress_cleared = ("E2b-Sandbox-Id", "E2b-Sandbox-Port")   # client-proxy routes on them too
    internal_suffix = INTERNAL_HOST_SUFFIX
    ports = frozenset({ENVD_PORT})
    host_unit = "e2b-embed.service"
    credential_keys = ("E2B_API_KEY", "E2B_API_URL", "E2B_SANDBOX_URL", "E2B_HOST", "E2B_DOMAIN")
    secret_key = "E2B_API_KEY"

    def configure(self, secrets):
        """install.sh copies the key and URLs from Embed's sdk.env into runtime.env."""
        if "E2B_API_KEY" not in secrets or not ({"E2B_API_URL", "E2B_HOST"} & secrets.keys()):
            raise SystemExit("missing platform secrets: E2B_API_KEY and E2B_API_URL (/etc/sbx/runtime.env, written "
                             "by install.sh)")
        host = secrets.get("E2B_HOST", "")
        # A sandbox. hostname makes client-proxy route by header (proxy/host.go).
        self.connect_with(api_key=secrets["E2B_API_KEY"], api_url=secrets.get("E2B_API_URL") or f"http://{host}:3000",
                          sandbox_url=secrets.get("E2B_SANDBOX_URL") or f"http://sandbox.{host}:{CLIENT_PROXY_PORT}",
                          domain=secrets.get("E2B_DOMAIN"))

    def upstream_host(self, port, sandbox_id):
        from ... import addressing
        return f"{port}-{sandbox_id}.{addressing.domain() or INTERNAL_HOST_SUFFIX}"

    def own_route(self, host, sandbox_id, port):
        """sandbox.<domain> is the E2B dashboard's header-routed host: the
        target is in the headers, so the same rules apply to it as to the
        preview hosts, but for envd, which checks its own token."""
        if host != dashboard_host():
            return None
        if port == str(ENVD_PORT):
            return True
        return int(port or 0), sandbox_id

    def capacity(self, memory_mb):
        return capacity.room(memory_mb)

    def limits(self):
        """This host's: a VM has at most its cores and all of the hugepages
        that back sandbox memory. How many run at once is capacity()'s."""
        return Limits(cpu=os.cpu_count(), memory_mb=capacity.hugepages_mb())

    def placing(self):
        return capacity.placing()

    def apply_network(self):
        from . import network
        return network.apply()

    def disk(self):
        return storage.disk()

    def free_disk_mb(self):
        return storage.free_mb()

    def why_busy(self, memory_mb):
        free = storage.free_mb()
        if memory_mb and free is not None and free < memory_mb + PAUSE_DISK_HEADROOM_MB:
            return (f"the runtime's disk has {free / 1024:.1f} GB free and a pause needs room for the sandbox's "
                    f"whole memory ({memory_mb / 1024:.1f} GB) plus {PAUSE_DISK_HEADROOM_MB // 1024} GB; free disk "
                    "(delete sandboxes or templates nobody uses, sbx-admin template prune) and pause again")
        return ("the runtime is still saving the template or snapshot the sandbox started from, or another "
                "sandbox's snapshot; pause again in a minute")

    def snapshot_usage(self, sandbox_id):
        return storage.usage().get(sandbox_id) or {}

    def collect_storage(self):
        """Start the storage GC (e2b-storage-gc) now, on a host that runs Embed."""
        import subprocess
        if not storage.ROOT.exists():
            return
        try:
            subprocess.run(["systemctl", "start", "--no-block", "e2b-storage-gc.service"], capture_output=True,
                           timeout=10)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"storage GC: {error!r}", flush=True)

    def jobs(self):
        from ...jobs import Job
        usage = storage.USAGE
        return [
            Job("e2b-storage-gc", "storage", "Template storage cleanup",
                lambda: "Deletes template layers no build or live sandbox needs.", lambda: "hourly", "timer",
                runnable=True),
            Job("compact", "storage", "Snapshot compaction",
                lambda: "A paused sandbox's chain of pause snapshots folded into one build: "
                        "`e2b-compact-snapshot <sandbox id> --apply` on the host."
                        + (f" Usage per sandbox in {usage}." if usage.exists() else ""),
                lambda: "", "manual"),
        ]

    def preview(self, name, port, listen, *, preserve_host=False):
        from .proxy import serve
        serve(name, port, listen, preserve_host=preserve_host)

    def host_units(self):
        from . import host
        return host.units()

    def host_commands(self, into):
        from . import host
        return host.commands(into)

    def apply_host(self, key):
        from . import host
        return host.apply(key)

    def guard_host(self):
        from . import network
        network.guard()

    def can_run(self, facts):
        """As install.sh checks before it enables Embed: /dev/kvm, memory, free disk."""
        if not facts.get("kvm"):
            return "needs /dev/kvm: bare metal, or a VM with nested virtualization enabled"
        if facts.get("memory_gb") is not None and facts["memory_gb"] < MIN_MEMORY_GB:
            return ("needs 16 GiB of memory: 12 for the host and 4 for one sandbox; "
                    f"this server has {facts['memory_gb']} GiB")
        if facts.get("disk_free_gb") is not None and facts["disk_free_gb"] < MIN_DISK_GB:
            return f"needs {MIN_DISK_GB} GiB of free disk; this server has {facts['disk_free_gb']} GiB"
        return ""

    def readiness(self, facts):
        from ... import vault
        from ...connections.base import Check
        return [Check("Virtualization", facts.get("kvm", False),
                      "KVM is available" if facts.get("kvm") else "KVM is unavailable",
                      "The server needs /dev/kvm; rerun install.sh after enabling it."),
                Check("Runtime configured", bool(vault.runtime().get("E2B_API_KEY")),
                      "Sandbox runtime credentials", "Run install.sh to finish installing the runtime.")]

    def status(self):
        from . import connection
        return connection.status()

    def test(self):
        from . import connection
        return connection.test()


def own_sandboxes() -> list:
    """Embed's own sandboxes, whether or not SBX_RUNTIMES enables it: what
    recreating its services would lose. Raises when Embed does not answer."""
    from ... import runtime, vault
    if Embed.kind in runtime.enabled():
        return runtime.get(Embed.kind).sandboxes()
    embed = Embed()
    embed.configure(vault.runtime(Embed.credential_keys))
    return embed.sandboxes()


def dashboard_host() -> str:
    """sandbox.<domain>: the E2B dashboard's header-routed host."""
    from ... import addressing
    return f"sandbox.{addressing.domain()}"

