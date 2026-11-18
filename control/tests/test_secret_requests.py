"""Secret requests: an agent asks for keys, the owner pastes each once on
the keys page, signed in, and every value goes straight into the store its
target already has (a runtime's settings through the host's queue, the DNS
token into the vault, a connector through its connect, a repo's values);
a key the server holds is reused for a repo value instead of pasted again.
No response, request file or row ever holds a value."""
import json
from unittest.mock import patch

from support import Server
from test_runtimes import TwoRuntimes

CLOUD_KEY = "test-only-cloud-key-5f3a"
DNS_TOKEN = "test-only-dns-token-77c1"
APP_VALUE = "test-only-app-value-91d0"


class Keys(TwoRuntimes, Server):
    """Embed (a stand-in) and the fake enabled, E2B Cloud and Boat offered;
    owner/app trusted; the managed store as references."""
    SETTINGS = {"repos": ["owner/app"]}

    def setUp(self):
        super().setUp()
        from sbx_control import secret_store, settings, template_scheduler, vault
        from sbx_control.connections.base import Check
        from sbx_control.runtime.e2b_cloud import Cloud
        self.file = self.root / "etc/runtime.env"
        self.addCleanup(self.file.write_text, self.RUNTIME_ENV)
        self.cloud_checks = [Check("E2B Cloud API", True, "answers")]
        self.enterContext(patch.object(Cloud, "test", side_effect=lambda: self.cloud_checks))
        self.enterContext(patch.object(Cloud, "sandboxes", side_effect=lambda *_: []))
        self.enterContext(patch.object(template_scheduler, "start_detached", return_value=("unit", True)))
        self.store, made = {}, iter(range(1, 1000))

        def seal(value, *, key):
            reference = f"infisical-ref-{next(made)}"
            self.store[reference] = value
            return reference
        self.enterContext(patch.object(vault, "sealing", return_value=True))
        for patcher in (patch.object(secret_store, "seal", side_effect=seal),
                        patch.object(secret_store, "is_reference",
                                     side_effect=lambda v: str(v).startswith("infisical-ref-")),
                        patch.object(secret_store, "unseal", side_effect=lambda r: self.store[r]),
                        patch.object(secret_store, "discard", side_effect=lambda r: self.store.pop(r, None))):
            self.enterContext(patcher)
        self.request_file = self.root / "run/apply/runtime-change"
        self.addCleanup(lambda: (self.root / "state/runtime-change.json").unlink(missing_ok=True))
        self.addCleanup(self.request_file.unlink, missing_ok=True)
        self.cloudflare = self.enterContext(patch("sbx_control.secret_requests.verify_cloudflare", return_value=""))
        self.delivered = self.enterContext(patch("sbx_control.sandboxes.deliver_env",
                                                 return_value={"running": 0, "paused": 0}))
        self.repo_admin()   # operator@example.com, signed in on self.client; the setup is complete
        settings.update("domain", {**settings.current().domain.model_dump(), "name": "sbx.example.com"}, by="test")

    def ask(self, *items, status=201, **body):
        response = self.client.post("/api/secret-requests", json={
            "items": [{"target": t, "label": label} for t, label in items], **body})
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def submit(self, request_id, values, status=200, headers=None):
        response = self.client.post(f"/api/secret-requests/{request_id}/submit", json={"values": values},
                                    headers=headers or {})
        self.assertEqual(response.status_code, status, response.text)
        for value in (CLOUD_KEY, DNS_TOKEN, APP_VALUE):
            self.assertNotIn(value, response.text)
        return response.json()

    def test_a_request_names_where_each_key_goes_and_links_to_the_keys_page(self):
        found = self.ask(("runtime:e2b-cloud", "E2B key"), ("dns:cloudflare", ""),
                         ("repo:Owner/App:E2B_API_KEY=from:runtime:e2b-cloud", "Same key for the app"))
        self.assertEqual(found["state"], "pending")
        self.assertRegex(found["url"], r"^https://login\.sbx\.example\.com/keys/[A-Za-z0-9_-]+$")
        self.assertEqual([i["target"] for i in found["items"]],
                         ["runtime:e2b-cloud:E2B_CLOUD_API_KEY", "dns:cloudflare",
                          "repo:owner/app:E2B_API_KEY=from:runtime:e2b-cloud:E2B_CLOUD_API_KEY"])
        runtime_item, dns, reuse = found["items"]
        self.assertEqual(runtime_item["label"], "E2B key")
        self.assertTrue(runtime_item["help_url"].endswith("/docs/howto/e2b.md"))
        self.assertEqual(dns["label"], "The Cloudflare API token for this server's certificate")
        self.assertTrue(dns["help_url"].endswith("/docs/howto/cloudflare.md"))
        self.assertEqual((reuse["reuse"], reuse["reuse_from"]),
                         ("runtime:e2b-cloud:E2B_CLOUD_API_KEY", "E2B Cloud's API key"))
        self.assertEqual(found["remaining"], 3)
        self.assertEqual(self.client.get(f"/api/secret-requests/{found['id']}").json()["items"], found["items"])
        page = self.client.get(f"/keys/{found['id']}", follow_redirects=False)
        self.assertEqual((page.status_code, page.headers["location"]), (307, f"/app/keys/{found['id']}"))
        listed = self.client.get("/api/secret-requests").json()["requests"]
        self.assertEqual([r["id"] for r in listed], [found["id"]])

    def test_a_target_the_server_cannot_take_is_refused_when_asked(self):
        for target, said in (("runtime:nowhere", "no runtime nowhere"),
                             ("runtime:embed", "install.sh sets up its keys"),
                             ("runtime:e2b-cloud:E2B_CLOUD_MAX_TIMEOUT", "no secret setting"),
                             ("dns:route53", "cloudflare"),
                             ("repo:owner/unknown:TOKEN", "not a trusted repo"),
                             ("repo:owner/app:not-a-name", "not a variable name"),
                             ("connector:nowhere", "no connector nowhere"),
                             ("runtime:boat=from:dns:cloudflare", "only a repo value may reuse"),
                             ("repo:owner/app:K=from:dns:cloudflare", "holds no the Cloudflare API token"),
                             ("bucket:x", "a target is")):
            with self.subTest(target):
                self.assertIn(said, self.ask((target, ""), status=400)["detail"])
        self.assertIn("help link", self.client.post("/api/secret-requests", json={"items": [
            {"target": "dns:cloudflare", "help_url": "javascript:alert(1)"}]}).json()["detail"])
        self.assertIn("5 minutes to 7 days", self.ask(("dns:cloudflare", ""), status=400, expires_in=60)["detail"])

    def test_keys_are_submitted_by_a_signed_in_admin_only(self):
        found = self.ask(("dns:cloudflare", ""))
        from fastapi.testclient import TestClient
        from sbx_control import auth
        with TestClient(self.app.app, base_url="https://203.0.113.10") as stranger:
            anonymous = stranger.post(f"/api/secret-requests/{found['id']}/submit", json={"values": {}})
            self.assertEqual(anonymous.status_code, 401)
            self.assertEqual(stranger.get(f"/api/secret-requests/{found['id']}").status_code, 401)
            member = {"Authorization": f"Bearer {auth.mint_token('someone@example.com', 'test')}"}
            refused = stranger.post(f"/api/secret-requests/{found['id']}/submit", headers=member,
                                    json={"values": {"dns:cloudflare": DNS_TOKEN}})
            self.assertEqual(refused.status_code, 403)
        self.assertFalse(self.vault_has_dns())

    def vault_has_dns(self) -> bool:
        from sbx_control import vault
        return vault.facts(vault.CLOUDFLARE_API_TOKEN).set

    def test_each_value_goes_into_its_own_store_and_a_pasted_key_is_reused(self):
        found = self.ask(("runtime:e2b-cloud", ""), ("dns:cloudflare", ""), ("repo:owner/app:APP_SECRET", ""),
                         ("repo:owner/app:E2B_API_KEY=from:runtime:e2b-cloud", ""))
        done = self.submit(found["id"], {"runtime:e2b-cloud": CLOUD_KEY, "dns:cloudflare": f" {DNS_TOKEN}\n",
                                         "repo:owner/app:APP_SECRET": APP_VALUE})
        self.assertTrue(done["ok"], done)
        self.assertEqual([r["ok"] for r in done["results"]], [True, True, True, True])
        self.assertEqual((done["request"]["state"], done["request"]["remaining"]), ("completed", 0))
        self.assertEqual(done["request"]["finished_by"], "operator@example.com")
        # The runtime: added through the host's queue, its key only as a store reference.
        request = json.loads(self.request_file.read_text())
        self.assertNotIn(CLOUD_KEY, self.request_file.read_text())
        self.assertEqual((request["operation"], request["kind"], request["by"]), ("add", "e2b-cloud", "operator@example.com"))
        self.assertEqual(self.store[request["values"]["E2B_CLOUD_API_KEY"]], CLOUD_KEY)
        self.assertEqual(request["values"]["E2B_CLOUD_MAX_TIMEOUT"], "3600")   # its first plan, as sbx runtimes add
        # The DNS token: checked by Cloudflare, then in the vault as the domain step keeps it.
        self.cloudflare.assert_called_once_with(DNS_TOKEN)
        from sbx_control import variables, vault
        self.assertEqual(vault.get(vault.CLOUDFLARE_API_TOKEN), DNS_TOKEN)
        self.assertIn("tls", self.effects())
        # The repo's values: the pasted one and the reused runtime key, secret, delivered.
        self.assertEqual(variables.values("owner/app"), {"APP_SECRET": APP_VALUE, "E2B_API_KEY": CLOUD_KEY})
        self.assertTrue(all(e["secret"] for e in variables.entries("owner/app")))
        self.assertEqual(self.delivered.call_count, 2)
        # One-time: done is done.
        self.assertIn("completed", self.submit(found["id"], {"dns:cloudflare": DNS_TOKEN}, status=409)["detail"])
        from sbx_control import state
        with state.db() as conn:
            rows = json.dumps(conn.execute("select * from secret_requests").fetchall())
        for value in (CLOUD_KEY, DNS_TOKEN, APP_VALUE):
            self.assertNotIn(value, rows)

    def test_a_refused_key_stays_pending_with_its_error_and_the_rest_are_kept(self):
        self.cloudflare.return_value = "Cloudflare refused the token: Invalid API Token"
        self.cloud_checks[:] = []
        from sbx_control.connections.base import Check
        self.cloud_checks.append(Check("E2B Cloud API", False, "401 unauthorized"))
        found = self.ask(("runtime:e2b-cloud", ""), ("dns:cloudflare", ""), ("repo:owner/app:APP_SECRET", ""))
        done = self.submit(found["id"], {"runtime:e2b-cloud": CLOUD_KEY, "dns:cloudflare": DNS_TOKEN,
                                         "repo:owner/app:APP_SECRET": APP_VALUE})
        self.assertFalse(done["ok"])
        results = {r["target"]: r for r in done["results"]}
        self.assertIn("401 unauthorized", results["runtime:e2b-cloud:E2B_CLOUD_API_KEY"]["error"])
        self.assertIn("Invalid API Token", results["dns:cloudflare"]["error"])
        self.assertTrue(results["repo:owner/app:APP_SECRET"]["ok"])
        self.assertEqual((done["request"]["state"], done["request"]["remaining"]), ("pending", 2))
        self.assertFalse(self.request_file.exists())
        self.assertFalse(self.vault_has_dns())
        self.assertEqual(self.store, {})   # the refused runtime key was sealed, then discarded
        self.cloudflare.return_value = ""   # pasted again, right this time
        again = self.submit(found["id"], {"dns:cloudflare": DNS_TOKEN})
        self.assertEqual([r["target"] for r in again["results"]], ["dns:cloudflare"])
        self.assertEqual(again["request"]["remaining"], 1)
        self.assertIn("does not ask for", self.submit(found["id"], {"runtime:boat": "x"}, status=400)["detail"])
        self.assertIn("at least one", self.submit(found["id"], {}, status=400)["detail"])

    def test_a_key_the_server_holds_is_reused_without_pasting_it(self):
        self.file.write_text(self.RUNTIME_ENV.replace("FAKE_RUNTIME_TOKEN=test-only-fake",
                                                      f"FAKE_RUNTIME_TOKEN={CLOUD_KEY}"))
        found = self.ask(("repo:owner/app:FAKE_TOKEN=from:runtime:fake", ""))
        self.assertEqual(found["items"][0]["reuse_from"], "Fake runtime's Token")
        done = self.submit(found["id"], {})
        self.assertTrue(done["ok"])
        from sbx_control import variables
        self.assertEqual(variables.values("owner/app"), {"FAKE_TOKEN": CLOUD_KEY})
        other = self.ask(("repo:owner/app:COPY=from:repo:owner/app:FAKE_TOKEN", ""))
        self.submit(other["id"], {})
        self.assertEqual(variables.values("owner/app")["COPY"], CLOUD_KEY)

    def test_a_connector_s_secret_goes_through_its_connect_and_its_settings_stay(self):
        found = self.ask(("connector:amp", "Amp provider token"))
        self.assertEqual(found["items"][0]["target"], "connector:amp:provider_token")
        self.assertTrue(found["items"][0]["help_url"].endswith("/docs/howto/amp.md"))
        bad = self.submit(found["id"], {"connector:amp": "not-a-token"})
        self.assertIn("orb provider token", bad["results"][0]["error"])
        good = self.submit(found["id"], {"connector:amp:provider_token": "aop_live_test-only-0123456789"})
        self.assertTrue(good["ok"])
        from sbx_control import extensions, vault
        from sbx_control.connectors.amp.common import TOKEN
        self.assertEqual(vault.get(TOKEN), "aop_live_test-only-0123456789")
        self.assertFalse(extensions.get("amp").settings().enabled)   # saving the token did not turn Amp on

    def test_a_request_expires_or_is_cancelled_and_then_takes_nothing(self):
        found = self.ask(("dns:cloudflare", ""))
        cancelled = self.client.delete(f"/api/secret-requests/{found['id']}")
        self.assertEqual(cancelled.json()["state"], "cancelled")
        self.assertEqual(self.client.delete(f"/api/secret-requests/{found['id']}").status_code, 409)
        self.assertIn("cancelled", self.submit(found["id"], {"dns:cloudflare": DNS_TOKEN}, status=409)["detail"])
        later = self.ask(("dns:cloudflare", ""), expires_in=300)
        from sbx_control import state
        with state.db() as conn:
            conn.execute("update secret_requests set expires_at = '2020-01-01T00:00:00Z' where id = ?", (later["id"],))
        self.assertEqual(self.client.get(f"/api/secret-requests/{later['id']}").json()["state"], "expired")
        self.assertIn("new one", self.submit(later["id"], {"dns:cloudflare": DNS_TOKEN}, status=409)["detail"])
        self.assertFalse(self.vault_has_dns())
        self.assertEqual(self.client.get("/api/secret-requests/nope-nope-nope").status_code, 404)


