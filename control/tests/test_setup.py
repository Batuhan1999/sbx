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

    def test_ip_setup_signs_in_without_repo_connections(self):
        self.assertEqual(self.client.get("/api/setup").status_code, 401)   # nothing but the account is open
        self.assertEqual(self.settings.current().sign_in.method, "password")
        self.assertEqual(self.settings.current().domain.acme, "delegated")
        admin = self.create_admin()                                         # the page's first step
        self.assertEqual([s["step"] for s in admin["steps"]], ["address", "sign_in", "runtime", "agents"])
        self.assertTrue(admin["signed_in"])
        self.assertFalse(admin["can_complete"])                             # no address yet
        chosen = self.choose_ip()
        self.assertEqual(chosen["access"]["url"], "https://203.0.113.10")
        self.assertTrue(chosen["can_complete"])   # choosing coding agents is optional
        self.assertIn(80, self.settings.opened_ports(self.settings.current()))
        self.assertNotIn(80, self.settings.current().network.public_ports)
        effects = self.effects()
        self.assertIn(self.settings.HostUnit.TLS, effects)
        self.assertIn(self.settings.HostUnit.HOST_GUARD, effects)
        self.assertTrue(self.client.get("/api/setup").json()["signed_in"])
        done = self.client.post("/api/setup/complete")
        self.assertEqual(done.status_code, 200, done.text)
        self.assertTrue(done.json()["complete"])
        me = self.client.get("/api/me").json()
        self.assertTrue(me["admin"])
        self.assertEqual(me["email"], "operator@example.com")
        self.assertEqual(me["base_url"], "https://203.0.113.10")
        self.assertEqual(me["repos"], [])
        from fastapi.testclient import TestClient
        with TestClient(self.app.app, base_url="https://203.0.113.10") as outsider:
            self.assertEqual(outsider.get("/api/setup").status_code, 401)
            self.assertEqual(outsider.get("/api/me").status_code, 401)
        self.client.get("/logout")
        login = self.client.post("/login", content="email=operator%40example.com&password=test-only-password-123",
                                 headers={"content-type": "application/x-www-form-urlencoded",
                                          "Origin": "https://203.0.113.10"}, follow_redirects=False)
        self.assertEqual(login.status_code, 303, login.text)
        self.assertEqual(self.client.get("/api/me").json()["email"], "operator@example.com")

    def test_finishing_does_not_wait_for_the_checks_which_still_say_what_fails(self):
        from sbx_control.connections import Check
        self.create_admin()
        self.choose_ip()
        self.choose_agents()
        self.runtime.return_value = [Check("Embed API", False, "Offline", "Restart the runtime.")]
        checks = self.client.get("/api/setup/checks").json()
        self.assertFalse(checks["ok"])
        self.assertIn("Offline", str(checks["checks"]))
        self.runtime.reset_mock()
        self.assertTrue(self.client.post("/api/setup/complete").json()["complete"])
        self.runtime.assert_not_called()

    def test_ip_setup_waits_for_the_active_certificate_and_allows_retry(self):
        self.ip_expiry.return_value = None
        self.create_admin()
        initial = self.choose_ip()
        self.assertFalse(initial["access"]["certificate_ready"])
        self.assertFalse(initial["steps"][0]["done"])
        self.assertFalse(initial["can_complete"])
        refused = self.client.post("/api/setup/complete")
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.json()["detail"], "Choose the server address first.")
        with patch("sbx_control.connections.domain.ip_certificate_error", return_value="HTTP validation timed out"):
            view = self.client.get("/api/setup").json()
            self.assertIn("timed out", view["access"]["certificate_error"])
        self.ip_expiry.return_value = datetime.now(timezone.utc) + timedelta(hours=160)
        self.assertTrue(self.choose_ip()["access"]["certificate_ready"])
        from sbx_control import connections
        self.assertIn(f"valid until {self.ip_expiry.return_value:%Y-%m-%d}", connections.get("domain").status().summary)
        self.choose_agents()
        self.assertTrue(self.client.post("/api/setup/complete").json()["complete"])

    def test_the_onboarding_address_is_the_page_s_and_carries_no_credential(self):
        self.ip_expiry.return_value = None
        found = self.setup.onboarding()
        self.assertEqual(found, {"link": "https://203.0.113.10/onboarding", "trusted": False, "domain": ""})
        self.assertEqual(self.setup.onboarding(), found)   # an installer rerun prints the same
        self.assertEqual(self.client.get("/api/setup/open").json()["link"], found["link"])
        self.ip_expiry.return_value = datetime.now(timezone.utc) + timedelta(hours=160)
        self.create_admin()
        self.choose_ip()
        self.assertEqual(self.setup.onboarding(), {**found, "trusted": True})   # the same page once someone signed in
        self.choose_agents()
        self.assertTrue(self.client.post("/api/setup/complete").json()["complete"])
        with self.assertRaises(RuntimeError):   # a set-up server prints no onboarding address
            self.setup.onboarding()

    def test_the_installer_reads_the_onboarding_address_as_shell_lines(self):
        import contextlib
        import io
        def said():
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = self.setup.main(["onboarding"])   # install-report.sh evals what this prints
            return code, out.getvalue().splitlines()
        self.assertEqual(said(), (0, ["link=https://203.0.113.10/onboarding", "trusted=", "domain=''"]))
        self.settings.update("domain", {"address": "203.0.113.10"}, by="test")   # its IP certificate is there
        self.assertEqual(said(), (0, ["link=https://203.0.113.10/onboarding", "trusted=1", "domain=''"]))
        self.setup.mark_complete("test")
        self.assertEqual(said(), (3, []))

    def first_certificate(self, *, issued=True, error="", public=True, port_free=True):
        """setup.first_certificate with the host's tls effect played by its sleep: it takes the request
        and leaves the certificate (or Certbot's error)."""
        def apply(_seconds):
            self.effects()
            if issued:
                self.ip_expiry.return_value = datetime.now(timezone.utc) + timedelta(hours=160)
        self.ip_expiry.return_value = None
        with patch.object(self.setup, "_public", return_value=public), \
                patch.object(self.setup, "_port_free", return_value=port_free), \
                patch("sbx_control.connections.domain.ip_certificate_error", return_value=error):
            return self.setup.first_certificate(sleep=apply)

    def test_the_installer_gets_the_ip_certificate_before_the_first_visit(self):
        trusted, said = self.first_certificate()
        self.assertTrue(trusted, said)
        self.assertEqual(self.settings.current().domain.address, "203.0.113.10")
        self.assertIn(80, self.settings.opened_ports(self.settings.current()))
        self.assertEqual(self.setup.onboarding(), {"link": "https://203.0.113.10/onboarding", "trusted": True,
                                                   "domain": ""})
        self.assertTrue(self.create_admin()["steps"][0]["done"])   # the address is there before the account

    def test_without_the_certificate_the_setup_keeps_the_address_step_and_certbots_error(self):
        trusted, said = self.first_certificate(issued=False, error="Certbot failed\nTimeout during connect")
        self.assertFalse(trusted)
        self.assertIn("Timeout during connect", said)
        self.assertEqual(self.settings.current().domain.address, "203.0.113.10")   # the renewal keeps trying
        self.assertFalse(self.setup.onboarding()["trusted"])

    def test_no_public_address_or_a_busy_port_80_changes_nothing(self):
        self.effects()   # the installation's own, before
        for kwargs, words in (({"public": False}, "no public IP"), ({"port_free": False}, "port 80 is in use")):
            with self.subTest(words=words):
                trusted, said = self.first_certificate(**kwargs)
                self.assertFalse(trusted)
                self.assertIn(words, said)
                self.assertEqual(self.settings.current().domain.address, "")
                self.assertEqual(self.effects(), [])

    def test_the_installer_stops_waiting_for_the_certificate_at_its_timeout(self):
        now = [0.0]
        self.ip_expiry.return_value = None
        with patch.object(self.setup, "_public", return_value=True), \
                patch.object(self.setup, "_port_free", return_value=True):
            trusted, said = self.setup.first_certificate(
                30, sleep=lambda seconds: now.__setitem__(0, now[0] + seconds), clock=lambda: now[0])
        self.assertFalse(trusted)
        self.assertIn("within 30 s", said)

    def test_a_domain_or_a_finished_setup_is_left_to_its_own_certificate(self):
        self.settings.update("domain", {**self.settings.current().domain.model_dump(), "name": "sbx.example.com"},
                             by="test")
        self.effects()
        trusted, said = self.first_certificate()
        self.assertIn("sbx.example.com gets its own certificate", said)
        self.assertEqual((self.settings.current().domain.address, self.effects()), ("", []))

    def test_validation_and_existing_admin_cannot_be_overwritten(self):
        from sbx_control import users
        body = {"email": "operator@example.com", "password": "test-only-password-123"}
        for invalid in ({**body, "email": "invalid"}, {**body, "password": "short"}):
            response = self.client.post("/api/setup/admin", json=invalid)
            self.assertEqual(response.status_code, 400)
            self.assertFalse(users.any_password())
        self.assertEqual(self.client.get("/api/setup").status_code, 401)   # a refused account signs nobody in
        self.create_admin()
        self.assertEqual(self.client.post("/api/setup/address", json={"address": "example.com/path"}).status_code, 400)
        self.assertEqual(self.client.post("/api/setup/admin", json=body).status_code, 409)
        self.assertEqual(self.settings.current().access.admins, ("operator@example.com",))

    def password_login(self, email):
        self.client.get("/logout")
        return self.client.post("/login", content=f"email={email}&password=test-only-password-123",
                                headers={"content-type": "application/x-www-form-urlencoded", "accept": "application/json",
                                         "Origin": "https://203.0.113.10"}).status_code

    def test_the_sign_in_cookie_is_named_per_installation_and_leaves_an_app_s_sbx_session_alone(self):
        from sbx_control import platform
        # The session middleware reads the key once per process; this test's installation has its own.
        self.app.app.middleware_stack = None
        self.create_admin()
        self.choose_ip()
        self.choose_agents()
        done = self.client.post("/api/setup/complete")
        self.assertEqual(done.status_code, 200, done.text)
        self.assertTrue(done.json()["complete"])
        self.assertEqual(self.password_login("operator%40example.com"), 200)
        name = platform.session_cookie()
        self.assertRegex(name, r"^sbx_session_[0-9a-f]{12}$")
        self.assertIn(name, self.client.cookies)
        self.assertNotIn("sbx_session", self.client.cookies)
        # An app's own sbx_session on the same host (sbx as a repository on sbx) is not ours.
        self.client.cookies.set("sbx_session", "an-app-session", domain="203.0.113.10")
        self.assertEqual(self.client.get("/api/me").json()["email"], "operator@example.com")
        # Another installation (another session key) names its cookie differently.
        with patch("sbx_control.state.session_key", return_value="another installation's key"):
            self.assertNotEqual(platform.session_cookie(), name)

    def test_admins_the_installer_named_get_a_login_through_a_link(self):
        from sbx_control import users
        self.settings.update("access", {"admins": ["named@example.com"]}, by="install.sh")
        self.assertFalse(self.client.get("/api/setup/open").json()["account"])   # named, but nobody can sign in
        response = self.client.post("/api/setup/admin", json={
            "email": "Operator@example.com", "password": "test-only-password-123"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.settings.current().access.admins, ("named@example.com", "operator@example.com"))
        self.choose_ip()
        self.choose_agents()
        self.assertTrue(self.client.post("/api/setup/complete").json()["complete"])
        self.assertEqual(self.password_login("named%40example.com"), 401)  # no password of their own yet
        users.use_link(users.mint_link("named@example.com", by="test"), "test-only-password-123")
        self.assertEqual(self.password_login("named%40example.com"), 200)

    def set_password(self, link, password="colleague-password-1"):
        token = link.split("#t=", 1)[1]
        return self.client.post("/auth/set-password", json={"token": token, "password": password},
                                headers={"Origin": "https://203.0.113.10"})

    def test_each_person_has_their_own_password(self):
        self.repo_admin()
        origin = {"Origin": "https://203.0.113.10"}
        added = self.client.post("/api/users", json={"email": "Colleague@example.com"}, headers=origin)
        self.assertEqual(added.status_code, 200, added.text)
        self.assertEqual({u["email"]: u["admin"] for u in added.json()["users"]},
                         {"operator@example.com": True, "colleague@example.com": False})
        link = added.json()["link"]
        self.assertEqual(self.client.post("/api/users", json={"email": "colleague@example.com"},
                                          headers=origin).status_code, 400)
        # The admin's password is not theirs, and they have none until they use the link.
        self.assertEqual(self.password_login("colleague%40example.com"), 401)
        self.assertEqual(self.set_password(link, "short").status_code, 400)
        self.assertEqual(self.set_password(link).status_code, 200)
        self.assertEqual(self.client.get("/api/me").json()["email"], "colleague@example.com")
        self.assertFalse(self.client.get("/api/me").json()["admin"])
        self.assertEqual(self.client.get("/api/users").status_code, 403)
        self.assertEqual(self.set_password(link).status_code, 400)  # once
        # Changing one's password ends the other sessions.
        from fastapi.testclient import TestClient
        with TestClient(self.app.app, base_url="https://203.0.113.10") as laptop:
            laptop.post("/login", content="email=colleague%40example.com&password=colleague-password-1",
                        headers={"content-type": "application/x-www-form-urlencoded", **origin})
            self.assertEqual(laptop.get("/api/me").status_code, 200)
            changed = self.client.post("/api/me/password", headers=origin,
                                       json={"current": "colleague-password-1", "password": "colleague-password-2"})
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertEqual(self.client.get("/api/me").status_code, 200)   # this one stays
            self.assertEqual(laptop.get("/api/me").status_code, 401)
        # Removing them ends their session and their API tokens.
        from sbx_control import auth
        token = auth.mint_token("colleague@example.com", "cli")
        self.client.get("/logout")
        self.assertEqual(self.password_login("operator%40example.com"), 200)
        removed = self.client.delete("/api/users/colleague@example.com", headers=origin)
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertIsNone(auth.token_email(token))
        self.assertEqual(self.client.delete("/api/users/operator@example.com", headers=origin).status_code, 400)
        self.assertEqual(self.client.put("/api/users/operator@example.com", json={"admin": False},
                                         headers=origin).status_code, 400)  # the last admin

    def test_promoting_someone_makes_them_an_admin(self):
        self.repo_admin()
        origin = {"Origin": "https://203.0.113.10"}
        link = self.client.post("/api/users", json={"email": "lead@example.com"}, headers=origin).json()["link"]
        self.assertEqual(self.client.put("/api/users/lead@example.com", json={"admin": True},
                                         headers=origin).status_code, 200)
        self.assertIn("lead@example.com", self.settings.current().admins)
        newer = self.client.post("/api/users/lead@example.com/link", headers=origin).json()["link"]
        self.assertEqual(self.set_password(link).status_code, 400)  # the newer link replaced it
        self.assertEqual(self.set_password(newer).status_code, 200)
        self.assertTrue(self.client.get("/api/me").json()["admin"])

    def test_both_first_installs_set_the_same_password_sign_in(self):
        import contextlib
        import io
        from sbx_control import admin, users
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(admin.main(["sign-in", "password"]), 1)  # no admin: nobody could sign in
        self.assertFalse(users.any_password())
        self.settings.update("access", {"admins": ["operator@example.com"]}, by="install.sh")
        with contextlib.redirect_stdout(out):
            self.assertEqual(admin.main(["sign-in", "password"]), 0)
        generated = out.getvalue().split("generated password for operator@example.com: ", 1)[1].strip()
        self.assertIsNotNone(users.check_password("operator@example.com", generated))
        installed = self.settings.current().sign_in
        self.assertEqual(installed.method, "password")
        self.setup.reset(sign_in=True)
        self.assertFalse(users.any_password())
        self.settings.update("access", {"admins": ["named@example.com"]}, by="install.sh")
        self.create_admin()
        self.assertEqual(self.settings.current().sign_in, installed)

    def test_sbx_admin_manages_people(self):
        import contextlib
        import io
        from sbx_control import admin, users
        self.settings.update("access", {"admins": ["operator@example.com"]}, by="install.sh")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(admin.main(["user", "add", "dev@example.com"]), 0)
            self.assertEqual(admin.main(["user", "admin", "dev@example.com", "on"]), 0)
            self.assertEqual(admin.main(["user", "rm", "operator@example.com"]), 1)   # dev@ has no password yet
            users.set_password("dev@example.com", "test-only-password-123", by="test")
            self.assertEqual(admin.main(["user", "rm", "operator@example.com"]), 0)
            self.assertEqual(admin.main(["user", "rm", "dev@example.com"]), 1)   # the last admin
            self.assertEqual(admin.main(["user", "link", "nobody@example.com"]), 1)
            self.assertEqual(admin.main(["user", "ls"]), 0)
        self.assertIn("/app/set-password#t=", out.getvalue())
        self.assertEqual(self.settings.current().access.admins, ("dev@example.com",))
        self.assertEqual([u["email"] for u in users.ls()], ["dev@example.com"])

    def test_oidc_admits_the_listed_the_admins_and_the_people_added(self):
        from sbx_control import auth, connections, users
        sign_in = connections.get("sign_in")
        oidc = {"method": "oidc", "oidc_issuer": "https://accounts.google.com", "oidc_client_id": "fixture-client"}
        self.settings.update("access", {"admins": ["operator@example.com"]}, by="test")
        self.settings.update("sign_in", oidc, by="test")
        self.assertFalse(auth.may_sign_in("anyone@gmail.com"))
        self.assertTrue(auth.may_sign_in("operator@example.com"))
        users.add("guest@gmail.com", by="test")
        self.assertTrue(auth.may_sign_in("guest@gmail.com"))
        self.vault.put(self.vault.OIDC_CLIENT_SECRET, "fixture-secret", by="test")
        self.settings.update("sign_in", {**oidc, "allowed_emails": ["*@example.com"]}, by="test")
        self.assertEqual(sign_in.status().state, "connected")
        self.assertTrue(auth.may_sign_in("dev@example.com"))
        self.assertFalse(auth.may_sign_in("anyone@gmail.com"))

    def test_a_link_works_once_and_only_until_it_expires(self):
        from sbx_control import state, users
        token = users.mint_link("dev@example.com", by="test")
        with state.db() as conn:
            conn.execute("update user_links set expires_at = 0")
        with self.assertRaisesRegex(users.Refused, "expired"):
            users.use_link(token, "test-only-password-123")
        token = users.mint_link("dev@example.com", by="test")
        with self.assertRaisesRegex(users.Refused, "between"):
            users.use_link(token, "short")       # a refused password leaves the link usable
        users.use_link(token, "test-only-password-123")
        with self.assertRaisesRegex(users.Refused, "used"):
            users.use_link(token, "test-only-password-456")

    def test_passwords_are_refused_until_an_admin_has_one(self):
        from sbx_control import connections, users
        from sbx_control.connections import Refused
        self.settings.update("access", {"admins": ["operator@example.com"]}, by="test")
        self.settings.update("sign_in", {"method": "oidc", "oidc_issuer": "https://id.example.com",
                                         "oidc_client_id": "fixture-client"}, by="test")
        users.set_password("member@example.com", "test-only-password-123", by="test")
        with self.assertRaisesRegex(Refused, "check failed"):
            connections.get("sign_in").connect({"method": "password"}, by="test")
        self.assertEqual(self.settings.current().sign_in.method, "oidc")
        users.set_password("operator@example.com", "test-only-password-123", by="test")
        connections.get("sign_in").connect({"method": "password"}, by="test")
        self.assertEqual(self.settings.current().sign_in.method, "password")

    def test_removing_an_oidc_person_ends_their_session(self):
        from unittest.mock import AsyncMock, MagicMock
        from sbx_control import users
        self.repo_admin()
        self.settings.update("sign_in", {"method": "oidc", "oidc_issuer": "https://id.example.com",
                                         "oidc_client_id": "fixture-client"}, by="test")
        users.add("guest@gmail.com", by="test")
        client = MagicMock()
        client.authorize_access_token = AsyncMock(return_value={"userinfo": {"email": "guest@gmail.com", "email_verified": True}})
        with patch("sbx_control.auth.oidc_client", return_value=client):
            self.assertEqual(self.client.get("/auth/callback", follow_redirects=False).status_code, 307)
        self.assertEqual(self.client.get("/api/me").json()["email"], "guest@gmail.com")
        users.remove("guest@gmail.com", by="test")
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_a_password_change_loses_to_a_removal_in_between(self):
        from sbx_control import users
        self.settings.update("access", {"admins": ["operator@example.com"]}, by="test")
        users.set_password("dev@example.com", "test-only-password-123", by="test")
        checked = users.check_password("dev@example.com", "test-only-password-123")
        users.remove("dev@example.com", by="test")
        self.assertIsNone(users.change_password("dev@example.com", checked, "test-only-password-456"))
        self.assertIsNone(users.get("dev@example.com"))   # not made again

    def test_coming_back_does_not_revive_an_old_session(self):
        from sbx_control import users
        first = users.set_password("dev@example.com", "test-only-password-123", by="test")
        users.remove("dev@example.com", by="test")
        again = users.set_password("dev@example.com", "test-only-password-123", by="test")
        self.assertNotEqual(first, again)

    def test_the_last_admin_with_a_password_stays_one(self):
        from sbx_control import users
        self.settings.update("access", {"admins": ["operator@example.com", "named@example.com"]}, by="test")
        self.settings.update("sign_in", {"method": "password"}, by="test")
        users.set_password("operator@example.com", "test-only-password-123", by="test")
        with self.assertRaisesRegex(users.Refused, "password"):
            users.set_admin("operator@example.com", False, by="test")   # named@ has no password
        with self.assertRaisesRegex(users.Refused, "password"):
            users.remove("operator@example.com", by="test")
        users.set_admin("named@example.com", False, by="test")

    def test_resetting_sign_in_revokes_every_api_token(self):
        from sbx_control import auth
        token = auth.mint_token("operator@example.com", "cli")
        self.assertEqual(auth.token_email(token), "operator@example.com")
        self.setup.reset(sign_in=True)
        self.assertIsNone(auth.token_email(token))

    def test_oidc_needs_someone_who_may_sign_in(self):
        from sbx_control import auth, users
        from sbx_control.connections import sign_in as sign_in_connection
        oidc = {"method": "oidc", "oidc_issuer": "https://id.example.com", "oidc_client_id": "fixture-client"}
        self.vault.put(self.vault.OIDC_CLIENT_SECRET, "fixture-secret", by="test")
        self.settings.update("sign_in", oidc, by="test")
        self.assertFalse(sign_in_connection.ready())          # no admin, person or pattern
        self.settings.update("sign_in", {**oidc, "allowed_emails": ["*@example.com"]}, by="test")
        self.assertTrue(sign_in_connection.ready())
        # Someone a pattern let in is not made a person: dropping the pattern ends their access.
        users.signed_in("dev@example.com")
        self.assertIsNone(users.get("dev@example.com"))
        self.settings.update("sign_in", oidc, by="test")
        self.assertFalse(auth.may_sign_in("dev@example.com"))

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
                self.client.get("/logout")
                response = self.client.get("/auth/callback", follow_redirects=False)
                self.assertEqual(response.status_code, status, claims)

    def test_admin_password_boundary_is_eight_characters(self):
        from sbx_control import users
        for password in ("1234567", "x" * 1025):
            response = self.client.post("/api/setup/admin",
                                        json={"email": "operator@example.com", "password": password})
            self.assertEqual(response.status_code, 400)
            self.assertFalse(users.any_password())
        response = self.client.post("/api/setup/admin",
                                    json={"email": "operator@example.com", "password": "12345678"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["signed_in"])
        self.assertNotIn("12345678", response.text)

    def test_react_password_sign_in_preserves_destination_and_errors(self):
        self.create_admin()
        self.client.get("/logout")
        response = self.client.get("/login?next=/onboarding", follow_redirects=False)
        self.assertNotIn("location", response.headers)   # the web app's sign-in page, at /login itself
        self.assertIn("content-security-policy", response.headers)
        headers = {"content-type": "application/x-www-form-urlencoded", "accept": "application/json",
                   "Origin": "https://203.0.113.10"}
        wrong = self.client.post("/login", headers=headers,
                                 content="email=operator%40example.com&password=wrong")
        self.assertEqual(wrong.status_code, 401)
        self.assertIn("incorrect", wrong.json()["detail"])
        self.assertFalse(self.client.get("/api/setup/open").json()["signed_in"])
        self.assertEqual(self.client.get("/api/setup").status_code, 401)
        correct = self.client.post("/login", headers=headers,
                                   content="email=operator%40example.com&password=test-only-password-123")
        self.assertEqual(correct.status_code, 200)
        self.assertEqual(correct.json(), {"next": "/onboarding"})   # back to the page, to go on where it was
        self.assertEqual(self.client.get("/api/setup").json()["email"], "operator@example.com")

    def test_sign_in_rejects_protocol_relative_redirect(self):
        self.create_admin()
        self.client.get("/logout")
        self.client.get("/login?next=//example.org")
        response = self.client.post("/login", content="email=operator%40example.com&password=test-only-password-123",
                                    headers={"content-type": "application/x-www-form-urlencoded", "accept": "application/json",
                                             "Origin": "https://203.0.113.10"})
        self.assertEqual(response.json(), {"next": "/"})

    def test_domain_is_not_ready_until_certificate_exists(self):
        self.create_admin()   # the account comes first, whatever the address's state
        self.settings.update("domain", {"name": "sbx.example.com", "address": "203.0.113.10"}, by="test")
        self.assertFalse(self.setup.address_ready())
        view = self.client.get("/api/setup").json()
        self.assertEqual(view["link"], "https://203.0.113.10/onboarding")   # at the IP address until then
        refused = self.client.post("/api/setup/complete")
        self.assertEqual((refused.status_code, refused.json()["detail"]), (400, "Choose the server address first."))
        self.expiry.return_value = datetime.now(timezone.utc) + timedelta(days=90)
        self.assertTrue(self.setup.address_ready())
        view = self.client.get("/api/setup").json()
        self.assertEqual(view["link"], "https://app.sbx.example.com/onboarding")
        self.assertEqual(view["access"]["needs_ports"], [80, 53])  # delegated DNS answers here, on 53
        self.assertEqual(view["host"]["address"], "203.0.113.10")
        self.expiry.return_value = datetime.now(timezone.utc) - timedelta(days=1)
        self.assertFalse(self.setup.address_ready())

    def test_an_installation_that_saved_another_sign_in_host_moves_to_app(self):
        self.settings.update("domain", {"name": "sbx.example.com", "login_host": "login"}, by="test")
        self.assertEqual(self.settings.current().base_url, "https://app.sbx.example.com")

    def test_bad_dns_does_not_store_provider_credentials(self):
        from sbx_control.connections.domain import Domain
        from sbx_control.connections import Check, Refused
        with patch.object(Domain, "test", return_value=[Check("*.sbx.example.com resolves", False)]):
            with self.assertRaises(Refused):
                Domain().connect({"name": "sbx.example.com", "acme": "cloudflare",
                                  "cloudflare_api_token": "test-only-dns-token"}, by="test")
        self.assertFalse(self.vault.facts(self.vault.CLOUDFLARE_API_TOKEN).set)
        self.assertEqual(self.settings.current().domain.name, "")

    def test_nothing_but_the_first_account_is_open_before_signing_in(self):
        found = self.client.get("/api/setup/open")
        self.assertEqual(found.status_code, 200, found.text)
        self.assertEqual(found.json(), {"complete": False, "account": False, "signed_in": False,
                                        "link": "https://203.0.113.10/onboarding", "version": self.setup.version()})
        for method, url, body in (("GET", "/api/setup", None), ("GET", "/api/setup/checks", None),
                                  ("POST", "/api/setup/address", {"address": "203.0.113.10"}),
                                  ("PUT", "/api/setup/agents", {"agents": []}),
                                  ("POST", "/api/setup/complete", None), ("POST", "/api/setup/handoff", None),
                                  ("GET", "/api/connections", None),
                                  ("PUT", "/api/connections/domain", {"values": {"name": "sbx.example.com"}}),
                                  ("GET", "/api/runtimes", None), ("GET", "/api/runtimes/available", None),
                                  ("POST", "/api/runtimes", {"kind": "e2b-cloud", "values": {}}),
                                  ("GET", "/api/settings", None), ("GET", "/api/templates", None),
                                  ("GET", "/api/secret-requests", None)):
            with self.subTest(url=f"{method} {url}"):
                refused = self.client.request(method, url, json=body)
                self.assertEqual(refused.status_code, 401, refused.text)
        self.assertIn("the onboarding page's first step", self.client.get("/api/setup").json()["detail"])
        # The retired credential is nothing: not a way in, and no exemption from the cross-origin rule.
        retired = {"Authorization": "Setup anything"}
        self.assertEqual(self.client.get("/api/setup", headers=retired).status_code, 401)
        self.assertEqual(self.client.post("/api/setup/address", json={"address": "203.0.113.10"},
                                          headers={**retired, "Origin": "https://unrelated.example"}).status_code, 403)
        self.assertEqual(self.settings.current().domain.address, "")

    def test_the_first_account_needs_no_credential_and_no_address_and_is_made_once(self):
        from fastapi.testclient import TestClient
        self.assertEqual(self.settings.current().domain.address, "")
        self.assertFalse(self.client.cookies)
        made = self.client.post("/api/setup/admin", json={"email": "Operator@example.com",
                                                          "password": "test-only-password-123"})
        self.assertEqual(made.status_code, 200, made.text)
        self.assertNotIn("token", made.json())           # only a client without a browser asks for one
        self.assertEqual((made.json()["signed_in"], made.json()["email"]), (True, "operator@example.com"))
        self.assertEqual(self.settings.current().access.admins, ("operator@example.com",))
        self.assertEqual(self.client.get("/api/setup").json()["email"], "operator@example.com")   # the browser is signed in
        opened = self.client.get("/api/setup/open").json()
        self.assertEqual((opened["account"], opened["signed_in"], opened["complete"]), (True, True, False))
        with TestClient(self.app.app, base_url="https://203.0.113.10") as other:
            opened = other.get("/api/setup/open").json()
            self.assertEqual((opened["account"], opened["signed_in"]), (True, False))
            again = other.post("/api/setup/admin", json={"email": "other@example.com",
                                                         "password": "test-only-password-456"})
            self.assertEqual(again.status_code, 409)
            self.assertIn("already has an admin", again.json()["detail"])
            self.assertEqual(other.get("/api/setup").status_code, 401)
        self.assertEqual(self.settings.current().access.admins, ("operator@example.com",))

    def test_a_client_without_a_browser_gets_an_api_token_with_the_account(self):
        from fastapi.testclient import TestClient
        from sbx_control import auth
        made = self.client.post("/api/setup/admin", json={"email": "operator@example.com",
                                                          "password": "test-only-password-123", "token": True})
        self.assertEqual(made.status_code, 200, made.text)
        token = made.json()["token"]
        self.assertEqual(auth.token_email(token), "operator@example.com")
        bearer = {"Authorization": f"Bearer {token}"}
        with TestClient(self.app.app, base_url="https://203.0.113.10") as agent:   # no cookie: the token alone
            self.assertEqual(agent.get("/api/setup", headers=bearer).json()["email"], "operator@example.com")
            chosen = agent.post("/api/setup/address", headers=bearer, json={"address": "203.0.113.10"})
            self.assertEqual(chosen.status_code, 200, chosen.text)
            done = agent.post("/api/setup/complete", headers=bearer)
            self.assertEqual(done.status_code, 200, done.text)
            self.assertTrue(done.json()["complete"])
            self.assertEqual(agent.get("/api/setup").status_code, 401)

    def test_finishing_takes_no_body_and_names_the_step_that_is_missing(self):
        from fastapi.testclient import TestClient
        anonymous = TestClient(self.app.app, base_url="https://203.0.113.10")
        self.addCleanup(anonymous.close)
        self.assertEqual(anonymous.post("/api/setup/complete").status_code, 401)
        self.create_admin()
        with_password = anonymous.post("/api/setup/complete", json={
            "email": "operator@example.com", "password": "test-only-password-123"})
        self.assertEqual(with_password.status_code, 401)   # a password in the body signs nobody in
        refused = self.client.post("/api/setup/complete")
        self.assertEqual((refused.status_code, refused.json()["detail"]), (400, "Choose the server address first."))
        self.assertFalse(self.setup.complete())
        self.choose_ip()
        with patch("sbx_control.runtime.enabled", return_value=[]):
            refused = self.client.post("/api/setup/complete")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("Add a sandbox runtime first", refused.json()["detail"])
        done = self.client.post("/api/setup/complete")   # no body: whoever is signed in finishes
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual((done.json()["complete"], done.json()["email"]), (True, "operator@example.com"))
        self.assertEqual(self.setup.missing({"steps": [{"step": s, "done": False} for s in
                                                       ("address", "sign_in", "runtime", "agents")]}),
                         "Create the admin account first.")   # the page's order: the account first

    def domain_client(self):
        """A browser at the server's domain, after sbx-control restarted for it (the session cookie then covers
        the domain: auth.Sessions)."""
        from fastapi.testclient import TestClient
        self.settings.update("domain", {"name": "sbx.example.com", "address": "203.0.113.10"}, by="test")
        self.app.app.middleware_stack = None
        client = TestClient(self.app.app, base_url="https://app.sbx.example.com")
        self.addCleanup(client.close)
        return client

    def handoff(self) -> str:
        response = self.client.post("/api/setup/handoff", headers={"Origin": "https://203.0.113.10"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["code"]

    def enter(self, client, code):
        return client.post("/api/setup/enter", json={"code": code}, headers={"Origin": "https://app.sbx.example.com"})

    def test_the_move_to_the_domain_carries_the_sign_in_over_once(self):
        self.create_admin()                      # at the IP address
        there = self.domain_client()
        self.assertEqual(there.get("/api/setup").status_code, 401)   # the IP address's cookie does not reach it
        self.assertEqual(there.post("/api/setup/handoff").status_code, 401)   # nor does anyone get a code unasked
        code = self.handoff()
        self.assertNotIn(code, str(self.setup.HANDOFF.get()))                  # kept as a hash
        self.assertEqual(self.enter(there, "not-the-code").status_code, 403)   # a guess, which leaves the code be
        entered = self.enter(there, code)
        self.assertEqual(entered.status_code, 200, entered.text)
        self.assertEqual((entered.json()["signed_in"], entered.json()["complete"]), (True, False))
        self.assertEqual(there.get("/api/setup").json()["email"], "operator@example.com")
        again = self.domain_client()
        refused = self.enter(again, code)        # used
        self.assertEqual(refused.status_code, 403)
        self.assertIn("expired", refused.json()["detail"])
        self.assertEqual(again.get("/api/setup").status_code, 401)

    def test_a_newer_handoff_replaces_the_last_and_an_old_one_is_refused(self):
        from types import SimpleNamespace
        import time
        self.create_admin()
        there = self.domain_client()
        first, second = self.handoff(), self.handoff()
        self.assertEqual(self.enter(there, first).status_code, 403)    # replaced
        self.assertEqual(there.get("/api/setup").status_code, 401)
        self.assertEqual(self.enter(there, second).status_code, 200)
        there.get("/logout")
        old = self.handoff()
        later = time.time() + self.setup.HANDOFF_SECONDS + 1
        with patch.object(self.setup, "time", SimpleNamespace(time=lambda: later)):
            self.assertEqual(self.enter(there, old).status_code, 403)
        self.assertEqual(self.enter(there, old).status_code, 403)      # and it is gone, not waiting for the clock
        self.assertEqual(there.get("/api/setup").status_code, 401)
        just = time.time() + self.setup.HANDOFF_SECONDS - 1
        fresh = self.handoff()
        with patch.object(self.setup, "time", SimpleNamespace(time=lambda: just)):
            self.assertEqual(self.enter(there, fresh).status_code, 200)

    def test_a_handoff_signs_in_only_someone_who_is_still_an_admin(self):
        self.create_admin()
        there = self.domain_client()
        code = self.handoff()
        self.settings.update("access", {"admins": ["other@example.com"]}, by="test")
        self.assertEqual(self.enter(there, code).status_code, 403)
        self.assertEqual(there.get("/api/me").status_code, 401)

    def test_finishing_ends_the_handoff(self):
        self.create_admin()
        self.choose_ip()
        code = self.handoff()
        self.assertEqual(self.client.post("/api/setup/complete").status_code, 200)
        self.assertEqual(self.setup.enter(code), "")

    def page(self):
        """The web app's page, as a release has it (this checkout may not have built it)."""
        from sbx_control import spa
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (folder / "index.html").write_text("<!doctype html><html><head><title>sbx</title></head><body></body></html>")
        self.enterContext(patch.object(spa, "DIST", folder))

    def test_the_server_opens_on_the_onboarding_until_it_is_done_and_never_after(self):
        self.page()
        for path in ("/", "/app", "/app/"):
            with self.subTest(path=path):
                response = self.client.get(path, follow_redirects=False)
                self.assertEqual((response.status_code, response.headers["location"]), (307, "/onboarding"))
        page = self.client.get("/onboarding", follow_redirects=False)   # open to anyone: its first step is the account
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("<title>sbx</title>", page.text)
        old = self.client.get("/setup", follow_redirects=False)         # the retired link's path
        self.assertEqual(old.status_code, 503)
        self.assertNotIn("location", old.headers)
        self.assertIn("Finish at /onboarding", old.text)
        gated = self.client.get("/api/sandboxes")                         # nothing but the onboarding
        self.assertEqual((gated.status_code, gated.json()), (503, {"detail": "This installation is being set up. Finish at /onboarding."}))
        self.repo_admin()
        for path, target in (("/onboarding", "/app/repos"), ("/", "/app/repos")):
            with self.subTest(after=path):
                response = self.client.get(path, follow_redirects=False)
                self.assertEqual((response.status_code, response.headers["location"]), (307, target))
        old = self.client.get("/setup", follow_redirects=False)
        self.assertEqual(old.status_code, 404)
        self.assertNotIn("location", old.headers)

    def test_the_onboarding_page_may_ask_its_new_address_whether_it_answers(self):
        # The page loads at the IP address before any domain is chosen, and its policy is fixed then: it must
        # already allow asking the address it will move to. Once the onboarding is complete, only this server.
        self.page()
        policy = self.client.get("/onboarding").headers["content-security-policy"]
        self.assertIn("connect-src 'self' https:;", policy)
        self.assertIn("script-src 'self';", policy)
        from sbx_control import setup
        setup.mark_complete("test")
        policy = self.client.get("/app/repos").headers["content-security-policy"]
        self.assertIn("connect-src 'self';", policy)

    def test_the_page_still_at_the_ip_address_writes_there_until_the_onboarding_is_done(self):
        self.create_admin()
        ip, foreign = {"Origin": "https://203.0.113.10"}, {"Origin": "https://preview.elsewhere.example"}
        self.settings.update("domain", {"name": "sbx.example.com", "address": "203.0.113.10"}, by="test")
        self.expiry.return_value = datetime.now(timezone.utc) + timedelta(days=90)

        def write(headers):
            return self.client.put("/api/setup/agents", json={"agents": []}, headers=headers)
        passed = write(ip)                       # the web app's own origin is the domain's now
        self.assertEqual(passed.status_code, 200, passed.text)
        self.assertEqual(self.client.post("/api/setup/handoff", headers=ip).status_code, 200)
        refused = write(foreign)
        self.assertEqual(refused.status_code, 403)
        self.assertIn("requests from https://preview.elsewhere.example are not accepted here", refused.text)
        self.assertEqual(write({"Origin": "https://app.sbx.example.com"}).status_code, 200)   # the web app's own
        done = self.client.post("/api/setup/complete", headers=ip)
        self.assertEqual(done.status_code, 200, done.text)
        refused = write(ip)                      # set up: the web app's own origin only
        self.assertEqual(refused.status_code, 403)
        self.assertIn("requests from https://203.0.113.10 are not accepted here", refused.text)

    def test_a_host_name_is_never_the_ip_address_s_exception(self):
        from fastapi.testclient import TestClient
        self.create_admin()
        self.settings.update("domain", {"name": "sbx.example.com", "address": "203.0.113.10"}, by="test")
        with TestClient(self.app.app, base_url="https://preview.sbx.example.com") as preview:
            refused = preview.put("/api/setup/agents", json={"agents": []},
                                  headers={"Origin": "https://preview.sbx.example.com"})
        self.assertEqual(refused.status_code, 403)

    def admin(self, *argv):
        import contextlib
        import io
        from sbx_control import admin
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = admin.main(list(argv))
        return code, out.getvalue().strip()

    def test_setup_reset_opens_the_onboarding_again_and_prints_its_address(self):
        from sbx_control import users
        self.repo_admin()
        self.assertEqual(self.admin("setup-reset"), (0, "https://203.0.113.10/onboarding"))
        self.assertFalse(self.setup.complete())
        self.assertEqual(self.client.get("/", follow_redirects=False).headers["location"], "/onboarding")
        opened = self.client.get("/api/setup/open").json()
        self.assertEqual((opened["account"], opened["signed_in"]), (True, True))   # the account stays
        self.assertEqual(self.client.post("/api/setup/admin", json={
            "email": "other@example.com", "password": "test-only-password-456"}).status_code, 409)
        self.assertEqual(self.admin("setup-reset", "--sign-in"), (0, "https://203.0.113.10/onboarding"))
        self.assertFalse(users.any_password())
        self.assertFalse(self.client.get("/api/setup/open").json()["account"])   # the first step is open again
        self.create_admin()

    def test_setup_link_is_no_longer_a_command(self):
        import contextlib
        import io
        from sbx_control import admin
        said = io.StringIO()
        with contextlib.redirect_stderr(said), self.assertRaises(SystemExit) as stopped:
            admin.main(["setup-link"])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("invalid choice: 'setup-link'", said.getvalue())
        self.assertFalse((self.paths.etc() / "setup-token").exists())
        for gone in ("mint_token", "current_token", "revoke_token", "token_ok", "token_file", "TOKEN_HASH"):
            self.assertFalse(hasattr(self.setup, gone), gone)

    def test_a_configured_installation_counts_as_set_up(self):
        self.assertFalse(self.setup.infer_complete())          # nothing yet
        self.create_admin()
        self.assertFalse(self.setup.infer_complete())          # an account, no address
        self.choose_ip()
        self.assertTrue(self.setup.configured())
        self.assertTrue(self.setup.infer_complete())           # an address, a way to sign in, a runtime
        self.assertTrue(self.setup.complete())
        self.assertFalse(self.setup.infer_complete())          # marked once


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
        vault.put(vault.OIDC_CLIENT_SECRET, "fixture-secret", by="test")
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
