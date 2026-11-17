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
        from sbx_control import endpoints, host_requests, hosted_services, runtime, settings
        from sbx_control.catalog import attachments
        from sbx_control.endpoints import certs, model, nginx
        self.endpoints, self.hosted, self.runtime, self.settings = endpoints, hosted_services, runtime, settings
        self.certs, self.model, self.nginx, self.host_requests = certs, model, nginx, host_requests
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
        host_requests._record_file("endpoints").unlink(missing_ok=True)   # the class's earlier tests' applies
        # Deliveries to running sandboxes: recorded here, run where a test asks for them (delivered()).
        self.deliveries = []
        self.enterContext(patch.object(model, "_background", side_effect=self.deliveries.append))
        self.effects()

    def delivered(self):
        """Run the deliveries recorded so far."""
        while self.deliveries:
            self.deliveries.pop(0)()

    def credential(self, name, repo="owner/app"):
        """What a runtime write gives the sandbox's forwarder: a credential of its own, bound."""
        import secrets
        credential = secrets.token_urlsafe(32)
        self.certs.bind(name, repo, credential)
        return credential

    def write(self, name="box", repo="owner/app", kind="fake"):
        from sbx_control import sandbox_layers
        return sandbox_layers.Write(name, repo, kind)

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
        self.host_requests.record("endpoints", {"revision": 1, "ok": True, "ok_revision": 1})
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
        self.certs.record("box", "owner/app", ["dev-pg"], time.time() + 3600)
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
        self.host_requests.record("endpoints", {"revision": 1, "ok": False, "message": "nginx said no"})
        found = self.client.get("/api/endpoints").json()
        self.assertEqual((found["endpoints"][0]["state"], found["endpoints"][0]["message"]), ("failed", "nginx said no"))
        self.assertEqual(found["failure"], "nginx said no")
        self.host_requests._record_file("endpoints").unlink()
        request = self.root / "run/apply/endpoints"
        request.parent.mkdir(parents=True, exist_ok=True)
        request.write_text("0\n")
        old = time.time() - self.host_requests.PICKUP_SECONDS - 5
        import os
        os.utime(request, (old, old))
        self.assertIn("did not pick up", self.client.get("/api/endpoints").json()["endpoints"][0]["message"])

    def test_only_admins(self):
        from sbx_control import auth, users
        users.set_password("someone@example.com", "test-only-password", by="test")   # a person, not an admin
        headers = {"Authorization": f"Bearer {auth.mint_token('someone@example.com', 'test')}"}
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/endpoints", headers=headers).status_code, 403)
        self.assertEqual(self.client.post("/api/endpoints", headers=headers, json={"server": "dev_pg"}).status_code, 403)
        self.assertEqual(self.client.put("/api/endpoints/x", headers=headers, json={"repos": []}).status_code, 403)

    def test_a_sandbox_renews_with_its_own_credential_and_only_while_it_may(self):
        from sbx_control import sandboxes
        self.sandbox_found()
        self.client.post("/api/endpoints", json={"server": "dev_pg"})
        self.client.post("/api/endpoints", json={"name": "temporal", "target": "10.0.0.5:7233", "repos": ["owner/app"]})
        self.client.post("/api/endpoints", json={"name": "other", "target": "10.0.0.6:80", "repos": ["other/repo"]})
        self.register("box")
        self.client.cookies.clear()
        mine = self.credential("box")   # what its runtime write gives it

        def renew(name="box", credential=None):
            return self.client.post("/api/sandbox/endpoint-certs",
                                    headers={"Authorization": f"Bearer {name}:{credential or mine}"})
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
        self.assertEqual(renew().status_code, 401)
        self.register("box")   # made again for the same repo: the old credential is not its
        self.assertEqual(renew().status_code, 401)

    def test_a_revoke_during_a_renewal_leaves_the_revoked_repo_nothing_the_new_ca_signed(self):
        from cryptography import x509
        from sbx_control.endpoints import sandbox
        self.sandbox_found()
        self.endpoints.add(name="temporal", target="10.0.0.5:7233", repos=["owner/app"], by="test")
        self.register("box")
        mine = self.credential("box")
        chosen, revoking = sandbox.routes, []

        def routes(kind, repo):   # the revoke lands once the renewal has chosen its endpoints
            found = chosen(kind, repo)
            if not revoking:
                revoking.append(threading.Thread(target=self.endpoints.revoke, args=("temporal", "owner/app")))
                revoking[0].start()
                revoking[0].join(1)
            return found
        with patch.object(sandbox, "routes", side_effect=routes):
            try:
                issued = {e["name"]: e["cert"] for e in self.endpoints.issue_for(f"Bearer box:{mine}")}
            except PermissionError:
                issued = {}
        revoking[0].join()
        ca = x509.load_pem_x509_certificate(self.model.find("temporal").ca_cert.encode())
        if "temporal" in issued:
            with self.assertRaises(Exception):   # from the CA before the revoke: nginx no longer takes it
                x509.load_pem_x509_certificate(issued["temporal"].encode()).verify_directly_issued_by(ca)

    def test_a_name_made_again_for_another_repo_doesn_t_take_the_old_credential(self):
        repos = self.sandbox_found()
        self.client.post("/api/endpoints", json={"name": "theirs", "target": "10.0.0.6:80", "repos": ["other/repo"]})
        self.register("box")
        old = self.credential("box")   # box of owner/app, deleted since
        repos["box"] = "other/repo"
        new = self.credential("box", "other/repo")  # box made again, of other/repo
        self.client.cookies.clear()
        headers = {"Authorization": f"Bearer box:{old}"}
        self.assertEqual(self.client.post("/api/sandbox/endpoint-certs", headers=headers).status_code, 401)
        headers = {"Authorization": f"Bearer box:{new}"}
        self.assertEqual([e["name"] for e in self.client.post("/api/sandbox/endpoint-certs", headers=headers).json()
                          ["endpoints"]], ["theirs"])
        from sbx_control import state
        with state.db() as conn:   # the old instance's record, as if a write raced the new create: refused, not B's
            conn.execute("update endpoint_credentials set repo = 'owner/app' where sandbox = 'box'")
        self.assertEqual(self.client.post("/api/sandbox/endpoint-certs", headers=headers).status_code, 404)
        with state.db() as conn:
            stored = conn.execute("select digest from endpoint_credentials").fetchone()[0]
        self.assertNotIn(new, stored)   # only its digest is kept

    def test_a_push_that_a_revoke_overtakes_ends_with_certificates_from_the_new_ca(self):
        from cryptography import x509
        from sbx_control.endpoints import sandbox
        self.sandbox_found()
        self.endpoints.add(name="temporal", target="10.0.0.5:7233", repos=["owner/app", "other/repo"], by="test")
        self.register("box")
        bundle, written = f"{sandbox.RENEWED}/.push/temporal.bundle.pem", []

        def write(path, text, **kw):   # the revoke lands while the push writes what the old CA signed
            if path == bundle and not written:
                self.endpoints.revoke("temporal", "other/repo")
            if path == bundle:
                written.append(text)
        vm = SimpleNamespace(commands=SimpleNamespace(run=lambda c, **kw: SimpleNamespace(stdout="")),
                             files=SimpleNamespace(write=write))
        with patch.object(self.model, "deliver"):
            self.assertTrue(sandbox.push(vm, self.info("box")))
        ca = x509.load_pem_x509_certificate(self.model.find("temporal").ca_cert.encode())
        x509.load_pem_x509_certificate(written[-1].encode()).verify_directly_issued_by(ca)   # still owner/app's

    def test_a_name_made_again_between_a_renewal_s_check_and_its_issue_gives_it_nothing(self):
        from sbx_control import endpoints
        repos = self.sandbox_found()
        self.client.post("/api/endpoints", json={"name": "theirs", "target": "10.0.0.6:80", "repos": ["other/repo"]})
        self.register("box")
        old = self.credential("box")
        checked = endpoints.sandbox_of

        def remade(authorization):   # the old credential passes, then box is deleted and made again for another repo
            name = checked(authorization)
            repos["box"] = "other/repo"
            self.credential("box", "other/repo")
            return name
        self.client.cookies.clear()
        with patch.object(endpoints, "sandbox_of", side_effect=remade):
            response = self.client.post("/api/sandbox/endpoint-certs", headers={"Authorization": f"Bearer box:{old}"})
        self.assertEqual(response.status_code, 404)   # not other/repo's certificates and keys

    def test_one_repo_is_granted_and_revoked_at_a_time(self):
        self.client.post("/api/endpoints", json={"name": "temporal", "target": "10.0.0.5:7233"})
        response = self.client.post("/api/endpoints/temporal/repos", json={"repo": "Owner/App"})
        self.assertEqual((response.status_code, response.json()["repos"]), (200, ["owner/app"]))
        self.client.post("/api/endpoints/temporal/repos", json={"repo": "other/app"})
        response = self.client.delete("/api/endpoints/temporal/repos/owner/app")
        self.assertEqual((response.status_code, response.json()["repos"]), (200, ["other/app"]))
        response = self.client.delete("/api/endpoints/temporal/repos/owner/app")
        self.assertEqual(response.status_code, 404)
        self.assertIn("owner/app is not granted the endpoint temporal", response.json()["detail"])
        self.client.post("/api/endpoints", json={"server": "dev_pg"})
        response = self.client.post("/api/endpoints/dev-pg/repos", json={"repo": "owner/app"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("attach the server to the repo instead", response.json()["detail"])


class HostTests(Setup, Installation):
    """nginx's stream configuration and the root side's apply."""

    def setUp(self):
        super().setUp()
        self.configure()
        self.tls = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="sbx-tls-")))
        (self.tls / "nginx").mkdir()
        from sbx_control import paths
        self.enterContext(patch.object(paths, "tls", return_value=self.tls))

    def stream(self):
        return self.nginx.stream_config(self.model.routable(), self.model.domain())

    def test_443_s_front_sends_endpoint_names_to_their_servers_and_the_rest_to_https(self):
        from sbx_control import tls_front
        front = self.stream()
        self.assertIn("map $ssl_preread_server_name $sbx_backend {\n  default 127.0.0.1:8442;\n}", front)
        self.assertIn("  listen 443;\n  ssl_preread on;\n  proxy_pass $sbx_backend;\n  proxy_protocol on;", front)
        self.assertNotIn("8441", front)   # no endpoint: every name goes to the HTTPS server, as before
        self.nginx.write_nginx(self.tls / "nginx")
        self.assertEqual(sorted(p.name for p in (self.tls / "nginx").iterdir()), ["endpoints.stream"])
        self.endpoints.add(server="dev_pg", by="test")
        self.endpoints.add(name="temporal", target="10.0.0.5:7233", repos=["owner/app"], by="test")
        text = self.stream()
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
        self.nginx.write_nginx(self.tls / "nginx")
        cas = {p.name: p.read_text() for p in (self.tls / "nginx").glob("endpoint-*-ca.pem")}
        self.assertEqual(sorted(cas), ["endpoint-dev-pg-ca.pem", "endpoint-temporal-ca.pem"])
        self.assertNotEqual(cas["endpoint-dev-pg-ca.pem"], cas["endpoint-temporal-ca.pem"])   # a CA each
        self.assertNotIn("PRIVATE KEY", "".join(cas.values()))
        # The HTTPS server learns the client's address from the PROXY protocol, from the front only.
        self.assertIn("set_real_ip_from 127.0.0.1;\n  real_ip_header proxy_protocol;", tls_front.MAIN_CONF)
        # A reload's old workers end their sessions soon: a revoked or removed endpoint's go with them.
        self.assertIn("worker_shutdown_timeout 30s;", tls_front.MAIN_CONF)
        self.assertIn("proxy_timeout 1h;", servers[1])
        self.assertIn("stream {\n  include /etc/nginx/conf.d/*.stream;", tls_front.MAIN_CONF)
        # No port opens for endpoints: the firewall's ports are the settings' alone.
        self.assertIn("public_ports='22 443 53'", self.settings.shell_lines(self.settings.current(), {}))
        self.endpoints.remove("temporal")
        self.nginx.write_nginx(self.tls / "nginx")
        self.assertEqual([p.name for p in (self.tls / "nginx").glob("endpoint-*-ca.pem")], ["endpoint-dev-pg-ca.pem"])
        self.settings.update("domain", {"name": ""}, by="test")
        self.assertEqual(self.stream(), front)   # without a domain: the front alone
        self.nginx.write_nginx(self.tls / "nginx")
        self.assertEqual(list((self.tls / "nginx").glob("endpoint-*-ca.pem")), [])

    def test_an_internal_host_outside_narrowed_ranges_is_not_routed_until_fixed(self):
        self.endpoints.add(name="temporal", target="10.0.0.5:7233", repos=["owner/app"], by="test")
        self.settings.update("network", {"internal_cidrs": ["10.9.0.0/24"]}, by="test")
        found = self.endpoints.one("temporal")
        self.assertEqual(found["state"], "failed")
        self.assertIn("10.0.0.5:7233 is outside network.internal_cidrs now", found["message"])
        self.assertNotIn("temporal", self.stream())
        with provider(self.runtime):
            self.assertEqual(self.endpoints.routes("fake", "owner/app").rows, ())
        self.settings.update("network", {"internal_cidrs": ["10.0.0.0/24"]}, by="test")
        self.assertIn(f"temporal.{DOMAIN} 127.0.0.1:8441;", self.stream())
        self.assertNotEqual(self.endpoints.one("temporal")["state"], "failed")

    def test_endpoint_names_and_sandbox_slugs_never_share_a_host(self):
        from sbx_control import core, sandboxes
        self.register("app")
        for name in ("app", "app-5432", "app-api", "3000-abc"):
            with self.subTest(name=name), self.assertRaises(self.endpoints.Conflict):
                self.endpoints.add(name=name, target="10.0.0.5:7233", by="test")
        self.endpoints.add(name="cache", target="10.0.0.5:6379", by="test")
        self.endpoints.add(name="web-cache", target="10.0.0.6:6379", by="test")
        for slug in ("cache", "web"):
            with self.subTest(slug=slug), self.assertRaisesRegex(core.SandboxError, "is an endpoint"):
                sandboxes.check_slug(slug, "new")
        self.assertEqual(sandboxes.check_slug("cache2", "new"), "cache2")

    def test_a_name_checked_before_the_other_kind_was_recorded_is_checked_again_with_its_record(self):
        from sbx_control import core, sandboxes
        self.endpoints.add(name="demo", target="10.0.0.5:7233", by="test")   # after the sandbox's check_slug
        with self.assertRaisesRegex(core.SandboxError, "is an endpoint"):
            self.register("demo")
        self.assertIsNone(sandboxes.registered_slug("demo"))
        held = []

        def check(name):   # the endpoint's name check runs holding the lock register takes
            import fcntl
            with (self.model.paths.state_dir() / "endpoints.lock").open("a") as other:
                try:
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    held.append(name)
        with patch.object(self.model, "_check_name", side_effect=check):
            self.endpoints.add(name="other", target="10.0.0.6:7233", by="test")
        self.assertEqual(held, ["other"])

    def fake_nginx(self, code):
        script = self.tls / "fake-nginx"
        script.write_text(f"#!/bin/sh\necho 'nginx: [emerg] details for the journal'\nexit {code}\n")
        script.chmod(0o755)
        from sbx_control import tls_front
        (self.tls / "nginx.conf").write_text(tls_front.MAIN_CONF)
        return patch.object(self.nginx, "NGINX", str(script))

    def test_apply_records_its_outcome_and_leaves_the_firewall_alone(self):
        self.endpoints.add(server="dev_pg", by="test")
        real = self.nginx.subprocess.run
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return real(argv, **kwargs)
        with self.fake_nginx(0), patch.object(self.nginx.subprocess, "run", side_effect=run):
            self.assertEqual(self.endpoints.apply_host(), 0)
        self.assertEqual(calls, [[str(self.tls / "fake-nginx")]])
        self.assertEqual(self.endpoints.one("dev-pg")["state"], "on")
        self.assertIn(f"dev-pg.{DOMAIN} 127.0.0.1:8441;", (self.tls / "nginx/endpoints.stream").read_text())

    def test_a_refused_configuration_keeps_the_one_before_and_keeps_nginx_s_words_out_of_the_answer(self):
        (self.tls / "nginx/endpoints.stream").write_text("# before\n")
        self.endpoints.add(server="dev_pg", by="test")
        with self.fake_nginx(1), patch("sys.stderr"):
            self.assertEqual(self.endpoints.apply_host(), 1)
        self.assertEqual((self.tls / "nginx/endpoints.stream").read_text(), "# before\n")
        self.assertEqual(list((self.tls / "nginx").glob("endpoint-*-ca.pem")), [])
        found = self.endpoints.one("dev-pg")
        self.assertEqual(found["state"], "failed")
        self.assertIn("the one before stays", found["message"])
        self.assertNotIn("emerg", found["message"])


    def test_an_apply_that_can_t_run_nginx_keeps_the_one_before(self):
        (self.tls / "nginx/endpoints.stream").write_text("# before\n")
        self.endpoints.add(server="dev_pg", by="test")
        with self.fake_nginx(0), patch.object(self.nginx, "NGINX", str(self.tls / "missing")), patch("sys.stderr"):
            self.assertEqual(self.endpoints.apply_host(), 1)
        self.assertEqual((self.tls / "nginx/endpoints.stream").read_text(), "# before\n")
        self.assertEqual(list((self.tls / "nginx").glob("endpoint-*-ca.pem")), [])
        self.assertEqual(self.endpoints.one("dev-pg")["state"], "failed")

