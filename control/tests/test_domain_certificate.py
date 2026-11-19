"""A domain's certificate in the setup: delegated DNS's port 53 probe,
records that send the challenge elsewhere, an optional certificate email,
the certificate's error in /api/setup, and the setup link that stays the same."""
import contextlib
import io
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support import Installation, Server

DOMAIN = "sbx.example.com"
ZONE = f"_acme-challenge.{DOMAIN}"
HOST = "10.0.0.5"   # the responder's address on this host (acme_dns.primary_address)


def query(name: str, qtype: int = 16) -> bytes:
    labels = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    return struct.pack("!HHHHHH", 7, 0x0100, 1, 0, 0, 0) + labels + struct.pack("!HH", qtype, 1)


def reply_parts(reply: bytes) -> tuple[int, list[str]]:
    """The rcode and the TXT values of a responder's reply."""
    from sbx_control import acme_dns
    rcode = struct.unpack("!H", reply[2:4])[0] & 0xF
    count = struct.unpack("!H", reply[6:8])[0]
    _, offset = acme_dns._read_name(reply, 12)
    offset += 4
    found = []
    for _ in range(count):
        _, offset = acme_dns._read_name(reply, offset)
        rtype, _, _, length = struct.unpack("!HHIH", reply[offset:offset + 10])
        data = reply[offset + 10:offset + 10 + length]
        offset += 10 + length
        if rtype == 16:
            found.append(data[1:1 + data[0]].decode())
    return rcode, found


class Probe(Installation):
    SETTINGS = {"domain": {"name": DOMAIN}}

    def resolvers(self, reachable: set):
        """acme_dns._txt as the network would answer: a server in reachable
        gets the query to this host's responder (acme_dns.answer); any other
        times out."""
        from sbx_control import acme_dns
        asked = []

        def txt(server, name, timeout):
            asked.append((server, name))
            return reply_parts(acme_dns.answer(query(name)))[1] if server in reachable else []
        self.enterContext(patch.object(acme_dns, "_txt", side_effect=txt))
        self.enterContext(patch.object(acme_dns, "primary_address", return_value=HOST))
        return asked

    def leftovers(self):
        from sbx_control import acme_dns
        return list(acme_dns.probe_dir().iterdir()) if acme_dns.probe_dir().exists() else []

    def test_a_public_resolver_that_hears_the_responder_passes_and_nothing_stays_behind(self):
        from sbx_control import acme_dns
        asked = self.resolvers({HOST, "8.8.8.8"})   # 1.1.1.1 times out: the next resolver is asked
        ok, detail, fix = acme_dns.probe()
        self.assertTrue(ok, detail)
        self.assertEqual(detail, "a public resolver (8.8.8.8) reached this server on port 53")
        self.assertEqual(fix, "")
        self.assertEqual([server for server, _ in asked], [HOST, "1.1.1.1", "8.8.8.8"])
        names = {name for _, name in asked}
        [name] = names
        self.assertRegex(name, rf"^sbx-probe-[0-9a-f]{{8}}\.{ZONE.replace('.', '[.]')}$")
        self.assertEqual(self.leftovers(), [])
        acme_dns.probe()
        self.assertNotIn(name, {n for _, n in asked[3:]})   # a new name each time: no cached answer counts

    def test_a_dropped_port_53_fails_with_the_firewall_fix(self):
        from sbx_control import acme_dns
        self.resolvers({HOST})
        ok, detail, fix = acme_dns.probe()
        self.assertFalse(ok)
        self.assertIn("no public resolver (1.1.1.1, 8.8.8.8) reached this server on port 53", detail)
        self.assertEqual(fix, "allow TCP and UDP port 53 in your provider's firewall, or use a DNS API token "
                              "(sbx setup address --dns cloudflare)")
        self.assertEqual(self.leftovers(), [])

    def test_a_responder_that_is_not_running_is_told_apart_from_the_firewall(self):
        from sbx_control import acme_dns
        asked = self.resolvers(set())
        ok, detail, fix = acme_dns.probe()
        self.assertFalse(ok)
        self.assertIn(f"does not answer on {HOST}:53", detail)
        self.assertIn("sbx-acme-dns", fix)
        self.assertEqual([server for server, _ in asked], [HOST])
        self.assertEqual(self.leftovers(), [])

    def test_the_command_prints_the_finding_and_fails(self):
        from sbx_control import acme_dns
        self.resolvers({HOST})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(acme_dns.main(["probe"]), 1)
        self.assertIn("port 53: no public resolver", out.getvalue())
        self.assertIn("fix: allow TCP and UDP port 53", out.getvalue())

    def test_the_responder_answers_under_its_zone_only_for_a_running_probe(self):
        from sbx_control import acme_dns
        name = f"sbx-probe-0123abcd.{ZONE}"
        self.assertEqual(reply_parts(acme_dns.answer(query(name))), (3, []))   # NXDOMAIN
        acme_dns.probe_dir().mkdir(parents=True, exist_ok=True)
        (acme_dns.probe_dir() / "sbx-probe-0123abcd").write_text("fixture-value\n")
        self.addCleanup((acme_dns.probe_dir() / "sbx-probe-0123abcd").unlink)
        self.assertEqual(reply_parts(acme_dns.answer(query(name))), (0, ["fixture-value"]))
        self.assertEqual(reply_parts(acme_dns.answer(query(f"other.{ZONE}")))[0], 3)
        self.assertEqual(reply_parts(acme_dns.answer(query(f"sbx-probe-0123abcd.{DOMAIN}")))[0], 5)   # REFUSED
        self.assertEqual(reply_parts(acme_dns.answer(query(ZONE)))[0], 0)   # the zone itself, as before


