"""The how-to links (howto.py): every page and anchor a check links to
exists in docs/howto/, and the checks for what the owner sets up outside
sbx carry their help_url through the API, `sbx-admin doctor` and the CLI."""
import contextlib
import io
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from fake_boat import configured

from sbx_control import howto
from support import Installation, Server

DOCS = Path(__file__).resolve().parents[2] / "docs/howto"


def slugs(markdown: str) -> set:
    """GitHub's anchors for a page's headings."""
    return {re.sub(r"[^\w\- ]", "", line.lstrip("#").strip().lower()).replace(" ", "-")
            for line in markdown.splitlines() if re.match(r"#{2,3} ", line)}


class Pages(unittest.TestCase):
    def test_every_linked_anchor_is_a_heading_of_its_page(self):
        for page, anchors in howto.PAGES.items():
            found = slugs((DOCS / f"{page}.md").read_text())
            self.assertLessEqual(set(anchors), found, page)

    def test_the_links_go_to_sbx_s_own_repository(self):
        from sbx_control.settings import Updates
        self.assertEqual(howto.REPOSITORY, Updates().repository)
        self.assertEqual(howto.E2B_KEY, f"https://github.com/{Updates().repository}/blob/main/docs/howto/e2b.md#api-key")
        with self.assertRaises(KeyError):
            howto.url("cloudflare", "no-such-section")


class Providers(unittest.TestCase):
    def test_a_refused_key_links_to_its_provider_s_page(self):
        from sbx_control.runtime.e2b_cloud import Cloud
        cloud = Cloud()
        with patch.object(Cloud, "templates", side_effect=RuntimeError("401 unauthorized")):
            (check,) = cloud.test()
        self.assertEqual((check.ok, check.help_url), (False, howto.E2B_KEY))
        backend, fake = configured()
        with patch.object(type(backend), "templates", side_effect=RuntimeError("401")):
            (check,) = backend.test()
        self.assertEqual((check.ok, check.help_url), (False, howto.BOAT_KEY))

    def test_the_boat_trial_links_to_the_plans(self):
        backend, fake = configured()
        fake.plan = "trial"
        checks = {c.name: c for c in backend.test()}
        self.assertEqual(checks["Boat plan"].help_url, howto.BOAT_PLAN)
        self.assertEqual(checks["Boat API"].help_url, "")   # a check that passed needs no page

    def test_a_runtime_without_an_owner_side_setup_has_no_page(self):
        from sbx_control.runtime.embed import Embed
        self.assertEqual(Embed.help_url, "")


class Domain(Installation):
    def checks(self, **values):
        from sbx_control.connections import domain
        with patch.object(domain, "_resolve", return_value=[]), patch.object(domain, "_ns", return_value=[]), \
                patch.object(domain, "_host_ips", return_value={"203.0.113.7"}):
            return {c.name: c for c in domain.Domain().test({"name": "sbx.example.com", **values})}

    def test_missing_records_and_credentials_link_to_the_dns_page(self):
        found = self.checks(acme="cloudflare")
        self.assertEqual(found["app.sbx.example.com resolves"].help_url, howto.DNS_RECORDS)
        self.assertEqual(found["*.sbx.example.com resolves"].help_url, howto.DNS_RECORDS)
        self.assertEqual(found["Cloudflare token"].help_url, howto.CLOUDFLARE_TOKEN)
        self.assertEqual(found["certificate"].help_url, howto.CLOUDFLARE_TOKEN)
        delegated = self.checks(acme="delegated")
        self.assertEqual(delegated["delegation"].help_url, howto.DELEGATED_DNS)
        self.assertEqual(self.checks(acme="none")["certificate"].help_url, "")

    def test_doctor_prints_the_page_under_a_failed_check(self):
        from sbx_control import admin, connections
        from sbx_control.connections import Check, Status

        class Dns:
            kind, title = "domain", "Domain & certificate"

            def status(self):
                return Status(self.kind, self.title, "attention", "", {}, ())

            def test(self):
                return [Check("*.sbx.example.com resolves", False, "no address", "a wildcard A record",
                              help_url=howto.DNS_RECORDS),
                        Check("certificate", True, "valid", help_url=howto.CLOUDFLARE_TOKEN)]

        self.addCleanup(connections.REGISTRY.clear)
        connections.REGISTRY.clear()
        connections.REGISTRY.update({"domain": Dns()})
        with patch("sbx_control.platform.connect"), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(admin.do_doctor(), 1)
        self.assertIn(f"how to: {howto.DNS_RECORDS}", out.getvalue())
        self.assertNotIn(howto.CLOUDFLARE_TOKEN, out.getvalue())   # only a failed check's


class DomainApi(Server):
    def test_a_refused_domain_sends_help_url_with_each_check(self):
        from sbx_control.connections import domain
        with patch.object(domain, "_resolve", return_value=[]), patch.object(domain, "_host_ips", return_value=set()):
            r = self.client.put("/api/connections/domain", headers=self.headers,
                                json={"values": {"name": "sbx.example.com", "acme": "cloudflare"}})
        self.assertEqual(r.status_code, 400, r.text)
        sent = {c["name"]: c for c in r.json()["detail"]["checks"]}
        self.assertEqual(sent["app.sbx.example.com resolves"]["help_url"], howto.DNS_RECORDS)
        self.assertEqual(sent["Cloudflare token"]["help_url"], howto.CLOUDFLARE_TOKEN)


class GitHubAndAmp(Installation):
    def test_no_github_app_links_to_creating_it(self):
        from sbx_control.connections.github import GitHub
        found = {c.name: c for c in GitHub().test()}
        self.assertEqual(found["GitHub App"].help_url, howto.GITHUB_APP)

    def test_the_readiness_github_step_links_to_the_app_or_its_repositories(self):
        from sbx_control import readiness
        info = {"repo": "owner/app", "trust": "trusted", "configured": True, "prebuilt": True, "previews": [],
                "secrets_missing": [], "github": "anonymous"}
        def github(**over):
            found = readiness.steps({**info, **over}, [], [], {}, False)
            return next(s for s in found["steps"] if s["step"] == "github")
        self.assertEqual(github()["help_url"], howto.GITHUB_APP)
        self.assertEqual(github(github_app_install="https://github.com/apps/x/installations/new")["help_url"],
                         howto.GITHUB_REPOS)
        self.assertNotIn("help_url", github(github="app"))

    def test_amp_s_token_and_connection_link_to_its_page(self):
        from sbx_control import extensions
        found = {c.name: c for c in extensions.get("amp").test()}
        self.assertEqual(found["provider token"].help_url, howto.AMP_PROVIDER)
        self.assertEqual(found["provider service"].help_url, howto.AMP_CONNECTION)


if __name__ == "__main__":
    unittest.main()
