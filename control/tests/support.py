"""What the tests share: a throwaway installation per test class (its own
/etc/sbx, state directory, sbx.db and /run/sbx under a temporary directory;
paths are read on every call, so classes in one process never see each
other's), and boundary fakes."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

RUNTIME = "E2B_API_KEY=test-only\nE2B_API_URL=http://127.0.0.1:59999\n"


def _builds_settled(seconds: float = 10.0) -> None:
    """A template build a test started on a thread (template_builds.start) ends before the next test: one
    still running would hold its key (_running) and write its row into the next test's tables."""
    import time
    from sbx_control import template_builds
    deadline = time.monotonic() + seconds
    while template_builds._running and time.monotonic() < deadline:
        time.sleep(0.05)
    if template_builds._running:
        raise AssertionError(f"template builds still running after {seconds:g} s: {sorted(template_builds._running)}")


def forget_extensions() -> None:
    """Load the extensions afresh next time, with the connections that hold
    them: never one without the other, or a connection is no longer its
    extension."""
    from sbx_control import connections, extensions
    extensions._connectors = None
    connections.REGISTRY.clear()


def live_pid(test: unittest.TestCase) -> int:
    """The pid of a live process that isn't this one, stopped when test ends:
    another build's or job's owner. Not the parent: a runner may not keep it."""
    import subprocess
    import sys
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    test.addCleanup(child.wait)
    test.addCleanup(child.kill)
    return child.pid


def amp_tools(gateway: bool = True, executable: bool = True) -> bytes:
    """A gzipped amp-wayland tarball with the files Amp's ready check runs."""
    import io
    import tarfile
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for path in ("amp-wayland/bin/labwc", "amp-wayland/bin/waymote-gateway", "amp-wayland/libexec/waymote-streamd"):
            if not gateway and path.endswith("waymote-gateway"):
                continue
            entry = tarfile.TarInfo(path)
            entry.size, entry.mode = 4, 0o755 if executable else 0o644
            tar.addfile(entry, io.BytesIO(b"\x7fELF"))
    return out.getvalue()


class Installation(unittest.TestCase):
    """setUpClass makes the installation and migrates it; setUp empties the
    settings and what a test leaves in the tables named in RESET."""
    SETTINGS: dict = {}
    RUNTIME_ENV = RUNTIME   # /etc/sbx/runtime.env as install.sh writes it
    SET_UP = False          # the first-run setup is complete
    RESET = ("settings", "config", "config_history", "repos", "sealed", "store_values", "template_builds",
             "repo_setup_prs", "sandbox_lifecycle_events", "sandbox_lifecycle", "sandboxes", "attachments",
             "sandbox_services", "secrets", "sandbox_random", "env_pending", "package_versions", "package_current",
             "managed_servers", "ip_preview_routes", "ip_preview_retired", "ip_preview_missing", "repo_runtimes",
             "runtime_requests", "endpoints", "endpoint_repos", "endpoint_certs", "endpoint_credentials", "users",
             "user_links", "secret_requests")

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix=f"sbx-{cls.__name__}-")
        cls.root = Path(cls.directory.name)
        cls.environment = patch.dict(os.environ, {
            "SBX_ETC": str(cls.root / "etc"), "SBX_STATE_DIR": str(cls.root / "state"),
            "SBX_STATE": str(cls.root / "state/sbx.db"), "SBX_RUN_DIR": str(cls.root / "run"),
            # Where builds fetch what this server serves them (the base's Amp tools among them).
            "SBX_TEMPLATE_BUNDLE_ORIGIN": "https://sbx.example.test",
        })
        cls.environment.start()
        (cls.root / "etc").mkdir()
        (cls.root / "etc/runtime.env").write_text(cls.RUNTIME_ENV)
        from sbx_control import platform, settings, state
        state.migrate()
        settings.forget()
        platform.connect()

    @classmethod
    def tearDownClass(cls):
        from sbx_control import settings
        settings.forget()
        cls.environment.stop()
        cls.directory.cleanup()

    def setUp(self):
        from unittest.mock import patch
        from sbx_control import recipes, runtime, settings, state, users
        users.forget()
        self.enterContext(patch.dict(runtime._index, clear=True))   # which runtime saw which sandbox: per test
        # No test asks GitHub for a repo's default branch (self.remote_head: the real lookup).
        self.remote_head = recipes.remote_head
        self.enterContext(patch.object(recipes, "remote_head", return_value=None))
        # Every base template build serves Amp's desktop tools; no test reads them from Amp's registry.
        from sbx_control.connectors.amp import registry
        self.enterContext(patch.object(registry, "extract", return_value=amp_tools()))
        # No test asks public DNS: a domain's records are empty unless a test says otherwise, and a
        # port 53 probe hears nothing back (domain._dig, acme_dns._txt: the lookups).
        from sbx_control import acme_dns
        from sbx_control.connections import domain
        self.enterContext(patch.object(domain, "_dig", return_value=[]))
        self.enterContext(patch.object(acme_dns, "_txt", return_value=[]))
        self.addCleanup(_builds_settled)   # after the patch: cleanups run last first, so builds settle before it goes
        with state.db() as conn:
            present = state.tables(conn)
            for table in self.RESET:
                if table in present:
                    conn.execute(f"delete from {table}")
        settings.forget()
        if self.SET_UP:
            from sbx_control import setup
            setup.COMPLETE.set("1")
        for section, value in self.SETTINGS.items():
            if section == "repos":
                settings.set_repos(value, by="test")
            else:
                settings.update(section, value, by="test")

    def effects(self) -> list:
        """The host effects requested so far (and clears them)."""
        folder = self.root / "run/apply"
        found = sorted(p.name for p in folder.glob("*")) if folder.exists() else []
        for name in found:
            (folder / name).unlink()
        return found