class Records(Installation):
    def checks(self, acme="delegated", ns=(), cname=()):
        from sbx_control.connections import domain
        with patch.object(domain, "_resolve", return_value=["203.0.113.7"]), \
                patch.object(domain, "_host_ips", return_value={"203.0.113.7"}), \
                patch.object(domain, "_ns", return_value=list(ns)), patch.object(domain, "_cname", return_value=list(cname)):
            return {c.name: c for c in domain.Domain().test({"name": DOMAIN, "acme": acme})}

    def test_the_delegation_to_this_host_alone_passes(self):
        found = self.checks(ns=[f"{DOMAIN}."])
        self.assertTrue(found["delegation"].ok)
        self.assertTrue(found["other _acme-challenge records"].ok)

    def test_a_cname_or_another_name_server_is_named_with_the_fix(self):
        found = self.checks(cname=["challenges.elsewhere.example.net."])
        check = found["other _acme-challenge records"]
        self.assertFalse(check.ok)
        self.assertEqual(check.detail, f"{ZONE} CNAME challenges.elsewhere.example.net.")
        self.assertTrue(check.fix.startswith("remove it at your DNS provider"))
        self.assertIn(f"keep only {ZONE} NS {DOMAIN}.", check.fix)
        found = self.checks(ns=[f"{DOMAIN}.", "ns1.elsewhere.example.net."])
        self.assertTrue(found["delegation"].ok)
        self.assertFalse(found["other _acme-challenge records"].ok)
        self.assertIn("NS ns1.elsewhere.example.net.", found["other _acme-challenge records"].detail)

    def test_with_a_dns_api_any_delegation_or_alias_is_in_the_way(self):
        for acme in ("cloudflare", "route53"):
            check = self.checks(acme, ns=[f"{DOMAIN}."])["other _acme-challenge records"]
            self.assertFalse(check.ok, acme)
            self.assertIn(f"NS {DOMAIN}.", check.detail)
            self.assertIn(f"with {acme}, sbx writes the TXT record in the zone itself", check.fix)
        both = self.checks("cloudflare", ns=["a.example.net."], cname=["b.example.net."])["other _acme-challenge records"]
        self.assertEqual(both.detail, f"{ZONE} CNAME b.example.net., NS a.example.net.")
        self.assertTrue(both.fix.startswith("remove them"))
        self.assertTrue(self.checks("cloudflare")["other _acme-challenge records"].ok)

    def test_a_self_signed_certificate_asks_for_no_challenge_records(self):
        found = self.checks("none", ns=["a.example.net."])
        self.assertNotIn("other _acme-challenge records", found)
        self.assertNotIn("port 53", found)

    def test_the_probe_runs_only_once_the_domain_is_saved_for_delegated_dns(self):
        from sbx_control import acme_dns, settings
        with patch.object(acme_dns, "probe", return_value=(True, "reached", "")) as probe:
            self.assertNotIn("port 53", self.checks(ns=[f"{DOMAIN}."]))   # not saved: nothing answers yet
            settings.update("domain", {"name": DOMAIN}, by="test")
            found = self.checks(ns=[f"{DOMAIN}."])
            self.assertEqual(probe.call_count, 1)
            self.assertTrue(found["port 53"].ok)
            self.assertNotIn("port 53", self.checks("cloudflare"))