class DuringSetup(Server):
    """The first-run setup's agent asks with the setup link; the page waits for the admin's sign-in."""

    def test_the_setup_link_asks_and_follows_but_never_submits(self):
        self.choose_ip()
        asked = self.client.post("/api/secret-requests", headers=self.headers,
                                 json={"items": [{"target": "dns:cloudflare", "label": "DNS token"}]})
        self.assertEqual(asked.status_code, 201, asked.text)
        found = asked.json()
        self.assertEqual(found["created_by"], "setup")
        self.assertTrue(found["url"].startswith("https://203.0.113.10/keys/"))
        self.assertEqual(self.client.get(f"/keys/{found['id']}", follow_redirects=False).status_code, 307)
        refused = self.client.post(f"/api/secret-requests/{found['id']}/submit", headers=self.headers,
                                   json={"values": {"dns:cloudflare": DNS_TOKEN}})
        self.assertEqual(refused.status_code, 401, refused.text)
        self.create_admin()   # signed in on self.client now
        with patch("sbx_control.secret_requests.verify_cloudflare", return_value=""):
            done = self.client.post(f"/api/secret-requests/{found['id']}/submit",
                                    json={"values": {"dns:cloudflare": DNS_TOKEN}})
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["request"]["state"], "completed")
        followed = self.client.get(f"/api/secret-requests/{found['id']}", headers=self.headers).json()
        self.assertEqual(followed["state"], "completed")


