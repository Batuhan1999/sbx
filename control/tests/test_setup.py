"""The first-run setup, sign-in, the settings and backups."""
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from support import Server


class SetupTests(Server):
    def setUp(self):
        super().setUp()
        from sbx_control import paths, settings, setup, state, variables, vault
        self.paths, self.settings, self.setup, self.state, self.variables, self.vault = (
            paths, settings, setup, state, variables, vault)

    def test_wrong_backup_key_keeps_the_installed_key_and_database_and_restarts_the_service(self):
        from cryptography.fernet import Fernet
        from sbx_control import admin, backup
        self.repo_admin()
        original_key = self.paths.secret_key().read_bytes()
        original_settings = self.settings.current()
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'backup.tgz'
            backup.bundle(archive)
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            with patch.object(admin.subprocess, 'run') as run, self.assertRaisesRegex(SystemExit, 'not the key'):
                admin.do_restore(archive, Fernet.generate_key().decode())
        self.assertEqual(self.paths.secret_key().read_bytes(), original_key)
        self.assertEqual(self.settings.current(), original_settings)
        systemctl = [call.args[0] for call in run.call_args_list if call.args[0][0] == 'systemctl']
        self.assertEqual(systemctl[-1], ['systemctl', 'start', 'sbx-control'])   # stopped for the restore, back after

    def test_backup_roundtrip_preserves_settings_and_secrets(self):
        from sbx_control import backup
        self.repo_admin()
        self.settings.add_repo('owner/backup-check', by='test')
        self.variables.put('owner/backup-check', 'ROUNDTRIP', 'fixture-value', 'test')
        key = self.paths.secret_key().read_text()
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'backup.tgz'
            backup.bundle(archive)
            self.variables.put('owner/backup-check', 'ROUNDTRIP', 'changed', 'test')
            backup.restore(archive, key)
        self.assertIn('owner/backup-check', self.settings.current().repos)
        self.assertEqual(self.variables.values('owner/backup-check')['ROUNDTRIP'], 'fixture-value')

    def test_failed_backup_preserves_the_previous_archive(self):
        from sbx_control import backup
        self.repo_admin()
        apps = self.paths.etc() / 'apps'
        apps.mkdir(exist_ok=True)
        self.addCleanup(shutil.rmtree, apps)
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'backup.tgz'
            backup.bundle(archive)
            previous = archive.read_bytes()
            (apps / 'linked').symlink_to('/etc/passwd')   # a link in the configuration fails the bundle
            with self.assertRaisesRegex(ValueError, 'not links'):
                backup.bundle(archive)
            self.assertEqual(archive.read_bytes(), previous)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ['backup.tgz'])

    def test_ip_setup_signs_in_and_retires_token_without_repo_connections(self):
        initial = self.client.get("/api/setup", headers=self.headers).json()
        self.assertEqual([s["step"] for s in initial["steps"]], ["address", "sign_in"])
        self.assertFalse(initial["can_complete"])
        self.assertEqual(self.settings.current().sign_in.method, "password")
        self.assertEqual(self.settings.current().domain.acme, "delegated")
        self.assertEqual(self.choose_ip()["access"]["url"], "https://203.0.113.10")
        self.assertIn(80, self.settings.opened_ports(self.settings.current()))
        self.assertNotIn(80, self.settings.current().network.public_ports)
        effects = self.effects()
        self.assertIn(self.settings.HostUnit.TLS, effects)
        self.assertIn(self.settings.HostUnit.HOST_GUARD, effects)
        admin = self.create_admin()
        self.assertTrue(admin["signed_in"])
        self.assertTrue(admin["can_complete"])
        # The setup header must not hide the session established on the same origin.
        self.assertTrue(self.client.get("/api/setup", headers=self.headers).json()["signed_in"])
        done = self.client.post("/api/setup/complete", headers=self.headers)
        self.assertEqual(done.status_code, 200, done.text)
        self.assertTrue(done.json()["complete"])
        self.assertFalse(self.setup.token_ok(self.token))
        self.assertFalse(self.setup.token_file().exists())
        me = self.client.get("/api/me").json()
        self.assertTrue(me["admin"])
        self.assertEqual(me["email"], "operator@example.com")
        self.assertEqual(me["base_url"], "https://203.0.113.10")
        self.assertEqual(me["repos"], [])
        from fastapi.testclient import TestClient
        with TestClient(self.app.app, base_url="https://203.0.113.10") as outsider:
            self.assertEqual(outsider.get("/api/setup", headers=self.headers).status_code, 401)
            self.assertEqual(outsider.get("/api/me").status_code, 401)
        self.client.get("/auth/logout")
        login = self.client.post("/auth/login", content="email=operator%40example.com&password=test-only-password-123",
                                 headers={"content-type": "application/x-www-form-urlencoded",
                                          "Origin": "https://203.0.113.10"}, follow_redirects=False)
        self.assertEqual(login.status_code, 303, login.text)
        self.assertEqual(self.client.get("/api/me").json()["email"], "operator@example.com")

    def test_runtime_failure_keeps_progress_and_can_be_retried(self):
        from sbx_control.connections import Check
        self.choose_ip()
        self.create_admin()
        self.runtime.return_value = [Check("Embed API", False, "Offline", "Restart the runtime.")]
        response = self.client.post("/api/setup/complete", headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.setup.complete())
        self.assertTrue(self.setup.token_ok(self.token))
        self.assertTrue(self.client.get("/api/setup", headers=self.headers).json()["signed_in"])
        self.runtime.return_value = [Check("Embed API", True)]
        self.assertTrue(self.client.post("/api/setup/complete", headers=self.headers).json()["complete"])

    def test_ip_setup_waits_for_the_active_certificate_and_allows_retry(self):
        self.ip_expiry.return_value = None
        initial = self.choose_ip()
        self.assertFalse(initial["access"]["certificate_ready"])
        self.assertFalse(initial["steps"][0]["done"])
        response = self.client.post("/api/setup/admin", headers=self.headers, json={
            "email": "operator@example.com", "password": "test-only-password-123",
        })
        self.assertEqual(response.status_code, 400)
        with patch("sbx_control.connections.domain.ip_certificate_error", return_value="HTTP validation timed out"):
            view = self.client.get("/api/setup", headers=self.headers).json()
            self.assertIn("timed out", view["access"]["certificate_error"])
        self.ip_expiry.return_value = datetime.now(timezone.utc) + timedelta(hours=160)
        self.assertTrue(self.choose_ip()["access"]["certificate_ready"])
        from sbx_control import connections
        self.assertIn(f"valid until {self.ip_expiry.return_value:%Y-%m-%d}", connections.get("domain").status().summary)
        self.create_admin()
        self.assertTrue(self.client.post("/api/setup/complete", headers=self.headers).json()["complete"])

    def test_validation_and_existing_admin_cannot_be_overwritten(self):
        body = {"email": "operator@example.com", "password": "test-only-password-123"}
        self.assertEqual(self.client.post("/api/setup/admin", headers=self.headers, json=body).status_code, 400)
        self.assertEqual(self.client.post("/api/setup/address", headers=self.headers,
                                          json={"address": "example.com/path"}).status_code, 400)
        self.choose_ip()
        for invalid in ({**body, "email": "invalid"}, {**body, "password": "short"}):
            response = self.client.post("/api/setup/admin", headers=self.headers, json=invalid)
            self.assertEqual(response.status_code, 400)
            self.assertFalse(self.vault.facts(self.vault.LOGIN_PASSWORD).set)
        self.create_admin()
        self.assertEqual(self.client.post("/api/setup/admin", headers=self.headers, json=body).status_code, 409)
        self.assertEqual(self.settings.current().access.admins, ("operator@example.com",))

    def password_login(self, email):
        self.client.get("/auth/logout")
        return self.client.post("/auth/login", content=f"email={email}&password=test-only-password-123",
                                headers={"content-type": "application/x-www-form-urlencoded", "accept": "application/json",
                                         "Origin": "https://203.0.113.10"}).status_code

    def test_admins_the_installer_named_still_get_a_login(self):
        self.settings.update("access", {"admins": ["named@example.com"]}, by="install.sh")
        self.choose_ip()
        response = self.client.post("/api/setup/admin", headers=self.headers, json={
            "email": "Operator@example.com", "password": "test-only-password-123"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.settings.current().access.admins, ("named@example.com", "operator@example.com"))
        self.assertTrue(self.client.post("/api/setup/complete", headers=self.headers).json()["complete"])
        self.assertEqual(self.password_login("named%40example.com"), 200)

    def test_the_shared_password_admits_only_the_admins_and_allowed_emails(self):
        self.choose_ip()
        self.create_admin()
        self.client.post("/api/setup/complete", headers=self.headers)
        # Whoever holds the password types any email: with no list, only the admins'.
        self.settings.update("sign_in", {"method": "password", "allowed_emails": []}, by="install.sh")
        self.assertEqual(self.password_login("colleague%40example.com"), 401)
        self.assertEqual(self.password_login("operator%40example.com"), 200)
        self.settings.update("sign_in", {**self.settings.current().sign_in.model_dump(),
                                         "allowed_emails": ["*@example.com"]}, by="test")
        self.assertEqual(self.password_login("colleague%40example.com"), 200)
        self.assertEqual(self.password_login("someone%40example.org"), 401)

    def test_both_first_installs_set_the_same_password_sign_in(self):
        import contextlib
        import io
        from sbx_control import admin
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(admin.main(["sign-in", "password"]), 1)  # no admin: nobody could sign in
        self.assertFalse(self.vault.facts(self.vault.LOGIN_PASSWORD).set)
        self.settings.update("access", {"admins": ["operator@example.com"]}, by="install.sh")
        with contextlib.redirect_stdout(out):
            self.assertEqual(admin.main(["sign-in", "password"]), 0)
        self.assertIn("generated password: ", out.getvalue())
        installed = self.settings.current().sign_in
        self.assertEqual((installed.method, installed.allowed_emails), ("password", ()))
        self.headers = {"Authorization": f"Setup {self.setup.reset(sign_in=True)}"}
        self.settings.update("access", {"admins": ["named@example.com"]}, by="install.sh")
        self.choose_ip()
        self.create_admin()
        self.assertEqual(self.settings.current().sign_in, installed)

    def test_oidc_names_who_may_sign_in_whoever_writes_it(self):
        import contextlib
        import io
        from sbx_control import admin, auth, connections
        from sbx_control.connections import Refused
        sign_in = connections.get("sign_in")
        oidc = {"method": "oidc", "oidc_issuer": "https://accounts.google.com", "oidc_client_id": "fixture-client"}
        with self.assertRaisesRegex(Refused, "allowed_emails"):
            sign_in.connect({**oidc, "oidc_client_secret": "fixture-secret"}, by="test")
        self.assertFalse(self.vault.facts(self.vault.OIDC_CLIENT_SECRET).set)
        with self.assertRaisesRegex(self.settings.Invalid, "allowed_emails"):
            self.settings.update("sign_in", oidc, by="test")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(admin.main(["config", "set", "sign_in", '{"method": "oidc"}']), 1)
        response = self.client.put("/api/settings/sign_in", headers=self.headers, json={"value": oidc})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.settings.current().sign_in.method, "password")
        # An installation that stored it before the rule admits nobody.
        from sbx_control import state
        with state.db() as conn:
            conn.execute("insert into config (section, value, updated_by) values ('sign_in', ?, 'old')",
                         (__import__("json").dumps(oidc),))
        self.settings.REVISION.set(self.settings.current().revision + 1)
        self.settings.forget()
        self.assertEqual(self.settings.current().sign_in.method, "oidc")
        self.assertFalse(auth.may_sign_in("anyone@gmail.com"))
        self.vault.put(self.vault.OIDC_CLIENT_SECRET, "fixture-secret", by="test")
        self.assertEqual(sign_in.status().state, "attention")
        self.settings.update("sign_in", {**oidc, "allowed_emails": ["*@example.com"]}, by="test")
        self.assertEqual(sign_in.status().state, "connected")
        self.assertTrue(auth.may_sign_in("dev@example.com"))
        self.assertFalse(auth.may_sign_in("anyone@gmail.com"))

    def test_oidc_takes_only_a_verified_email(self):
        from unittest.mock import AsyncMock, MagicMock
        self.settings.update("sign_in", {"method": "oidc", "oidc_issuer": "https://id.example.com",
                                         "oidc_client_id": "fixture-client", "allowed_emails": ["*@example.com"]}, by="test")
        client = MagicMock()
        with patch("sbx_control.auth.oidc_client", return_value=client):
            for claims, status in (({"email": "admin@example.com", "email_verified": False}, 403),
                                   ({"email": "admin@example.com", "email_verified": "false"}, 403),
                                   ({"preferred_username": "admin@example.com"}, 403),
                                   ({"email": "dev@example.com", "email_verified": True}, 307)):
                client.authorize_access_token = AsyncMock(return_value={"userinfo": claims})
                self.client.get("/auth/logout")
                response = self.client.get("/auth/callback", follow_redirects=False)
                self.assertEqual(response.status_code, status, claims)

    def test_admin_password_boundary_is_eight_characters(self):
        self.choose_ip()
        for password in ("1234567", "x" * 1025):
            response = self.client.post("/api/setup/admin", headers=self.headers,
                                        json={"email": "operator@example.com", "password": password})
            self.assertEqual(response.status_code, 400)
            self.assertFalse(self.vault.facts(self.vault.LOGIN_PASSWORD).set)
        response = self.client.post("/api/setup/admin", headers=self.headers,
                                    json={"email": "operator@example.com", "password": "12345678"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["signed_in"])
        self.assertNotIn("12345678", response.text)

    def test_react_password_sign_in_preserves_destination_and_errors(self):
        self.choose_ip()
        self.create_admin()
        self.client.get("/auth/logout")
        response = self.client.get("/auth/login?next=/app/setup", follow_redirects=False)
        self.assertEqual(response.headers["location"], "/app/sign-in")
        headers = {"content-type": "application/x-www-form-urlencoded", "accept": "application/json",
                   "Origin": "https://203.0.113.10"}
        wrong = self.client.post("/auth/login", headers=headers,
                                 content="email=operator%40example.com&password=wrong")
        self.assertEqual(wrong.status_code, 401)
        self.assertIn("incorrect", wrong.json()["detail"])
        self.assertFalse(self.client.get("/api/setup", headers=self.headers).json()["signed_in"])
        correct = self.client.post("/auth/login", headers=headers,
                                   content="email=operator%40example.com&password=test-only-password-123")
        self.assertEqual(correct.status_code, 200)
        self.assertEqual(correct.json(), {"next": "/app/setup"})
        self.assertEqual(self.client.get("/api/setup", headers=self.headers).json()["email"], "operator@example.com")

    def test_sign_in_rejects_protocol_relative_redirect(self):
        self.choose_ip()
        self.create_admin()
        self.client.get("/auth/logout")
        self.client.get("/auth/login?next=//example.org")
        response = self.client.post("/auth/login", content="email=operator%40example.com&password=test-only-password-123",
                                    headers={"content-type": "application/x-www-form-urlencoded", "accept": "application/json",
                                             "Origin": "https://203.0.113.10"})
        self.assertEqual(response.json(), {"next": "/"})

    def test_domain_is_not_ready_until_certificate_exists(self):
        self.settings.update("domain", {"name": "sbx.example.com", "address": "203.0.113.10"}, by="test")
        self.assertFalse(self.setup.address_ready())
        self.assertEqual(self.client.post("/api/setup/admin", headers=self.headers,
                                          json={"email": "operator@example.com", "password": "test-only-password-123"}).status_code, 400)
        self.expiry.return_value = datetime.now(timezone.utc) + timedelta(days=90)
        self.assertTrue(self.setup.address_ready())
        view = self.client.get("/api/setup", headers=self.headers).json()
        self.assertEqual(view["link"], "https://login.sbx.example.com/setup")
        self.assertEqual(view["access"]["needs_ports"], [80, 53])  # delegated DNS answers here, on 53
        self.assertEqual(view["host"]["address"], "203.0.113.10")
        self.expiry.return_value = datetime.now(timezone.utc) - timedelta(days=1)
        self.assertFalse(self.setup.address_ready())

    def test_bad_dns_does_not_store_provider_credentials(self):
        from sbx_control.connections.domain import Domain
        from sbx_control.connections import Check, Refused
        with patch.object(Domain, "test", return_value=[Check("*.sbx.example.com resolves", False)]):
            with self.assertRaises(Refused):
                Domain().connect({"name": "sbx.example.com", "acme": "cloudflare",
                                  "cloudflare_api_token": "test-only-dns-token"}, by="test")
        self.assertFalse(self.vault.facts(self.vault.CLOUDFLARE_API_TOKEN).set)
        self.assertEqual(self.settings.current().domain.name, "")

    def test_setup_requires_token_and_foreign_origin_cannot_write(self):
        self.assertEqual(self.client.get("/api/setup").status_code, 401)
        self.assertEqual(self.client.post("/api/setup/address", json={"address": "203.0.113.10"}).status_code, 401)
        self.assertEqual(self.client.post("/api/setup/address", json={"address": "203.0.113.10"},
                                          headers={"Origin": "https://unrelated.example"}).status_code, 403)


class SettingsTests(Server):
    """Sections, an extension's own section, and the host effects a change asks for."""

    def test_an_extensions_section_is_stored_generically_with_its_own_effects(self):
        from sbx_control import extensions, settings
        claude = extensions.get("claude")
        self.effects()
        self.assertFalse(claude.settings().enabled)   # off until an operator connects it
        claude.save_settings({**claude.settings().model_dump(), "version": "9.9.9", "enabled": True}, by="test")
        self.assertEqual(claude.settings().version, "9.9.9")
        self.assertEqual(settings.current().extensions["claude"]["version"], "9.9.9")
        self.assertEqual(self.effects(), ["orchestrator"])
        lines = "\n".join(settings.shell_lines(settings.current(), {"claude": claude.settings()}))
        self.assertIn("claude_version=9.9.9", lines)
        self.assertIn("claude_enabled=1", lines)
        with self.assertRaises(settings.Invalid):
            settings.update("claude", {"version": "1"}, by="test")  # no model: not a section of settings'

    def test_a_secret_asks_for_the_effects_it_declares(self):
        from sbx_control import vault
        self.effects()
        vault.put(vault.CLOUDFLARE_API_TOKEN, "fixture-token", by="test")
        self.assertEqual(self.effects(), ["tls"])
        vault.put(vault.LOGIN_PASSWORD, "fixture-password", by="test")
        self.assertEqual(self.effects(), [])

    def test_the_domains_effects_depend_on_what_changed(self):
        from sbx_control import settings
        self.effects()
        settings.update("domain", {"address": "203.0.113.10"}, by="test")
        self.assertEqual(self.effects(), ["host-guard", "tls"])  # 80 opens for the address's certificate
        settings.update("domain", {"address": "203.0.113.10", "acme_email": "ops@example.com"}, by="test")
        self.assertEqual(self.effects(), ["tls"])
        settings.update("domain", {"address": "203.0.113.10", "name": "sbx.example.com"}, by="test")
        self.assertEqual(self.effects(), ["control", "host-guard", "runtime-env", "tls"])  # 53 for delegated DNS-01
        self.assertEqual(settings.opened_ports(settings.current()), (22, 443, 80, 53))

    def test_a_section_is_written_whole_or_not_at_all_and_its_history_kept(self):
        from sbx_control import settings
        before = settings.current().revision
        with self.assertRaises(settings.Invalid):
            settings.update("network", {"internal_cidrs": ["not a range"]}, by="test")
        self.assertEqual(settings.current().revision, before)
        with self.assertRaises(settings.Conflict):
            settings.update("access", {"admins": ["a@example.com"]}, by="test", expect_revision=before - 1)
        settings.update("access", {"admins": ["a@example.com"]}, by="someone")
        self.assertEqual(settings.history(1)[0]["by"], "someone")


if __name__ == "__main__":
    unittest.main()