class Server(Installation):
    """An installation with the HTTP app: self.client, and the first-run
    setup's token in self.headers."""

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from sbx_control import app, setup
        from sbx_control.connections import Check
        self.app = app
        self.token = setup.mint_token()
        self.headers = {"Authorization": f"Setup {self.token}"}
        self.client = TestClient(app.app, base_url="https://203.0.113.10")
        self.addCleanup(self.client.close)
        self.enterContext(patch("sbx_control.connections.domain.public_ip", return_value="203.0.113.10"))
        self.enterContext(patch("sbx_control.setup.host_facts", return_value={
            "kvm": True, "memory_gb": 125, "disk_free_gb": 1618}))
        self.runtime = self.enterContext(patch("sbx_control.connections.runtime.Runtime.test", return_value=[
            Check("Embed API", True, "Runtime answers"), Check("disk", True, "Space available")]))
        from datetime import datetime, timedelta, timezone
        self.expiry = self.enterContext(patch("sbx_control.connections.domain._expiry", return_value=None))
        self.ip_expiry = self.enterContext(patch("sbx_control.connections.domain._ip_expiry", return_value=
                                                 datetime.now(timezone.utc) + timedelta(hours=160)))
        self.enterContext(patch("sbx_control.connections.domain.ip_certificate_error", return_value=""))
        # A round of template builds the setup's end (or an extension's change) starts: recorded, not run.
        from sbx_control import template_scheduler
        self.real_kick = template_scheduler.kick
        self.kick = self.enterContext(patch.object(template_scheduler, "kick", return_value=True))

    def choose_ip(self):
        response = self.client.post("/api/setup/address", headers=self.headers, json={"address": "203.0.113.10"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def create_admin(self):
        response = self.client.post("/api/setup/admin", headers=self.headers, json={
            "email": "Operator@example.com", "password": "test-only-password-123"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("test-only-password-123", response.text)
        return response.json()

    def choose_agents(self, *kinds):
        response = self.client.put("/api/setup/agents", headers=self.headers, json={"agents": list(kinds)})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def repo_admin(self):
        """Set up, with operator@example.com the signed-in admin."""
        self.choose_ip()
        self.create_admin()
        self.choose_agents()
        self.assertEqual(self.client.post('/api/setup/complete', headers=self.headers).status_code, 200)
