"""Endpoints: a managed server or an internal host for sandboxes on a
runtime that doesn't reach this host's private network. The model, its
per-repo access and refusals over the API, nginx's stream configuration (a
server per endpoint with its own CA, nothing without one), the CAs and the
sandboxes' short certificates (issued, pushed on resume, pulled by the
forwarder; refused for a deleted sandbox or a revoked grant), the gate's
refusal naming the command, the values a provider runtime's sandbox gets
(the forwarder's address; Embed's unchanged), the forwarder itself against
TLS servers that verify their own endpoint's CA, and no key in an answer."""
import datetime
import http.server
import json
import socket
import ssl
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import Installation, Server

DOMAIN = "sbx.example.com"
PG = "172.29.50.10:5432"
GUEST = Path(__file__).parents[1] / "sbx_control/template_files"


def provider(runtime):
    """A runtime whose sandboxes don't reach this host's private network."""
    return patch.object(runtime, "capabilities", return_value=runtime.Capabilities(snapshots=True, egress_rules=True))


class Setup:
    def configure(self):
        from sbx_control import endpoints, hosted_services, runtime, settings
        from sbx_control.catalog import attachments
        self.endpoints, self.hosted, self.runtime, self.settings = endpoints, hosted_services, runtime, settings
        settings.update("domain", {"name": DOMAIN}, by="test")
        settings.update("network", {"internal_cidrs": ["10.0.0.0/24"]}, by="test")
        self.servers = {"dev_pg": ("postgres", PG)}
        self.bound = {"owner/app": ["dev_pg"]}   # repo -> the managed servers its attachments bind to
        self.enterContext(patch.object(hosted_services, "target",
                                       side_effect=lambda name: (self.servers.get(name) or (None, None))[1]))
        self.enterContext(patch.object(hosted_services, "records", side_effect=lambda: [
            {"name": n, "kind": k} for n, (k, _) in sorted(self.servers.items())]))
        self.enterContext(patch.object(hosted_services, "attached_to", side_effect=lambda repo: self.bound.get(repo, [])))
        self.enterContext(patch.object(attachments, "repos", side_effect=lambda: sorted(self.bound)))
        self.effects()

    def register(self, name, repo="owner/app"):
        from sbx_control import sandboxes
        sandboxes.register(name, "operator@example.com", "main", name, {"repo": repo}, "fake")

    def info(self, name, repo="owner/app", state="running"):
        return SimpleNamespace(sandbox_id=f"id-{name}", metadata={"sbx": name, "repo": repo}, runtime="fake",
                               state=state, repo=repo)

    def sandbox_found(self):
        """core.find and core.app_of answer for the registered sandboxes; the runtime is a provider's."""
        from sbx_control import core, sandboxes
        repos = {}
        self.enterContext(patch.object(core, "find", side_effect=lambda name: self.info(name, repos.get(name, "owner/app"))
                                       if sandboxes.registered_owner(name) else None))
        self.enterContext(patch.object(core, "app_of", side_effect=lambda info: SimpleNamespace(repo=info.repo)))
        self.enterContext(provider(self.runtime))
        return repos