class CertificateEmail(Installation):
    def test_no_email_is_needed_and_one_given_is_checked(self):
        from sbx_control import settings
        settings.update("domain", {"name": DOMAIN}, by="test")
        self.assertEqual(settings.current().domain.acme_email, "")
        self.assertIn("email=''", settings.shell_lines(settings.current()))
        settings.update("domain", {"name": DOMAIN, "acme_email": " ops@example.com "}, by="test")
        self.assertEqual(settings.current().domain.acme_email, "ops@example.com")
        for wrong in ("not an email", "ops@example.com -n", "ops@example.com;true", "$(id)@example.com"):
            with self.assertRaises(settings.Invalid, msg=wrong):
                settings.update("domain", {"name": DOMAIN, "acme_email": wrong}, by="test")

    def test_the_form_marks_the_email_optional(self):
        from sbx_control.connections import domain
        field = next(f for f in domain.Domain().status().fields if f.name == "acme_email")
        self.assertTrue(field.optional)


class DomainSetup(Server):
    def setUp(self):
        super().setUp()
        from sbx_control.connections import domain
        self.tls = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="sbx-tls-")))
        self.enterContext(patch("sbx_control.paths.tls", return_value=self.tls))
        self.enterContext(patch.object(domain, "_resolve", return_value=["203.0.113.10"]))
        self.enterContext(patch.object(domain, "_host_ips", return_value={"203.0.113.10"}))
        self.enterContext(patch.object(domain, "_ns", return_value=[f"{DOMAIN}."]))
        self.probe = self.enterContext(patch("sbx_control.acme_dns.probe", return_value=(True, "reached", "")))
        self.error = self.tls / "certificate-error"

    def save(self):
        """What the setup page sends: the domain, the address, delegated, no email."""
        return self.client.put("/api/connections/domain", headers=self.headers, json={
            "values": {"name": DOMAIN, "address": "203.0.113.10", "acme": "delegated"}})

    def test_the_first_save_needs_no_email_and_the_first_failed_issuance_shows_in_the_setup(self):
        from sbx_control import settings
        response = self.save()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(settings.current().domain.acme_email, "")
        self.probe.assert_not_called()   # nothing answers for the name until tls.sh runs: it probes then
        self.assertIn(settings.HostUnit.HOST_GUARD, self.effects())   # 53 opens
        self.assertEqual(self.client.get("/api/setup", headers=self.headers).json()["access"]["certificate_error"], "")
        self.error.write_text("port 53: no public resolver (1.1.1.1, 8.8.8.8) reached this server on port 53 within "
                              "5 s; fix: allow TCP and UDP port 53 in your provider's firewall\n")
        view = self.client.get("/api/setup", headers=self.headers).json()
        self.assertIn("allow TCP and UDP port 53", view["access"]["certificate_error"])
        self.assertFalse(view["steps"][0]["done"])
        from sbx_control import connections
        self.assertIn("port 53", connections.get("domain").status().facts["certificate_error"])

    def test_a_retry_probes_port_53_and_asks_for_the_certificate_again(self):
        from sbx_control import acme_dns
        self.assertEqual(self.save().status_code, 200)
        self.effects()
        self.error.write_text("port 53: no public resolver reached this server on port 53\n")
        self.probe.return_value = (False, "no public resolver (1.1.1.1, 8.8.8.8) reached this server on port 53",
                                   acme_dns.PORT_53_FIX)
        refused = self.save()
        self.assertEqual(refused.status_code, 400, refused.text)
        check = next(c for c in refused.json()["detail"]["checks"] if c["name"] == "port 53")
        self.assertEqual((check["ok"], check["fix"]), (False, acme_dns.PORT_53_FIX))
        self.assertEqual(self.effects(), [])
        self.probe.return_value = (True, "a public resolver (1.1.1.1) reached this server on port 53", "")
        self.assertEqual(self.save().status_code, 200)
        self.assertEqual(self.effects(), ["tls"])   # the same values again: issuance again
        self.assertFalse(self.error.exists())       # and the old error is gone until it fails again

    def test_a_stray_record_refuses_the_domain(self):
        from sbx_control.connections import domain
        with patch.object(domain, "_cname", return_value=["elsewhere.example.net."]):
            refused = self.save()
        self.assertEqual(refused.status_code, 400)
        check = next(c for c in refused.json()["detail"]["checks"] if c["name"] == "other _acme-challenge records")
        self.assertIn("CNAME elsewhere.example.net.", check["detail"])
        self.assertIn("remove it", check["fix"])

    def test_an_issued_certificate_clears_the_error_in_the_setup(self):
        from datetime import datetime, timedelta, timezone
        self.assertEqual(self.save().status_code, 200)
        self.error.write_text("an old failure\n")
        self.expiry.return_value = datetime.now(timezone.utc) + timedelta(days=90)
        self.assertEqual(self.client.get("/api/setup", headers=self.headers).json()["access"]["certificate_error"], "")


