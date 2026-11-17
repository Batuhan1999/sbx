"""Upgrading from the one shared sign-in password to a password per person."""
from support import Server


class SharedPasswordUpgrade(Server):
    def setUp(self):
        super().setUp()
        from sbx_control import settings
        settings.update("access", {"admins": ["first@example.com", "second@example.com"]}, by="test")
        settings.update("sign_in", {"method": "password",
                                    "allowed_emails": ["member@example.com", "*@example.org"]}, by="test")

    def shared(self, value="old-shared-password"):
        from sbx_control import secret_store, state
        reference = secret_store.seal(value, key="platform/LOGIN_PASSWORD")
        with state.db() as conn:
            conn.execute("insert into sealed (key, value, updated_by) values ('LOGIN_PASSWORD', ?, 'test')", (reference,))

    def remaining(self) -> bool:
        from sbx_control import state
        with state.db() as conn:
            return bool(conn.execute("select 1 from sealed where key = 'LOGIN_PASSWORD'").fetchone())

    def test_everyone_who_used_it_keeps_it(self):
        from sbx_control import state, users
        users.set_password("second@example.com", "second-own-password", by="test")
        self.shared()
        state.migrate()
        self.assertIsNotNone(users.check_password("first@example.com", "old-shared-password"))
        self.assertIsNotNone(users.check_password("member@example.com", "old-shared-password"))
        self.assertIsNotNone(users.check_password("second@example.com", "second-own-password"))
        self.assertIsNone(users.check_password("second@example.com", "old-shared-password"))
        self.assertEqual({u["email"] for u in users.ls()},
                         {"first@example.com", "second@example.com", "member@example.com"})
        self.assertFalse(self.remaining())
        state.migrate()   # the next start finds nothing to do
        self.assertIsNotNone(users.check_password("first@example.com", "old-shared-password"))

    def test_signs_in_through_the_api_after_the_upgrade(self):
        from sbx_control import state
        self.shared()
        state.migrate()
        login = self.client.post("/auth/login", content="email=first%40example.com&password=old-shared-password",
                                 headers={"content-type": "application/x-www-form-urlencoded",
                                          "accept": "application/json", "Origin": "https://203.0.113.10"})
        self.assertEqual(login.status_code, 200, login.text)
        wrong = self.client.post("/auth/login", content="email=first%40example.com&password=not-the-password",
                                 headers={"content-type": "application/x-www-form-urlencoded",
                                          "accept": "application/json", "Origin": "https://203.0.113.10"})
        self.assertEqual(wrong.status_code, 401)

    def test_with_oidc_it_is_only_deleted(self):
        from sbx_control import settings, state, users
        settings.update("sign_in", {"method": "oidc", "oidc_issuer": "https://id.example.com",
                                    "oidc_client_id": "sbx"}, by="test")
        self.shared()
        state.migrate()
        self.assertFalse(any(u["password"] for u in users.ls()))
        self.assertFalse(self.remaining())

    def test_an_unreadable_store_keeps_it_for_the_next_start(self):
        from unittest.mock import patch
        from sbx_control import secret_store, state, users
        self.shared()
        with patch.object(secret_store, "unseal", side_effect=secret_store.StoreError("unreachable")):
            state.migrate()
        self.assertTrue(self.remaining())
        self.assertIsNone(users.check_password("first@example.com", "old-shared-password"))
        state.migrate()
        self.assertIsNotNone(users.check_password("first@example.com", "old-shared-password"))
        self.assertFalse(self.remaining())