class ApiTests(Setup, Server):
    def setUp(self):
        super().setUp()
        self.repo_admin()
        self.configure()

    def test_an_internal_host_is_added_for_its_repos_applying_then_on(self):
        response = self.client.post("/api/endpoints", json={"name": "temporal", "target": "10.0.0.5:7233",
                                                            "repos": ["Owner/App"]})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json(), {
            "name": "temporal", "address": f"temporal.{DOMAIN}", "kind": "internal", "server": "", "server_kind": "",
            "target": "10.0.0.5:7233", "repos": ["owner/app"], "state": "applying", "message": "", "used_by": []})
        self.assertIn("endpoints", self.effects())
        found = self.client.get("/api/endpoints").json()
        self.assertTrue(found["applying"])
        self.assertEqual((found["domain"], found["needed"], found["failure"]), (DOMAIN, False, ""))
        self.endpoints._applied_file().write_text(json.dumps({"revision": 1, "ok": True, "ok_revision": 1}))
        found = self.client.get("/api/endpoints").json()
        self.assertEqual(found["endpoints"][0]["state"], "on")
        self.assertFalse(found["applying"])
        self.assertFalse(any(port in json.dumps(found) for port in ("8441", "8442", ":443")))   # ports are not the user's concern
        self.assertNotIn("PRIVATE KEY", json.dumps(found))

    def test_grants_are_replaced_with_put_and_a_managed_server_s_follow_its_attachments(self):
        self.client.post("/api/endpoints", json={"name": "temporal", "target": "10.0.0.5:7233"})
        response = self.client.put("/api/endpoints/temporal", json={"repos": ["owner/app", "other/public"]})
        self.assertEqual((response.status_code, response.json()["repos"]), (200, ["other/public", "owner/app"]))
        self.assertEqual(self.client.put("/api/endpoints/temporal", json={"repos": ["not a repo"]}).status_code, 400)
        self.assertEqual(self.client.put("/api/endpoints/nope", json={"repos": []}).status_code, 404)
        self.client.post("/api/endpoints", json={"server": "dev_pg"})
        self.assertEqual(self.endpoints.one("dev-pg")["repos"], ["owner/app"])
        response = self.client.put("/api/endpoints/dev-pg", json={"repos": ["other/public"]})
        self.assertEqual(response.status_code, 400)
        self.assertIn("attach the server to the repo instead", response.json()["detail"])
        response = self.client.post("/api/endpoints", json={"server": "dev_pg", "name": "x", "repos": ["a/b"]})
        self.assertEqual(response.status_code, 400)

    def test_a_managed_server_is_a_candidate_until_it_has_one(self):
        found = self.client.get("/api/endpoints").json()
        self.assertEqual(found["candidates"], [{"server": "dev_pg", "kind": "postgres",
                                                "address": f"dev-pg.{DOMAIN}", "target": PG}])
        response = self.client.post("/api/endpoints", json={"server": "dev_pg"})
        self.assertEqual(response.status_code, 202, response.text)
        added = response.json()
        self.assertEqual((added["name"], added["kind"], added["server"], added["server_kind"], added["target"]),
                         ("dev-pg", "managed-server", "dev_pg", "postgres", PG))
        self.assertEqual(self.client.get("/api/endpoints").json()["candidates"], [])
        self.assertEqual(self.client.post("/api/endpoints", json={"server": "dev_pg", "name": "other"}).status_code, 409)
        with patch.object(self.runtime, "enabled", return_value=["embed"]), provider(self.runtime):
            self.assertTrue(self.client.get("/api/endpoints").json()["needed"])

    def test_refusals_say_why(self):
        for body, status, said in (
                ({"name": "x", "target": "10.9.9.9:80"}, 400, "outside network.internal_cidrs"),
                ({"name": "x", "target": "db.internal:5432"}, 400, "must be an IP address"),
                ({"name": "x", "target": "10.0.0.5"}, 400, "is not HOST:PORT"),
                ({"name": "Bad_Name", "target": "10.0.0.5:80"}, 400, "not a host name label"),
                ({"name": "login", "target": "10.0.0.5:80"}, 409, "taken by this installation"),
                ({"server": "nope"}, 400, "no managed server nope"),
                ({}, 400, "give a managed server")):
            with self.subTest(body=body):
                response = self.client.post("/api/endpoints", json=body)
                self.assertEqual(response.status_code, status, response.text)
                self.assertIn(said, response.json()["detail"])
        self.assertEqual(self.client.post("/api/endpoints", json={"name": "t", "target": "10.0.0.5:80"}).status_code, 202)
        for body, said in (({"name": "t", "target": "10.0.0.6:80"}, "there is an endpoint t already"),
                           ({"name": "u", "target": "10.0.0.5:80"}, "10.0.0.5:80 has an endpoint already: t")):
            response = self.client.post("/api/endpoints", json=body)
            self.assertEqual((response.status_code, response.json()["detail"]), (409, said))
        self.settings.update("domain", {"name": ""}, by="test")
        response = self.client.post("/api/endpoints", json={"name": "v", "target": "10.0.0.7:80"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("connect a domain first", response.json()["detail"])

    def test_removing_one_sandboxes_use_is_refused_naming_their_repos(self):
        self.client.post("/api/endpoints", json={"server": "dev_pg"})
        self.register("box")
        self.endpoints._record_certificate("box", "owner/app", ["dev-pg"], time.time() + 3600)
        self.assertEqual(self.client.get("/api/endpoints").json()["endpoints"][0]["used_by"], ["owner/app"])
        response = self.client.delete("/api/endpoints/dev-pg")
        self.assertEqual(response.status_code, 409)
        self.assertIn("sandboxes of owner/app use the endpoint dev-pg", response.json()["detail"])
        from sbx_control import sandboxes
        sandboxes.unregister("box")   # deleted: its certificate no longer counts
        self.effects()
        self.assertEqual(self.client.delete("/api/endpoints/dev-pg").status_code, 202)
        self.assertEqual(self.effects(), ["endpoints"])
        found = self.client.get("/api/endpoints").json()
        self.assertEqual((found["endpoints"], found["applying"]), ([], True))
        self.assertEqual(self.client.delete("/api/endpoints/dev-pg").status_code, 404)

    def test_a_failed_apply_and_one_never_picked_up_are_failed(self):
        self.client.post("/api/endpoints", json={"name": "t", "target": "10.0.0.5:80"})
        self.endpoints._applied_file().write_text(json.dumps({"revision": 1, "ok": False, "message": "nginx said no"}))
        found = self.client.get("/api/endpoints").json()
        self.assertEqual((found["endpoints"][0]["state"], found["endpoints"][0]["message"]), ("failed", "nginx said no"))
        self.assertEqual(found["failure"], "nginx said no")
        self.endpoints._applied_file().unlink()
        request = self.root / "run/apply/endpoints"
        request.parent.mkdir(parents=True, exist_ok=True)
        request.write_text("0\n")
        old = time.time() - self.endpoints.PICKUP_SECONDS - 5
        import os
        os.utime(request, (old, old))
        self.assertIn("did not pick up", self.client.get("/api/endpoints").json()["endpoints"][0]["message"])

    def test_only_admins(self):
        from sbx_control import auth
        headers = {"Authorization": f"Bearer {auth.mint_token('someone@example.com', 'test')}"}
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/endpoints", headers=headers).status_code, 403)
        self.assertEqual(self.client.post("/api/endpoints", headers=headers, json={"server": "dev_pg"}).status_code, 403)
        self.assertEqual(self.client.put("/api/endpoints/x", headers=headers, json={"repos": []}).status_code, 403)

    def test_a_sandbox_renews_with_its_own_credential_and_only_while_it_may(self):
        from sbx_control import github_app, platform, sandboxes
        self.sandbox_found()
        self.client.post("/api/endpoints", json={"server": "dev_pg"})
        self.client.post("/api/endpoints", json={"name": "temporal", "target": "10.0.0.5:7233", "repos": ["owner/app"]})
        self.client.post("/api/endpoints", json={"name": "other", "target": "10.0.0.6:80", "repos": ["other/repo"]})
        self.register("box")
        self.client.cookies.clear()

        def renew(name="box", credential=None):
            credential = credential or github_app.sandbox_credential(name, platform.session_key())
            return self.client.post("/api/sandbox/endpoint-certs", headers={"Authorization": f"Bearer {name}:{credential}"})
        self.assertEqual(renew(credential="0" * 64).status_code, 401)
        response = renew()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual([e["name"] for e in response.json()["endpoints"]], ["dev-pg", "temporal"])   # not other's
        self.endpoints.set_repos("temporal", [])   # revoked: no new certificate for it
        self.assertEqual([e["name"] for e in renew().json()["endpoints"]], ["dev-pg"])
        self.bound["owner/app"] = []
        response = renew()
        self.assertEqual(response.status_code, 403)
        self.bound["owner/app"] = ["dev_pg"]
        sandboxes.unregister("box")   # deleted
        self.assertEqual(renew().status_code, 404)


class HostTests(Setup, Installation):
    """nginx's stream configuration and the root side's apply."""

    def setUp(self):
        super().setUp()
        self.configure()
        self.tls = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="sbx-tls-")))
        (self.tls / "nginx").mkdir()
        self.enterContext(patch.object(self.endpoints.paths, "tls", return_value=self.tls))

    def test_443_s_front_sends_endpoint_names_to_their_servers_and_the_rest_to_https(self):
        front = self.endpoints.stream_config()
        self.assertIn("map $ssl_preread_server_name $sbx_backend {\n  default 127.0.0.1:8442;\n}", front)
        self.assertIn("  listen 443;\n  ssl_preread on;\n  proxy_pass $sbx_backend;\n  proxy_protocol on;", front)
        self.assertNotIn("8441", front)   # no endpoint: every name goes to the HTTPS server, as before
        self.endpoints.write_nginx(self.tls / "nginx")
        self.assertEqual(sorted(p.name for p in (self.tls / "nginx").iterdir()), ["endpoints.stream"])
        self.endpoints.add(server="dev_pg", by="test")
        self.endpoints.add(name="temporal", target="10.0.0.5:7233", repos=["owner/app"], by="test")
        text = self.endpoints.stream_config()
        self.assertIn(f"  default 127.0.0.1:8442;\n  dev-pg.{DOMAIN} 127.0.0.1:8441;\n  temporal.{DOMAIN} 127.0.0.1:8441;\n",
                      text)
        servers = text.split("server {")[2:]
        self.assertIn("listen 127.0.0.1:8441 ssl proxy_protocol;\n  ssl_reject_handshake on;", servers[0])
        self.assertEqual(len(servers), 3)
        for server, name, target in zip(servers[1:], ("dev-pg", "temporal"), (PG, "10.0.0.5:7233")):
            self.assertIn("listen 127.0.0.1:8441 ssl proxy_protocol;", server)
            self.assertIn(f"server_name {name}.{DOMAIN};", server)
            self.assertIn(f"ssl_client_certificate /etc/nginx/conf.d/endpoint-{name}-ca.pem;", server)
            self.assertIn("ssl_verify_client on;", server)
            self.assertIn(f"proxy_pass {target};", server)
        self.assertEqual(text.count("listen 443;"), 1)   # 443 has one owner: the front
        self.endpoints.write_nginx(self.tls / "nginx")
        cas = {p.name: p.read_text() for p in (self.tls / "nginx").glob("endpoint-*-ca.pem")}
        self.assertEqual(sorted(cas), ["endpoint-dev-pg-ca.pem", "endpoint-temporal-ca.pem"])
        self.assertNotEqual(cas["endpoint-dev-pg-ca.pem"], cas["endpoint-temporal-ca.pem"])   # a CA each
        self.assertNotIn("PRIVATE KEY", "".join(cas.values()))
        # The HTTPS server learns the client's address from the PROXY protocol, from the front only.
        self.assertIn("set_real_ip_from 127.0.0.1;\n  real_ip_header proxy_protocol;", self.endpoints.MAIN_CONF)
        self.assertIn("stream {\n  include /etc/nginx/conf.d/*.stream;", self.endpoints.MAIN_CONF)
        # No port opens for endpoints: the firewall's ports are the settings' alone.
        self.assertIn("public_ports='22 443 53'", self.settings.shell_lines(self.settings.current(), {}))
        self.endpoints._delete("temporal")
        self.endpoints.write_nginx(self.tls / "nginx")
        self.assertEqual([p.name for p in (self.tls / "nginx").glob("endpoint-*-ca.pem")], ["endpoint-dev-pg-ca.pem"])
        self.settings.update("domain", {"name": ""}, by="test")
        self.assertEqual(self.endpoints.stream_config(), front)   # without a domain: the front alone
        self.endpoints.write_nginx(self.tls / "nginx")
        self.assertEqual(list((self.tls / "nginx").glob("endpoint-*-ca.pem")), [])

    def nginx(self, code):
        script = self.tls / "fake-nginx"
        script.write_text(f"#!/bin/sh\necho 'nginx: [emerg] details for the journal'\nexit {code}\n")
        script.chmod(0o755)
        (self.tls / "nginx.conf").write_text(self.endpoints.MAIN_CONF)
        return patch.object(self.endpoints, "NGINX", str(script))

    def test_apply_records_its_outcome_and_leaves_the_firewall_alone(self):
        self.endpoints.add(server="dev_pg", by="test")
        real = self.endpoints.subprocess.run
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return real(argv, **kwargs)
        with self.nginx(0), patch.object(self.endpoints.subprocess, "run", side_effect=run):
            self.assertEqual(self.endpoints.apply_host(), 0)
        self.assertEqual(calls, [[str(self.tls / "fake-nginx")]])
        self.assertEqual(self.endpoints.one("dev-pg")["state"], "on")
        self.assertIn(f"dev-pg.{DOMAIN} 127.0.0.1:8441;", (self.tls / "nginx/endpoints.stream").read_text())

    def test_a_refused_configuration_keeps_the_one_before_and_keeps_nginx_s_words_out_of_the_answer(self):
        (self.tls / "nginx/endpoints.stream").write_text("# before\n")
        self.endpoints.add(server="dev_pg", by="test")
        with self.nginx(1), patch("sys.stderr"):
            self.assertEqual(self.endpoints.apply_host(), 1)
        self.assertEqual((self.tls / "nginx/endpoints.stream").read_text(), "# before\n")
        self.assertEqual(list((self.tls / "nginx").glob("endpoint-*-ca.pem")), [])
        found = self.endpoints.one("dev-pg")
        self.assertEqual(found["state"], "failed")
        self.assertIn("the one before stays", found["message"])
        self.assertNotIn("emerg", found["message"])


