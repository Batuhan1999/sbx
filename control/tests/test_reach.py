"""Network settings follow what the runtime enforces (runtime.Capabilities):
on one that fences sandboxes and reaches this host's private ranges they
are offered and kept; on one that doesn't, they are refused by the
capability's name, never silently stored."""
import io
from contextlib import redirect_stderr
from unittest.mock import patch

from support import Server

CIDR = {"internal_cidrs": ["10.0.0.0/24"]}
PUBLIC = {"public_repos": True}


class CapableRuntimeTests(Server):
    """The default runtime applies egress rules and reaches private ranges."""

    def setUp(self):
        super().setUp()
        from sbx_control import reach, runtime, settings
        self.reach, self.runtime, self.settings = reach, runtime, settings

    def test_it_offers_both_and_keeps_each_alone(self):
        caps = self.runtime.capabilities()
        self.assertTrue(caps.egress_rules and caps.private_networks)
        response = self.client.get("/api/settings", headers=self.headers)
        self.assertEqual(response.json()["offered"], {"public_repos": True, "internal_cidrs": True})
        self.settings.update("access", PUBLIC, by="test")
        self.settings.update("access", {"public_repos": False}, by="test")
        self.settings.update("network", CIDR, by="test")
        self.assertEqual(self.settings.current().network.internal_cidrs, ("10.0.0.0/24",))

    def test_public_repos_and_internal_ranges_exclude_each_other_whichever_comes_first(self):
        self.settings.update("network", CIDR, by="test")
        with self.assertRaisesRegex(self.settings.Invalid, r"every sandbox alike \(private_networks\)"):
            self.settings.update("access", PUBLIC, by="test")
        self.settings.update("network", {"internal_cidrs": []}, by="test")
        self.settings.update("access", PUBLIC, by="test")
        with self.assertRaisesRegex(self.settings.Invalid, "can't both be set"):
            self.settings.update("network", CIDR, by="test")

    def test_sandboxes_are_fenced_from_the_metadata_service(self):
        self.assertEqual(self.runtime.deny_out("169.254.169.254/32"), ["169.254.169.254/32"])


class IncapableRuntimeTests(Server):
    """A runtime that enforces neither: what it can't apply is not stored."""

    def setUp(self):
        super().setUp()
        from sbx_control import reach, runtime, settings
        self.reach, self.runtime, self.settings = reach, runtime, settings
        self.enterContext(patch.object(runtime, "capabilities", return_value=runtime.Capabilities()))

    def test_the_settings_page_is_offered_neither(self):
        response = self.client.get("/api/settings", headers=self.headers)
        self.assertEqual(response.json()["offered"], {"public_repos": False, "internal_cidrs": False})

    def test_public_repos_is_refused_by_the_capability_it_needs(self):
        response = self.client.put("/api/settings/access", headers=self.headers, json={"value": PUBLIC})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("egress_rules", response.text)
        self.assertNotIn("Embed", response.text)
        self.assertFalse(self.settings.current().access.public_repos)

    def test_internal_ranges_are_refused_by_the_capability_they_need(self):
        with self.assertRaisesRegex(self.settings.Invalid, r"private network \(private_networks\)"):
            self.settings.update("network", CIDR, by="test")
        self.assertEqual(self.settings.current().network.internal_cidrs, ())

    def test_sbx_admin_config_set_refuses_them_too(self):
        from sbx_control import admin
        for section, value in (("access", '{"public_repos": true}'), ("network", '{"internal_cidrs": ["10.0.0.0/24"]}')):
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(admin.do_config_set(section, value), 1)
            self.assertIn("not set:", err.getvalue())
        # What the runtime can apply is still written.
        with redirect_stderr(io.StringIO()):
            self.assertEqual(admin.do_config_set("sandboxes", '{"paused_days": 5}'), 0)

    def test_a_stale_public_repos_can_still_be_turned_off(self):
        with patch.object(self.runtime, "capabilities", return_value=self.runtime.Capabilities(egress_rules=True)):
            self.settings.update("access", PUBLIC, by="test")
        self.settings.update("access", {"public_repos": False}, by="test")
        self.assertFalse(self.settings.current().access.public_repos)

    def test_sandboxes_get_no_egress_rules_it_would_ignore(self):
        self.assertIsNone(self.runtime.deny_out("169.254.169.254/32"))