class SetupLink(Server):
    def link(self, *argv):
        from sbx_control import admin
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(admin.main(["setup-link", *argv]), 0)
        return out.getvalue().strip()

    def test_the_link_stays_the_same_until_a_new_one_is_asked_for(self):
        from sbx_control import setup
        first = self.link()
        self.assertTrue(first.endswith(f"/setup#t={self.token}"))
        self.assertEqual(self.link(), first)
        self.assertTrue(setup.token_ok(self.token))
        new = self.link("--new")
        self.assertNotEqual(new, first)
        self.assertFalse(setup.token_ok(self.token))   # the old link stops working
        self.assertEqual(self.link(), new)

    def test_a_missing_or_stale_token_file_gets_a_new_link(self):
        from sbx_control import setup
        setup.token_file().write_text("stale-token\n")
        fresh = self.link()
        self.assertNotIn("stale-token", fresh)
        self.assertTrue(setup.token_ok(fresh.split("#t=")[1]))
        setup.token_file().unlink()
        self.assertNotEqual(self.link(), fresh)


class DelegationFromTheParentZone(unittest.TestCase):
    """The NS record is read from the parent zone's own nameservers: a resolver fails (SERVFAIL) following a
    delegation to a server whose port 53 is closed, which must not read as a missing record."""

    def test_the_parent_zone_answers_even_while_port_53_is_closed(self):
        from unittest.mock import patch
        from sbx_control.connections import domain

        class Done:
            def __init__(self, stdout, returncode=0): self.stdout, self.returncode = stdout, returncode

        referral = "_acme-challenge.sbx.example.com.\t60\tIN\tNS\tsbx.example.com.\n"
        with patch.object(domain, "_dig_short", side_effect=lambda kind, name: ["ns1.example.net"] if name == "example.com" else []), \
                patch.object(domain.subprocess, "run", return_value=Done(referral)) as run, \
                patch.object(domain, "_dig", return_value=[]) as recursive:
            self.assertEqual(domain._ns("_acme-challenge.sbx.example.com"), ["sbx.example.com."])
        self.assertIn("@ns1.example.net", run.call_args.args[0])
        self.assertIn("+norecurse", run.call_args.args[0])
        recursive.assert_not_called()

    def test_a_parent_nameserver_that_does_not_answer_is_skipped(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from sbx_control.connections import domain
        referral = "_acme-challenge.sbx.example.com.\t60\tIN\tNS\tsbx.example.com.\n"
        answers = [SimpleNamespace(stdout=";; no servers could be reached\n", returncode=9),
                   SimpleNamespace(stdout=referral, returncode=0)]
        with patch.object(domain, "_dig_short", side_effect=lambda kind, name: ["ns1.example.net", "ns2.example.net"]
                          if name == "example.com" else []), \
                patch.object(domain.subprocess, "run", side_effect=answers) as run, \
                patch.object(domain, "_dig", return_value=[]) as recursive:
            self.assertEqual(domain._ns("_acme-challenge.sbx.example.com"), ["sbx.example.com."])
        self.assertIn("@ns2.example.net", run.call_args.args[0])
        recursive.assert_not_called()

    def test_without_a_parent_zone_it_asks_the_resolver(self):
        from unittest.mock import patch
        from sbx_control.connections import domain
        with patch.object(domain, "_dig_short", return_value=[]), patch.object(domain, "_dig", return_value=["x."]) as recursive:
            self.assertEqual(domain._ns("_acme-challenge.sbx.example.com"), ["x."])
        recursive.assert_called_once_with("NS", "_acme-challenge.sbx.example.com")