class Cloudflare(Server):
    def test_the_token_is_checked_with_cloudflare_s_own_api(self):
        from sbx_control import secret_requests

        class Answer:
            def __init__(self, body):
                self.body = json.dumps(body).encode()

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return self.body
        asked = []

        def urlopen(request, timeout):
            asked.append((request.full_url, request.get_header("Authorization")))
            return Answer(answers.pop(0))
        with patch("urllib.request.urlopen", side_effect=urlopen):
            answers = [{"success": True, "result": {"status": "active"}}]
            self.assertEqual(secret_requests.verify_cloudflare(DNS_TOKEN), "")
            answers = [{"success": False, "errors": [{"message": "Invalid API Token"}]},
                       {"success": True, "result": []}]   # an account token: it lists its zones
            self.assertEqual(secret_requests.verify_cloudflare(DNS_TOKEN), "")
            answers = [{"success": False, "errors": [{"message": "Invalid API Token"}]},
                       {"success": False, "errors": [{"message": "Invalid API Token"}]}]
            self.assertIn("Invalid API Token", secret_requests.verify_cloudflare(DNS_TOKEN))
            answers = [{"success": True, "result": {"status": "disabled"}}]
            self.assertIn("disabled", secret_requests.verify_cloudflare(DNS_TOKEN))
        self.assertEqual(asked[0], ("https://api.cloudflare.com/client/v4/user/tokens/verify", f"Bearer {DNS_TOKEN}"))
        self.assertEqual(asked[2][0], "https://api.cloudflare.com/client/v4/zones?per_page=1")
