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


class Installation(unittest.TestCase):
    """setUpClass makes the installation and migrates it; setUp empties the
    settings and what a test leaves in the tables named in RESET."""
    SETTINGS: dict = {}
    RUNTIME_ENV = RUNTIME   # /etc/sbx/runtime.env as install.sh writes it
    RESET = ("settings", "config", "config_history", "repos", "sealed", "store_values", "template_builds",
             "repo_setup_prs", "sandbox_lifecycle_events", "sandbox_lifecycle", "sandboxes", "attachments",
             "sandbox_services", "secrets", "sandbox_random", "env_pending", "package_versions", "package_current",
             "managed_servers", "ip_preview_routes", "ip_preview_retired", "ip_preview_missing", "repo_runtimes",
             "runtime_requests")

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix=f"sbx-{cls.__name__}-")
        cls.root = Path(cls.directory.name)
        cls.environment = patch.dict(os.environ, {
            "SBX_ETC": str(cls.root / "etc"), "SBX_STATE_DIR": str(cls.root / "state"),
            "SBX_STATE": str(cls.root / "state/sbx.db"), "SBX_RUN_DIR": str(cls.root / "run"),
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
        from sbx_control import recipes, settings, state
        # No test asks GitHub for a repo's default branch (self.remote_head: the real lookup).
        self.remote_head = recipes.remote_head
        self.enterContext(patch.object(recipes, "remote_head", return_value=None))
        with state.db() as conn:
            present = state.tables(conn)
            for table in self.RESET:
                if table in present:
                    conn.execute(f"delete from {table}")
        settings.forget()
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

    def repo_admin(self):
        """Set up, with operator@example.com the signed-in admin."""
        self.choose_ip()
        self.create_admin()
        self.assertEqual(self.client.post('/api/setup/complete', headers=self.headers).status_code, 200)