class CertificateTests(Setup, Installation):
    def setUp(self):
        super().setUp()
        self.configure()
        self.endpoints.add(server="dev_pg", by="test")
        self.endpoints.add(name="temporal", target="10.0.0.5:7233", repos=["owner/app"], by="test")
        self.rows = {r.name: r for r in self.endpoints.rows()}

    def test_a_sandbox_s_certificate_is_short_for_clients_only_and_from_its_endpoint_s_ca(self):
        from cryptography import x509
        from cryptography.x509.oid import ExtendedKeyUsageOID
        cert_pem, key_pem, not_after = self.endpoints.issue("box", "dev-pg")
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
        own = x509.load_pem_x509_certificate(self.rows["dev-pg"].ca_cert.encode())
        other = x509.load_pem_x509_certificate(self.rows["temporal"].ca_cert.encode())
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

    def test_the_keeper_pushes_new_certificates_to_running_sandboxes_whose_run_out_soon(self):
        from sbx_control import runtime
        self.sandbox_found()
        for name in ("soon", "later", "paused"):
            self.register(name)
        self.certs.record("soon", "owner/app", ["dev-pg"], time.time() + 3600)
        self.certs.record("paused", "owner/app", ["dev-pg"], time.time() + 3600)
        self.certs.record("later", "owner/app", ["dev-pg"], time.time() + 20 * 3600)
        self.certs.record("gone", "owner/app", ["dev-pg"], time.time())   # not registered
        self.assertEqual(self.certs.due(), ["paused", "soon"])
        written = {}
        vm = SimpleNamespace(commands=SimpleNamespace(run=lambda c, **kw: SimpleNamespace(stdout="")),
                             files=SimpleNamespace(write=lambda path, text, **kw: written.update({path: text})))
        infos = [self.info(n) for n in ("soon", "later", "paused")]
        with patch.object(runtime, "connect", return_value=vm) as connect:
            self.endpoints.LAYER.round(infos, {"soon", "later"})   # paused: when it resumes
        self.assertEqual([c.args[0] for c in connect.call_args_list], ["id-soon"])
        self.assertIn(f"{self.endpoints.sandbox.RENEWED}/.push/dev-pg.bundle.pem", written)
        self.assertEqual(self.certs.due(), ["paused"])

    def test_sbx_pushes_fresh_certificates_when_it_resumes_a_sandbox(self):
        from sbx_control import core
        self.sandbox_found()
        self.register("box")
        written, commands = {}, []
        sbx = SimpleNamespace(
            commands=SimpleNamespace(run=lambda c, **kw: commands.append(c) or SimpleNamespace(stdout="")),
            files=SimpleNamespace(write=lambda path, text, **kw: written.update({path: text})))
        with patch.object(core.runtime, "connect", return_value=sbx), \
                patch.object(core.lifecycle, "assert_binding"), patch.object(core.lifecycle, "observe"), \
                patch.object(core, "runtime_state", return_value="paused"):
            core.connect("box", "for a test", info=self.info("box", state="paused"))
        staging = f"{self.endpoints.sandbox.RENEWED}/.push"
        # Key and certificate in one file each, staged, then moved into place whole.
        self.assertEqual(sorted(written), [f"{staging}/bundles.json", f"{staging}/dev-pg.bundle.pem",
                                           f"{staging}/temporal.bundle.pem"])
        self.assertIn("PRIVATE KEY", written[f"{staging}/dev-pg.bundle.pem"])
        self.assertIn("BEGIN CERTIFICATE", written[f"{staging}/dev-pg.bundle.pem"])
        moved = next(i for i, c in enumerate(commands) if "mv -f" in c and "bundles.json" in c)
        # A revoked endpoint's bundle goes; the forwarder runs.
        self.assertIn("! -name dev-pg.bundle.pem ! -name temporal.bundle.pem -delete", commands[moved])
        self.assertGreater(next(i for i, c in enumerate(commands) if "forward ensure" in c), moved)
        self.assertGreater(json.loads(written[f"{staging}/bundles.json"])["dev-pg"], time.time() + 23 * 3600)
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

    def test_a_disabled_attachment_s_connection_needs_no_endpoint(self):
        from sbx_control.catalog import attachments
        with patch.object(attachments, "effective", return_value=[
                {"connections": {"server": "temporal"}, "disabled": True}]):
            self.assertEqual(self.endpoints.missing("owner/app"), [
                ("the managed server dev_pg", "sbx endpoints add --server dev_pg")])

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
        self.assertEqual([r.name for r in routes.rows], ["dev-pg", "temporal"])
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
        write = self.write()
        with provider(self.runtime):
            files = self.endpoints.LAYER.contribution(write).runtime.files
        config = json.loads(files["config.json"].content)
        self.assertEqual([(e["name"], e["address"], e["port"], e["local_port"]) for e in config["endpoints"]], [
            ("dev-pg", f"dev-pg.{DOMAIN}", 443, 17400),
            ("temporal", f"temporal.{DOMAIN}", 443, 17401)])
        self.assertEqual(config["renew"]["url"], f"https://login.{DOMAIN}/api/sandbox/endpoint-certs")
        self.assertEqual(sorted(files), ["config.json"])   # no certificate in the config: they come after the write
        # Nothing is recorded until the runtime is written: the credential is bound after.
        self.assertIsNone(self.certs.sandbox_of(f"Bearer box:{config['renew']['credential']}"))
        self.assertEqual(self.endpoints.used_by(), {})
        written = {}
        vm = SimpleNamespace(commands=SimpleNamespace(run=lambda c, **kw: SimpleNamespace(stdout="")),
                             files=SimpleNamespace(write=lambda path, text, **kw: written.update({path: text})))
        with provider(self.runtime):
            self.endpoints.LAYER.after_write(write, vm)
        self.assertEqual(self.certs.sandbox_of(f"Bearer box:{config['renew']['credential']}"), "box")
        self.assertEqual(self.endpoints.used_by(), {"dev-pg": ["owner/app"], "temporal": ["owner/app"]})
        self.assertIn(f"{self.endpoints.sandbox.RENEWED}/.push/temporal.bundle.pem", written)
        self.assertNotIn("PRIVATE KEY", json.dumps(self.endpoints.describe()))

    def test_a_public_repo_may_use_what_it_was_granted_and_nothing_else(self):
        with provider(self.runtime):
            self.assertEqual([r.name for r in self.endpoints.routes("fake", "public/repo").rows], ["temporal"])
            self.assertEqual(self.endpoints.routes("fake", "nobody/repo").rows, ())

    def test_running_sandboxes_get_a_grant_a_revoke_an_add_and_a_removal_without_a_restart(self):
        from sbx_control import sandboxes
        self.deliveries.clear()
        with patch.object(sandboxes, "deliver_env", return_value={"running": 1, "paused": 0}) as deliver:
            before = next(r for r in self.endpoints.rows() if r.name == "temporal")
            self.effects()
            self.endpoints.set_repos("temporal", ["owner/app", "new/repo"])          # public/repo revoked, new/repo granted
            # A revoke makes the endpoint's CA anew: nginx gets it, and every sandbox still granted new certificates.
            self.delivered()
            self.assertEqual(sorted(c.args[0] for c in deliver.call_args_list), ["new/repo", "owner/app", "public/repo"])
            after = next(r for r in self.endpoints.rows() if r.name == "temporal")
            self.assertNotEqual(after.ca_cert, before.ca_cert)
            self.assertGreater(after.revision, before.revision)
            self.assertEqual(self.effects(), ["endpoints"])
            for call in deliver.call_args_list:
                self.assertEqual((call.args[1], call.kwargs), (["SBX_ENDPOINT_TEMPORAL"], {"restart": False}))
            deliver.reset_mock()
            self.endpoints.grant("temporal", "third/repo")                          # a grant alone: no new CA
            self.delivered()
            self.assertEqual([c.args[0] for c in deliver.call_args_list], ["third/repo"])
            self.assertEqual(next(r for r in self.endpoints.rows() if r.name == "temporal").ca_cert,
                             after.ca_cert)
            self.endpoints.revoke("temporal", "third/repo")
            self.delivered()
            deliver.reset_mock()
            self.endpoints.add(name="cache", target="10.0.0.20:6379", repos=["owner/app"], by="test")
            self.delivered()
            self.assertEqual([c.args[:2] for c in deliver.call_args_list], [("owner/app", ["SBX_ENDPOINT_CACHE"])])
            deliver.reset_mock()
            self.endpoints.remove("cache")
            self.delivered()
            self.assertEqual([c.args[:2] for c in deliver.call_args_list], [("owner/app", ["SBX_ENDPOINT_CACHE"])])
            deliver.reset_mock()
            self.endpoints.set_repos("temporal", ["owner/app", "new/repo"])          # nothing changed: nothing to bring
            self.delivered()
            deliver.assert_not_called()
        with provider(self.runtime):   # what the revoked repo's sandbox is written with now
            routes = self.endpoints.routes("fake", "public/repo")
            self.assertIsNone(self.endpoints.LAYER.contribution(self.write("box", "public/repo")))
        self.assertEqual(routes.env({"A": "1"}), {"A": "1"})

    def test_a_restart_writes_the_runtime_again(self):
        from sbx_control import core
        info = self.info("box")
        with patch.object(core, "connect", return_value=("vm", info)), \
                patch.object(core, "restore_runtime", return_value=False), \
                patch.object(core, "app_of", return_value=SimpleNamespace(repo="owner/app")), \
                patch.object(core, "_refresh_env") as refresh, patch.object(core, "_bring_up") as bring_up:
            core._restart("box", SimpleNamespace(configure_workspace=lambda repo: None))
        refresh.assert_called_once_with("vm", info, False)   # no restart of its own: the restart follows
        bring_up.assert_called_once()

    def test_the_forwarder_s_ports_are_never_previews_and_other_ports_stay_detectable(self):
        from sbx_control import core, recipes, routing
        listening = ("LISTEN 0 64 127.0.0.1:17400 0.0.0.0:*\nLISTEN 0 64 127.0.0.1:17401 0.0.0.0:*\n"
                     "LISTEN 0 511 0.0.0.0:3000 0.0.0.0:*\nLISTEN 0 511 0.0.0.0:17799 0.0.0.0:*\n"
                     "LISTEN 0 511 0.0.0.0:17800 0.0.0.0:*\n")
        app = recipes.App("owner/app", Path("/nonexistent"), {})
        vm = SimpleNamespace(commands=SimpleNamespace(run=lambda c, **kw: SimpleNamespace(stdout=listening)))
        with patch.object(routing.runtime, "connect", return_value=vm), \
                patch.object(routing.addressing, "mode", return_value="domain"):
            # The endpoints' range (17400-17799) is theirs on every sandbox, with or without endpoints.
            self.assertEqual(routing.detect_ports("sbx-id", app), {3000: False, 17800: False})
        self.assertFalse(core.detectable_port(17400))
        self.assertTrue(core.detectable_port(17800))

    def test_endpoints_take_local_ports_nothing_else_in_the_sandbox_listens_on(self):
        with provider(self.runtime):
            routes = self.endpoints.routes("fake", "owner/app")
        placed = routes.placed({17400, 17402})
        self.assertEqual([(r.name, r.local_port) for r in placed.rows], [("dev-pg", 17401), ("temporal", 17403)])
        self.assertEqual(placed.env({"A": f"x://{PG}/d"})["A"], "x://127.0.0.1:17401/d")
        with self.assertRaisesRegex(RuntimeError, "no free port in 17400-17799"):   # placed only in the range
            routes.placed(set(range(17401, 17800)))
        # What listens, and the forwarder's own ports among it.
        output = ("LISTEN 0 64 127.0.0.1:17400 0.0.0.0:*\nLISTEN 0 64 0.0.0.0:17401 0.0.0.0:*\n---sbx-forwarder---\n"
                  + json.dumps({"endpoints": [{"local_port": 17400}]}))
        vm = SimpleNamespace(commands=SimpleNamespace(run=lambda c, **kw: SimpleNamespace(stdout=output)))
        self.assertEqual(self.endpoints.sandbox.listening_and_own(vm), ({17400, 17401}, {17400}))

    def test_an_address_in_the_recipe_s_env_is_advice_not_a_refusal(self):
        from sbx_control import recipes, sandboxes, state
        self.enterContext(patch.object(recipes, "load", return_value=SimpleNamespace(
            spec={"env": {"CACHE": "redis://10.0.0.30:6379"}}, size={}, repo="owner/app")))
        self.bound["owner/app"] = []
        self.assertEqual(self.endpoints.missing("owner/app"), [])
        self.assertEqual(self.endpoints.missing("owner/app", sources=("env",)), [
            ("the internal host 10.0.0.30:6379 named in sbx.yaml's env",
             "sbx endpoints add --name NAME --target 10.0.0.30:6379 --repo owner/app")])
        self.register("box")
        with provider(self.runtime):
            sandboxes.advise_endpoints("box", "owner/app", "fake")
        with state.db() as conn:
            said = [r[0] for r in conn.execute("select detail from events where sandbox = 'box' and "
                                               "kind = 'endpoint suggested'")]
        self.assertEqual(len(said), 1)
        self.assertIn("sbx endpoints add --name NAME --target 10.0.0.30:6379 --repo owner/app", said[0])

    def test_embed_keeps_its_values(self):
        routes = self.endpoints.routes("embed", "owner/app")
        self.assertEqual(routes.outputs(self.created), self.created)
        self.assertEqual(routes.env({"A": PG}), {"A": PG})
        self.assertIsNone(self.endpoints.LAYER.contribution(self.write(kind="embed")))

    def test_the_runtime_write_carries_the_forwarder_and_starts_it(self):
        from sbx_control import contributions, core, recipes
        self.register("box")
        write = self.write()
        with provider(self.runtime), patch.object(core.extensions, "contributions_for", return_value=[]):
            envs, layer = core.runtime_layer("box", recipes.App("owner/app", Path("/nonexistent"), {}),
                                             {"DATABASE_URL": f"x://{PG}/d"}, False, {}, write)
        self.assertEqual(envs["DATABASE_URL"], "x://127.0.0.1:17400/d")
        self.assertIn("endpoints/config.json", layer.files)
        commands, written = [], {}
        sbx = SimpleNamespace(
            commands=SimpleNamespace(run=lambda command, **kw: commands.append(command) or SimpleNamespace(stdout="k e")),
            files=SimpleNamespace(write=lambda path, text, **kw: written.update({path: text})))
        with provider(self.runtime):
            core.write_runtime(sbx, "box", envs, layer, write=write)
        published = next(i for i, c in enumerate(commands) if f"mv {core.RUN}/.runtime-staging/ext" in c)
        # After the write: its certificates, then the forwarder.
        bundles = next(i for i, c in enumerate(commands) if "bundles.json" in c and "mv -f" in c)
        ensure = next(i for i, c in enumerate(commands)
                      if "forward ensure" in c and f"{contributions.EXT}/endpoints/config.json" in c)
        self.assertLess(published, bundles)
        self.assertLess(bundles, ensure)
        self.assertIn(f"{self.endpoints.sandbox.RENEWED}/.push/dev-pg.bundle.pem", written)


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
        self.rows = {r.name: r for r in self.endpoints.rows()}
        server_ca, server_cert, server_key = self.server_identity()
        (self.folder / "server.pem").write_text(server_ca)
        (self.folder / "srv.pem").write_text(server_cert)
        (self.folder / "srv-key.pem").write_text(server_key)
        self.servers = {name: self.tls_server(row.ca_cert) for name, row in self.rows.items()}
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
        """certs: endpoint name -> (cert, key, not_after) the sandbox holds, as sbx writes them (RENEWED)."""
        self.renewed.mkdir(exist_ok=True)
        for name, (cert, key, _) in certs.items():
            (self.renewed / f"{name}.bundle.pem").write_text(key + cert)
        (self.renewed / "bundles.json").write_text(json.dumps({n: v[2] for n, v in certs.items()}))
        endpoints = [{"name": name, "address": "localhost", "port": self.servers[name], "local_port": self.local[name]}
                     for name in certs]
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
                return client.recv(1024)
        except OSError:
            return b""

    def test_plain_text_on_loopback_reaches_the_service_through_mutual_tls(self):
        forwarder = self.forwarder(self.config({"dev-pg": self.endpoints.issue("box", "dev-pg")}))
        self.assertEqual(self.ask("dev-pg"), b"echo:select 1")
        self.config({})   # the endpoint is gone from the config: so is its listener
        self.assertTrue(forwarder.reconcile())
        self.assertEqual(self.ask("dev-pg"), b"")
        (self.folder / "config.json").unlink()
        self.assertFalse(forwarder.reconcile())

    def test_repo_a_s_sandbox_can_t_open_an_endpoint_only_repo_b_may_use(self):
        with provider(self.runtime):
            self.assertEqual([r.name for r in self.endpoints.routes("fake", "owner/app").rows], ["dev-pg"])
        # Even pointed at it, its certificate (another endpoint's CA) is turned away there.
        mine = self.endpoints.issue("box", "dev-pg")
        self.forwarder(self.config({"theirs": mine}))
        self.assertEqual(self.ask("theirs"), b"")
        theirs = self.endpoints.issue("their-box", "theirs")
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
                try:
                    found = endpoints.issue_for(self.headers.get("Authorization", ""))
                    body, status = json.dumps({"endpoints": found}).encode(), 200
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
        renew = {"sandbox": "box", "credential": self.credential("box"), "url": f"http://127.0.0.1:{server.server_address[1]}/certs"}
        expired = self.endpoints.issue("box", "dev-pg", hours=-0.05)   # ran out while it slept
        self.forwarder(self.config({"dev-pg": expired}, renew=renew))
        self.assertEqual(self.ask("dev-pg"), b"echo:select 1")
        self.assertEqual(len(asked), 1)
        self.assertGreater(json.loads((self.renewed / "bundles.json").read_text())["dev-pg"], time.time() + 23 * 3600)
        self.assertEqual(self.ask("dev-pg"), b"echo:select 1")
        self.assertEqual(len(asked), 1)   # the renewed one is used, not renewed again
        self.config({"dev-pg": expired}, renew=renew)   # run out again
        self.bound["owner/app"] = []   # no longer attached: renewal refused, the expired one fails
        self.assertEqual(self.ask("dev-pg"), b"")
        self.assertEqual(len(asked), 2)

    def callback(self, answer):
        """sbx-control's renewal route as a local HTTP server: answer() -> (status, body); the calls it got."""
        asked = []

        class Callback(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                asked.append(time.monotonic())
                status, body = answer()
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
        return f"http://127.0.0.1:{server.server_address[1]}/certs", asked

    def test_a_renewal_answered_before_sbx_pushed_newer_certificates_leaves_them(self):
        older, pushed = self.endpoints.issue("box", "dev-pg"), self.endpoints.issue("box", "dev-pg")
        self.renewed.mkdir()

        def answer():   # sbx pushes while the answer is on its way
            (self.renewed / "dev-pg.bundle.pem").write_text(pushed[1] + pushed[0])
            (self.renewed / "bundles.json").write_text(json.dumps({"dev-pg": pushed[2]}))
            return 200, json.dumps({"endpoints": [{"name": "dev-pg", "cert": older[0], "key": older[1],
                                                   "not_after": older[2]}]}).encode()
        url, _ = self.callback(answer)
        self.guest.renew({"renew": {"url": url, "sandbox": "box", "credential": "c"}}, self.folder, self.renewed)
        self.assertEqual((self.renewed / "dev-pg.bundle.pem").read_text(), pushed[1] + pushed[0])

    def test_a_certificate_still_valid_is_used_while_its_renewal_runs_in_the_background(self):
        release = threading.Event()
        fresh = self.endpoints.issue("box", "dev-pg")

        def slow():
            release.wait(10)
            return 200, json.dumps({"endpoints": [{"name": "dev-pg", "cert": fresh[0], "key": fresh[1],
                                                   "not_after": fresh[2]}]}).encode()
        url, asked = self.callback(slow)
        soon = self.endpoints.issue("box", "dev-pg", hours=0.5)   # within the hour before it runs out
        self.forwarder(self.config({"dev-pg": soon}, renew={"url": url, "sandbox": "box", "credential": "c"}))
        began = time.monotonic()
        self.assertEqual(self.ask("dev-pg"), b"echo:select 1")
        self.assertEqual(self.ask("dev-pg"), b"echo:select 1")
        self.assertLess(time.monotonic() - began, 5)   # neither waited for the renewal
        release.set()
        self.assertTrue(self.forwarders[-1].renewer.wait(10))
        self.assertEqual(len(asked), 1)   # one renewal for both
        stored = (self.renewed / "dev-pg.bundle.pem").read_text()
        self.assertIn("PRIVATE KEY", stored)   # key and certificate in one file, written whole
        self.assertIn("BEGIN CERTIFICATE", stored)
        self.assertEqual(oct((self.renewed / "dev-pg.bundle.pem").stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.ask("dev-pg"), b"echo:select 1")
        self.assertEqual(len(asked), 1)   # the renewed one lasts a day

    def test_a_failed_renewal_backs_off(self):
        url, asked = self.callback(lambda: (403, b"{}"))
        soon = self.endpoints.issue("box", "dev-pg", hours=0.5)
        self.forwarder(self.config({"dev-pg": soon}, renew={"url": url, "sandbox": "box", "credential": "c"}))
        self.assertEqual(self.ask("dev-pg"), b"echo:select 1")
        self.assertTrue(self.forwarders[-1].renewer.wait(10))
        for _ in range(3):
            self.assertEqual(self.ask("dev-pg"), b"echo:select 1")   # the one it has, while it lasts
        self.assertEqual(len(asked), 1)   # not again before RENEW_RETRY
        self.assertGreaterEqual(self.forwarders[-1].renewer.next_try - time.monotonic(), self.guest.RENEW_RETRY - 5)

    def recorded_endpoint(self, behave):
        """An endpoint's TLS server (dev-pg's CA) behind a TCP relay that records what crosses the wire; behave(conn,
        heard) is the server's side of one connection. (the relay's port, the bytes each way, what the server heard)."""
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(self.folder / "srv.pem", self.folder / "srv-key.pem")
        context.load_verify_locations(cadata=self.rows["dev-pg"].ca_cert)
        context.verify_mode = ssl.CERT_REQUIRED
        server = socket.create_server(("127.0.0.1", 0))
        relay = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(server.close)
        self.addCleanup(relay.close)
        wire, heard = {"up": bytearray(), "down": bytearray()}, bytearray()

        def serve():
            raw, _ = server.accept()
            try:
                behave(context.wrap_socket(raw, server_side=True), heard)
            except (OSError, ssl.SSLError):
                raw.close()

        def copy(source, sink, into):
            try:
                while data := source.recv(65536):
                    into += data
                    sink.sendall(data)
            except OSError:
                pass
            for sock in (sink, source):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        def relay_one():
            inside, _ = relay.accept()
            outside = socket.create_connection(server.getsockname())
            threading.Thread(target=copy, args=(inside, outside, wire["up"]), daemon=True).start()
            copy(outside, inside, wire["down"])
        threading.Thread(target=serve, daemon=True).start()
        threading.Thread(target=relay_one, daemon=True).start()
        return relay.getsockname()[1], wire, heard

    def test_a_half_closing_client_sends_nothing_in_plain_text(self):
        def read_all(conn, heard):
            while data := conn.recv(1024):
                heard.extend(data)
            conn.close()
        port, wire, heard = self.recorded_endpoint(read_all)
        self.servers["dev-pg"] = port
        self.forwarder(self.config({"dev-pg": self.endpoints.issue("box", "dev-pg")}))
        with socket.create_connection(("127.0.0.1", self.local["dev-pg"]), timeout=10) as client:
            client.sendall(b"SECRET-QUERY-1")
            client.shutdown(socket.SHUT_WR)   # half way: the forwarder ends the session (nginx does too)
            self.assertEqual(client.recv(1024), b"")
        deadline = time.monotonic() + 5
        while heard != b"SECRET-QUERY-1" and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(bytes(heard), b"SECRET-QUERY-1")   # what was sent still arrived, through TLS
        self.assertNotIn(b"SECRET-QUERY", bytes(wire["up"]))
        self.assertTrue(wire["up"])

    def test_after_the_server_closes_first_nothing_goes_out_in_plain_text(self):
        def say_and_close(conn, heard):
            conn.sendall(b"SERVER-SECRET")
            conn.close()
        port, wire, heard = self.recorded_endpoint(say_and_close)
        self.servers["dev-pg"] = port
        self.forwarder(self.config({"dev-pg": self.endpoints.issue("box", "dev-pg")}))
        with socket.create_connection(("127.0.0.1", self.local["dev-pg"]), timeout=10) as client:
            got = bytearray()
            while data := client.recv(1024):
                got += data
            self.assertEqual(bytes(got), b"SERVER-SECRET")
            try:
                client.sendall(b"LATE-SECRET")   # after the end: must not reach the wire, least of all in plain text
                time.sleep(0.3)
            except OSError:
                pass
        self.assertNotIn(b"SERVER-SECRET", bytes(wire["down"]))
        self.assertNotIn(b"LATE-SECRET", bytes(wire["up"]))

    def test_a_port_it_can_t_listen_on_is_reported_and_said_once(self):
        taken = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(taken.close)
        self.local["dev-pg"] = taken.getsockname()[1]
        status = self.folder / "status.json"
        forwarder = self.guest.Forwarder(self.config({"dev-pg": self.endpoints.issue("box", "dev-pg")}),
                                         renewed=self.renewed, status=status)
        self.forwarders.append(forwarder)
        with patch.object(self.guest, "say") as said:
            forwarder.reconcile()
            forwarder.reconcile()
        self.assertEqual(said.call_count, 1)
        self.assertEqual(json.loads(status.read_text()), {"unbound": {"dev-pg": self.local["dev-pg"]}})


class ForwarderLivenessTests(Setup, Installation):
    """A forwarder that died is started again: by ensure, which sbx runs on writes, resumes and each keeper round."""

    def setUp(self):
        super().setUp()
        self.configure()
        sys.path.insert(0, str(GUEST))
        self.addCleanup(sys.path.remove, str(GUEST))
        import sbx_guest
        self.guest = sbx_guest
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="sbx-forward-")))

    def test_ensure_starts_one_only_when_none_runs(self):
        state = self.folder / "forwarder.json"
        state.write_text(json.dumps({"pid": 999999, "birth": "1"}))   # a forwarder that died
        with patch.object(self.guest.subprocess, "Popen", return_value=SimpleNamespace(pid=4242)) as popen, \
                patch.object(self.guest, "record", side_effect=lambda pid, proc=None: {"pid": pid, "birth": "7"}):
            self.guest.forward("ensure", self.folder / "config.json", state_file=state)
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(json.loads(state.read_text()), {"pid": 4242, "birth": "7"})
            with patch.object(self.guest, "alive", return_value=True):
                self.guest.forward("ensure", self.folder / "config.json", state_file=state)
            self.assertEqual(popen.call_count, 1)

    def test_two_ensures_at_once_start_one(self):
        state = self.folder / "forwarder.json"

        def spawn(*args, **kwargs):
            time.sleep(0.3)   # the other ensure looks meanwhile
            return SimpleNamespace(pid=4242)
        with patch.object(self.guest.subprocess, "Popen", side_effect=spawn) as popen, \
                patch.object(self.guest, "record", side_effect=lambda pid, proc=None: {"pid": pid, "birth": "7"}), \
                patch.object(self.guest, "alive", side_effect=bool):
            both = [threading.Thread(target=self.guest.forward, args=("ensure", self.folder / "config.json", state))
                    for _ in range(2)]
            for thread in both:
                thread.start()
            for thread in both:
                thread.join()
        self.assertEqual(popen.call_count, 1)

    def test_the_keeper_keeps_it_running_and_says_a_taken_port_once(self):
        from sbx_control import keeper, state
        self.endpoints.add(server="dev_pg", by="test")
        self.register("box")
        self.certs.record("box", "owner/app", ["dev-pg"], time.time() + 3600)
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            return SimpleNamespace(stdout=json.dumps({"unbound": {"dev-pg": 17400}}))
        vm = SimpleNamespace(commands=SimpleNamespace(run=run))
        info = SimpleNamespace(sandbox_id="id-box", metadata={"sbx": "box"})
        with patch.object(keeper.runtime, "connect", return_value=vm):
            keeper.sandbox_layers.tick(info)
            keeper.sandbox_layers.tick(info)
            keeper.sandbox_layers.tick(SimpleNamespace(sandbox_id="id-x", metadata={"sbx": "no-endpoints"}))
        self.assertEqual(len(commands), 2)   # the sandbox without endpoints is not looked into
        self.assertTrue(all("forward ensure" in c for c in commands))
        with state.db() as conn:
            said = [r[0] for r in conn.execute("select detail from events where sandbox = 'box' and "
                                               "kind = 'endpoint port taken'")]
        self.assertEqual(len(said), 1)
        self.assertIn("dev-pg can't listen on 127.0.0.1:17400", said[0])