class CertificateTests(Setup, Installation):
    def setUp(self):
        super().setUp()
        self.configure()
        self.endpoints.add(server="dev_pg", by="test")
        self.endpoints.add(name="temporal", target="10.0.0.5:7233", repos=["owner/app"], by="test")
        self.rows = {r["name"]: r for r in self.endpoints._rows()}

    def test_a_sandbox_s_certificate_is_short_for_clients_only_and_from_its_endpoint_s_ca(self):
        from cryptography import x509
        from cryptography.x509.oid import ExtendedKeyUsageOID
        cert_pem, key_pem, not_after = self.endpoints.issue("box", self.rows["dev-pg"])
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        own = x509.load_pem_x509_certificate(self.rows["dev-pg"]["ca_cert"].encode())
        other = x509.load_pem_x509_certificate(self.rows["temporal"]["ca_cert"].encode())
        cert.verify_directly_issued_by(own)
        with self.assertRaises(Exception):   # another endpoint's CA does not vouch for it
            cert.verify_directly_issued_by(other)
        lifetime = cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc)
        self.assertTrue(datetime.timedelta(hours=23, minutes=55) < lifetime <= datetime.timedelta(hours=24))
        self.assertAlmostEqual(not_after, cert.not_valid_after_utc.timestamp(), delta=1)
        self.assertEqual(list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value),
                         [ExtendedKeyUsageOID.CLIENT_AUTH])
        self.assertFalse(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
        self.assertIn("PRIVATE KEY", key_pem)
        from sbx_control import state
        with state.db() as conn:
            stored = [r[0] for r in conn.execute("select ca_key from endpoints")]
        self.assertFalse(any("PRIVATE KEY" in s for s in stored))   # sealed in the secret store, references here

    def test_the_keeper_renews_what_runs_out_soon(self):
        from sbx_control import delivery, keeper
        self.register("soon")
        self.register("later")
        self.endpoints._record_certificate("soon", "owner/app", ["dev-pg"], time.time() + 3600)
        self.endpoints._record_certificate("later", "owner/app", ["dev-pg"], time.time() + 20 * 3600)
        self.endpoints._record_certificate("gone", "owner/app", ["dev-pg"], time.time())   # not registered
        self.assertEqual(self.endpoints.due(), ["soon"])
        keeper.renew_certificates()
        keeper.renew_certificates()
        self.assertEqual(delivery.waiting(), {"soon"})
        self.assertEqual(delivery.pending("soon")[1], False)   # no app restart

    def test_sbx_pushes_fresh_certificates_when_it_resumes_a_sandbox(self):
        from sbx_control import core
        self.sandbox_found()
        self.register("box")
        written, commands = {}, []
        sbx = SimpleNamespace(commands=SimpleNamespace(run=lambda c, **kw: commands.append(c)),
                              files=SimpleNamespace(write=lambda path, text, **kw: written.update({path: text})))
        with patch.object(core.runtime, "connect", return_value=sbx), \
                patch.object(core.lifecycle, "assert_binding"), patch.object(core.lifecycle, "observe"), \
                patch.object(core, "runtime_state", return_value="paused"):
            core.connect("box", "for a test", info=self.info("box", state="paused"))
        folder = f"{core.RUN}/{self.endpoints.RENEWED}"
        self.assertEqual(sorted(written), sorted([f"{folder}/{n}{s}" for n in ("dev-pg", "temporal")
                                                  for s in (".pem", "-key.pem")] + [f"{folder}/renewed.json"]))
        self.assertGreater(json.loads(written[f"{folder}/renewed.json"])["dev-pg"], time.time() + 23 * 3600)
        self.assertEqual(self.endpoints.used_by(), {"dev-pg": ["owner/app"], "temporal": ["owner/app"]})


class GateTests(Setup, Installation):
    SETTINGS = {"repos": ["owner/app"]}

    def setUp(self):
        super().setUp()
        self.configure()
        from sbx_control import runtimes
        from sbx_control.catalog import attachments
        self.runtimes = runtimes
        self.enterContext(patch.object(attachments, "effective", return_value=[
            {"connections": {"server": "temporal"}}, {"connections": {"db": "neon"}}]))
        self.enterContext(patch.object(attachments, "connection", side_effect=lambda key: {
            "temporal": {"inputs": {"address": "10.0.0.5:7233"}},
            "neon": {"inputs": {"url": "postgresql://u:p@ep-x.neon.tech:5432/db"}}}.get(key)))

    def test_a_provider_runtime_is_refused_naming_the_command_until_the_repo_may_use_each(self):
        self.assertEqual(self.endpoints.missing("owner/app"), [
            ("the managed server dev_pg", "sbx endpoints add --server dev_pg"),
            ("the internal host 10.0.0.5:7233 (temporal)",
             "sbx endpoints add --name temporal --target 10.0.0.5:7233 --repo owner/app")])
        with provider(self.runtime):
            with self.assertRaises(self.runtimes.Refused) as refused:
                self.runtimes.choose("owner/app")
            self.assertIn("sbx endpoints add --server dev_pg; sbx endpoints add --name temporal --target "
                          "10.0.0.5:7233 --repo owner/app", str(refused.exception))
            self.endpoints.add(server="dev_pg", by="test")
            self.endpoints.add(name="temporal", target="10.0.0.5:7233", by="test")   # no grant yet
            self.assertEqual(self.endpoints.missing("owner/app"), [
                ("the internal host 10.0.0.5:7233 (temporal)", "sbx endpoints grant temporal owner/app")])
            with self.assertRaisesRegex(self.runtimes.Refused, "sbx endpoints grant temporal owner/app"):
                self.runtimes.choose("owner/app")
            self.endpoints.set_repos("temporal", ["owner/app"])
            self.assertEqual(self.endpoints.missing("owner/app"), [])
            self.assertEqual(self.runtimes.choose("owner/app"), self.runtime.default())

    def test_internal_ranges_are_kept_on_a_provider_only_installation_with_a_domain(self):
        from sbx_control import reach  # noqa: F401  (its settings checks)
        with provider(self.runtime):
            self.settings.update("network", {"internal_cidrs": ["10.0.1.0/24"]}, by="test")
            self.settings.update("domain", {"name": ""}, by="test")
            with self.assertRaisesRegex(self.settings.Invalid, "until a domain is connected"):
                self.settings.update("network", {"internal_cidrs": ["10.0.2.0/24"]}, by="test")

    def test_embed_needs_no_endpoint(self):
        self.assertEqual(self.runtimes.choose("owner/app"), "embed")


class RouteTests(Setup, Installation):
    """What a sandbox gets: the forwarder's address for the endpoints its repo may use, on a provider runtime."""

    def setUp(self):
        super().setUp()
        self.configure()
        self.endpoints.add(server="dev_pg", by="test")
        self.endpoints.add(name="temporal", target="10.0.0.5:7233", repos=["public/repo", "owner/app"], by="test")
        self.endpoints.add(name="secret-svc", target="10.0.0.9:443", repos=["other/repo"], by="test")
        from sbx_control.catalog.resources import Created
        self.created = {"db": Created(None, {"url": f"postgresql://sbx_box:pw@{PG}/sbx_box?sslmode=disable",
                                             "host": "172.29.50.10", "port": "5432", "user": "sbx_box"}),
                        "other": Created(None, {"url": "postgresql://u:p@172.29.50.100:5432/db"})}

    def test_a_provider_runtime_s_sandbox_gets_the_forwarder_s_address_for_what_its_repo_may_use(self):
        with provider(self.runtime):
            routes = self.endpoints.routes("fake", "owner/app")
        self.assertEqual([r["name"] for r in routes.rows], ["dev-pg", "temporal"])
        out = routes.outputs(self.created)
        self.assertEqual(out["db"].outputs, {"url": "postgresql://sbx_box:pw@127.0.0.1:17400/sbx_box?sslmode=disable",
                                             "host": "127.0.0.1", "port": "17400", "user": "sbx_box"})
        self.assertEqual(out["other"].outputs, self.created["other"].outputs)   # not an endpoint's target
        env = routes.env({"TEMPORAL_ADDRESS": "10.0.0.5:7233", "OTHER": "10.0.0.5:72330",
                          "SECRET": "http://10.0.0.9:443", "SBX_NAME": "box"})
        self.assertEqual(env, {"TEMPORAL_ADDRESS": "127.0.0.1:17401", "OTHER": "10.0.0.5:72330",
                               "SECRET": "http://10.0.0.9:443",   # another repo's: not routed
                               "SBX_NAME": "box", "SBX_ENDPOINT_TEMPORAL": "127.0.0.1:17401"})
        self.register("box")
        contribution = routes.contribution("box", "owner/app")
        files = contribution.runtime.files
        config = json.loads(files["config.json"].content)
        self.assertEqual([(e["name"], e["address"], e["port"], e["local_port"]) for e in config["endpoints"]], [
            ("dev-pg", f"dev-pg.{DOMAIN}", 443, 17400),
            ("temporal", f"temporal.{DOMAIN}", 443, 17401)])
        self.assertNotIn("secret-svc.pem", files)   # no certificate for an endpoint the repo may not use
        self.assertEqual(config["renew"]["url"], f"https://login.{DOMAIN}/api/sandbox/endpoint-certs")
        self.assertIn("BEGIN CERTIFICATE", files["dev-pg.pem"].content)
        self.assertEqual(files["dev-pg-key.pem"].mode, 0o600)
        self.assertEqual(self.endpoints.used_by(), {"dev-pg": ["owner/app"], "temporal": ["owner/app"]})
        self.assertNotIn("PRIVATE KEY", json.dumps(self.endpoints.describe()))

    def test_a_public_repo_may_use_what_it_was_granted_and_nothing_else(self):
        with provider(self.runtime):
            self.assertEqual([r["name"] for r in self.endpoints.routes("fake", "public/repo").rows], ["temporal"])
            self.assertEqual(self.endpoints.routes("fake", "nobody/repo").rows, [])

    def test_the_forwarder_s_ports_are_never_previews(self):
        from sbx_control import core, recipes
        listening = ("LISTEN 0 64 127.0.0.1:17400 0.0.0.0:*\nLISTEN 0 64 127.0.0.1:17401 0.0.0.0:*\n"
                     "LISTEN 0 511 0.0.0.0:3000 0.0.0.0:*\n")
        app = recipes.App("owner/app", Path("/nonexistent"), {})
        self.assertEqual(core.detected_ports(listening, app), {3000: False})
        self.assertFalse(any(core.detectable_port(p) for p in self.endpoints.LOCAL_PORTS))
        self.assertIsNone(core.suffix_port("-17400"))   # no preview host for it either

    def test_embed_keeps_its_values(self):
        routes = self.endpoints.routes("embed", "owner/app")
        self.assertEqual(routes.outputs(self.created), self.created)
        self.assertEqual(routes.env({"A": PG}), {"A": PG})
        self.assertIsNone(routes.contribution("box", "owner/app"))

    def test_the_runtime_write_carries_the_forwarder_and_starts_it(self):
        from sbx_control import contributions, core, recipes
        with provider(self.runtime):
            routes = self.endpoints.routes("fake", "owner/app")
        with patch.object(core.extensions, "contributions_for", return_value=[]):
            envs, layer = core.runtime_layer("box", recipes.App("owner/app", Path("/nonexistent"), {}),
                                             {"DATABASE_URL": f"x://{PG}/d"}, False, {}, routes)
        self.assertEqual(envs["DATABASE_URL"], "x://127.0.0.1:17400/d")
        self.assertIn("endpoints/config.json", layer.files)
        commands = []
        sbx = SimpleNamespace(
            commands=SimpleNamespace(run=lambda command, **kw: commands.append(command) or SimpleNamespace(stdout="k e")),
            files=SimpleNamespace(write=lambda *a, **kw: None))
        core.write_runtime(sbx, "box", envs, layer)
        self.assertTrue(any("forward ensure" in c and f"{contributions.EXT}/endpoints/config.json" in c
                            for c in commands), commands)


class ForwarderTests(Setup, Installation):
    """The guest's forwarder against TLS servers that, as nginx does, verify
    the client's certificate against their own endpoint's CA; and its renewal
    from sbx-control after a long pause."""

    def setUp(self):
        super().setUp()
        self.configure()
        sys.path.insert(0, str(GUEST))
        self.addCleanup(sys.path.remove, str(GUEST))
        import sbx_guest
        self.guest = sbx_guest
        self.enterContext(patch.object(sbx_guest, "say"))
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="sbx-forward-")))
        self.renewed = self.folder / "renewed"
        self.endpoints.add(server="dev_pg", by="test")
        self.endpoints.add(name="theirs", target="10.0.0.9:443", repos=["other/repo"], by="test")
        self.rows = {r["name"]: r for r in self.endpoints._rows()}
        server_ca, server_cert, server_key = self.server_identity()
        (self.folder / "server.pem").write_text(server_ca)
        (self.folder / "srv.pem").write_text(server_cert)
        (self.folder / "srv-key.pem").write_text(server_key)
        self.servers = {name: self.tls_server(row["ca_cert"]) for name, row in self.rows.items()}
        self.local = {name: self.free_port() for name in self.rows}
        self.forwarders = []

    def tearDown(self):
        for f in self.forwarders:
            f.close()
        super().tearDown()

    def server_identity(self):
        """A test CA and a certificate for localhost from it (the installation's certificate's stand-in)."""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        now = datetime.datetime.now(datetime.timezone.utc)

        def pem(cert):
            return cert.public_bytes(serialization.Encoding.PEM).decode()
        ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test server CA")])
        ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
              .serial_number(1).not_valid_before(now - datetime.timedelta(hours=1))
              .not_valid_after(now + datetime.timedelta(hours=1))
              .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
              .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                           data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                           crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
              .sign(ca_key, hashes.SHA256()))
        cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
                .issuer_name(ca_name).public_key(key.public_key()).serial_number(2)
                .not_valid_before(now - datetime.timedelta(hours=1)).not_valid_after(now + datetime.timedelta(hours=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
                .sign(ca_key, hashes.SHA256()))
        return pem(ca), pem(cert), key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()).decode()

    def tls_server(self, client_ca):
        """An echo server that, as nginx's server for one endpoint, accepts only clients from that endpoint's CA."""
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(self.folder / "srv.pem", self.folder / "srv-key.pem")
        context.load_verify_locations(cadata=client_ca)
        context.verify_mode = ssl.CERT_REQUIRED
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)

        def serve():
            while True:
                try:
                    raw, _ = listener.accept()
                except OSError:
                    return
                try:
                    conn = context.wrap_socket(raw, server_side=True)
                    conn.sendall(b"echo:" + conn.recv(1024))
                    conn.close()
                except (OSError, ssl.SSLError):
                    raw.close()
        threading.Thread(target=serve, daemon=True).start()
        return listener.getsockname()[1]

    def free_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def config(self, certs: dict, renew=None):
        """certs: endpoint name -> (cert, key, not_after) the sandbox holds."""
        endpoints = []
        for name, (cert, key, not_after) in certs.items():
            (self.folder / f"{name}.pem").write_text(cert)
            (self.folder / f"{name}-key.pem").write_text(key)
            endpoints.append({"name": name, "address": "localhost", "port": self.servers[name],
                              "local_port": self.local[name], "cert": f"{name}.pem", "key": f"{name}-key.pem",
                              "not_after": not_after})
        config = {"endpoints": endpoints, "trust": "server.pem", **({"renew": renew} if renew else {})}
        (self.folder / "config.json").write_text(json.dumps(config))
        return self.folder / "config.json"

    def forwarder(self, config):
        forwarder = self.guest.Forwarder(config, renewed=self.renewed)
        self.forwarders.append(forwarder)
        self.assertTrue(forwarder.reconcile())
        return forwarder

    def ask(self, name, payload=b"select 1"):
        try:
            with socket.create_connection(("127.0.0.1", self.local[name]), timeout=10) as client:
                client.sendall(payload)
                client.shutdown(socket.SHUT_WR)
                return client.recv(1024)
        except OSError:
            return b""

    def test_plain_text_on_loopback_reaches_the_service_through_mutual_tls(self):
        forwarder = self.forwarder(self.config({"dev-pg": self.endpoints.issue("box", self.rows["dev-pg"])}))
        self.assertEqual(self.ask("dev-pg"), b"echo:select 1")
        self.config({})   # the endpoint is gone from the config: so is its listener
        self.assertTrue(forwarder.reconcile())
        self.assertEqual(self.ask("dev-pg"), b"")
        (self.folder / "config.json").unlink()
        self.assertFalse(forwarder.reconcile())

    def test_repo_a_s_sandbox_can_t_open_an_endpoint_only_repo_b_may_use(self):
        with provider(self.runtime):
            self.assertEqual([r["name"] for r in self.endpoints.routes("fake", "owner/app").rows], ["dev-pg"])
        # Even pointed at it, its certificate (another endpoint's CA) is turned away there.
        mine = self.endpoints.issue("box", self.rows["dev-pg"])
        self.forwarder(self.config({"theirs": mine}))
        self.assertEqual(self.ask("theirs"), b"")
        theirs = self.endpoints.issue("their-box", self.rows["theirs"])
        self.config({"theirs": theirs})
        self.assertEqual(self.ask("theirs"), b"echo:select 1")   # repo B's own sandbox gets through

    def test_an_expired_certificate_after_a_long_pause_is_renewed_on_the_next_connection(self):
        self.sandbox_found()
        self.register("box")
        endpoints = self.endpoints
        asked = []

        class Callback(http.server.BaseHTTPRequestHandler):
            def do_POST(self):   # what POST /api/sandbox/endpoint-certs does (ApiTests covers the route)
                asked.append(self.path)
                name = endpoints.sandbox_of(self.headers.get("Authorization", ""))
                try:
                    body, status = json.dumps({"endpoints": endpoints.issue_for(name)}).encode(), 200
                except (LookupError, PermissionError, TypeError):
                    body, status = b"{}", 403
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Callback)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        renew = {**self.endpoints.renew_config("box"), "url": f"http://127.0.0.1:{server.server_address[1]}/certs"}
        expired = self.endpoints.issue("box", self.rows["dev-pg"], hours=-0.05)   # ran out while it slept
        self.forwarder(self.config({"dev-pg": expired}, renew=renew))
        self.assertEqual(self.ask("dev-pg"), b"echo:select 1")
        self.assertEqual(len(asked), 1)
        self.assertGreater(json.loads((self.renewed / "renewed.json").read_text())["dev-pg"], time.time() + 23 * 3600)
        self.assertEqual(self.ask("dev-pg"), b"echo:select 1")
        self.assertEqual(len(asked), 1)   # the renewed one is used, not renewed again
        (self.renewed / "renewed.json").unlink()
        self.bound["owner/app"] = []   # no longer attached: renewal refused, the expired one fails
        self.assertEqual(self.ask("dev-pg"), b"")
        self.assertEqual(len(asked), 2)
