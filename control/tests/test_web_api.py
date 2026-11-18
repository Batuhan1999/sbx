"""What the web app reads that no other client does, through the HTTP API:
a repo's readiness and its list state, a sandbox still being created before
its VM exists and the actions it allows, which activity is the server's own
housekeeping, the connections people act on, where GitHub and old links
send the browser, cross-origin writes, and the page's style policy."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from support import Server


class WebApiTests(Server):
    SETTINGS = {"repos": ["owner/app", "owner/bare"]}

    def setUp(self):
        super().setUp()
        self.repo_admin()
        self.enterContext(patch("sbx_control.core.sandboxes", return_value=[]))  # the runtime has no VM yet

    def recipe(self, repo, text):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (directory / repo / ".sbx").mkdir(parents=True)
        (directory / repo / ".sbx/sbx.yaml").write_text(text)
        self.enterContext(patch("sbx_control.paths.apps", return_value=directory))

    def test_the_repositories_list_says_what_each_repo_needs_first(self):
        self.recipe("owner/app", "secrets: [APP_SECRET, SET_ALREADY]\nservices:\n  db:\n    uses: no-such-package\n")
        from sbx_control import variables
        variables.put("owner/app", "SET_ALREADY", "fixture-value", "operator@example.com")
        found = {r["repo"]: r["state"] for r in self.client.get("/api/repos").json()["repos"]}
        self.assertEqual(found["owner/app"], {"label": "A package needs attention", "level": "warn", "who": "admin"})
        self.assertEqual(found["owner/bare"], {"label": "Needs a recipe", "level": "warn", "who": "agent"})

    def test_readiness_names_the_missing_values_and_who_supplies_them(self):
        self.recipe("owner/app", "secrets: [APP_SECRET]\n")
        with patch("sbx_control.github_app.access", return_value="app"):
            found = self.client.get("/api/repos/owner/app/readiness").json()
        self.assertEqual([s["step"] for s in found["steps"]],
                         ["served", "github", "configured", "services", "env", "template", "sandbox", "recipe_pr"])
        env = next(s for s in found["steps"] if s["step"] == "env")
        self.assertEqual((env["status"], env["who"], env["missing"]), ("todo", "person", ["APP_SECRET"]))
        self.assertEqual((found["stage"], found["next"]), ("person", "env"))

    def creating(self, name, owner="operator@example.com"):
        """A sandbox whose VM the runtime is still allocating."""
        from sbx_control import lifecycle, sandboxes
        sandboxes.register(name, owner, "main", name)
        lifecycle.begin(name, "create").advance("allocating")

    def as_user(self, email):
        """Headers for someone signed in with an API token (not the admin's cookie)."""
        from sbx_control import auth
        return {"Authorization": f"Bearer {auth.mint_token(email, 'test')}"}

    def test_actions_are_what_the_viewer_may_do_not_only_what_the_state_allows(self):
        self.creating("theirs", owner="someone@example.com")
        seen = self.client.get("/api/sandboxes/theirs/detail").json()   # an admin, not its owner
        self.assertFalse(any(seen["actions"].values()), seen["actions"])
        self.assertNotIn("mine", seen)
        own = self.client.get("/api/sandboxes/theirs/detail", headers=self.as_user("someone@example.com")).json()
        self.assertTrue(own["actions"]["remove"])
        listed = self.client.get("/api/sandbox-list?scope=all").json()["sandboxes"]
        self.assertFalse(next(i for i in listed if i["name"] == "theirs")["actions"]["remove"])

    def test_a_connection_packages_saved_connections_are_for_admins(self):
        from sbx_control import catalog
        name = catalog.listing("connection")[0].name
        saved = [{"package": name, "key": "main", "added_by": "operator@example.com", "used_by": ["box"]}]
        with patch("sbx_control.catalog_api._connections", return_value=saved):
            self.assertEqual(self.client.get(f"/api/catalog/{name}").json()["connections"], saved)
            found = self.client.get(f"/api/catalog/{name}", headers=self.as_user("someone@example.com"))
        self.assertEqual(found.status_code, 200, found.text)
        self.assertEqual(found.json()["connections"], [])

    def test_missing_values_go_to_an_admin_for_someone_who_is_not_one(self):
        self.recipe("owner/app", "secrets: [APP_SECRET]\n")
        with patch("sbx_control.github_app.access", return_value="app"):
            found = self.client.get("/api/repos/owner/app/readiness", headers=self.as_user("someone@example.com")).json()
        env = next(s for s in found["steps"] if s["step"] == "env")
        self.assertEqual((env["status"], env["who"], env["missing"]), ("todo", "admin", ["APP_SECRET"]))

    def test_the_template_says_its_state_in_words_and_a_level(self):
        self.recipe("owner/app", "secrets: []\n")   # no template of its own: the default one, not built here
        found = self.client.get("/api/repos/owner/app/template").json()
        self.assertEqual((found["state"], found["label"], found["level"]), ("missing", "Not built", "warn"))

    def test_an_operation_on_someone_elses_or_no_sandbox_names_the_sandbox(self):
        response = self.client.post("/api/sandboxes/nothing-here/pause")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "no sandbox nothing-here")

    def test_no_setup_link_is_offered_once_setup_is_complete(self):
        self.assertIn(self.client.post("/api/setup/token").status_code, (404, 405))

    def test_a_sandbox_being_created_before_its_vm_carries_its_lifecycle(self):
        self.creating("early")
        listed = self.client.get("/api/sandbox-list?scope=mine").json()["sandboxes"]
        self.assertEqual([item["name"] for item in listed], ["early"])
        self.assertEqual(listed[0]["lifecycle"]["phase"], "allocating")
        self.assertEqual(listed[0]["readiness"], "not_ready")
        self.assertEqual(listed[0]["actions"], {"pause": False, "resume": False, "restart": False, "remove": True,
                                                "logs": False})

    def test_a_preview_being_created_is_among_the_previews(self):
        from sbx_control import lifecycle, sandboxes
        sandboxes.register("pr-7", "github-preview", "feature", "pr-7", {"audience": "previews", "shared": "team"})
        lifecycle.begin("pr-7", "create").advance("allocating")
        listed = self.client.get("/api/sandbox-list?scope=previews").json()["sandboxes"]
        self.assertEqual([item["name"] for item in listed], ["pr-7"])

    def test_housekeeping_events_are_marked_internal(self):
        from sbx_control import state
        self.creating("box")
        state.event("box", "paused", "explicit operator request")
        state.event("box", "snapshot cleanup deferred", "retained for retry", internal=True)
        state.event("box", "runtime restored", "its tmpfs was empty", internal=True)
        events = self.client.get("/api/sandboxes/box/detail").json()["events"]
        self.assertEqual({e["kind"]: e["internal"] for e in events},
                         {"paused": False, "snapshot cleanup deferred": True, "runtime restored": True})

    def test_the_github_app_returns_to_the_extensions_page(self):
        response = self.client.post("/api/connections/github/manifest", json={"return_to": "/app/extensions"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(json.loads(response.json()["manifest"])["setup_url"].endswith("/app/extensions"))
        refused = self.client.post("/api/connections/github/manifest", json={"return_to": "https://elsewhere.example"})
        self.assertEqual(refused.status_code, 400)

    def test_the_page_runs_only_its_own_scripts(self):
        from sbx_control import spa
        page = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (page / "index.html").write_text("<!doctype html><html><head><title>sbx</title></head><body></body></html>")
        self.enterContext(patch.object(spa, "DIST", page))
        policy = self.client.get("/app/sandboxes").headers["content-security-policy"]
        self.assertIn("script-src 'self';", policy)
        self.assertIn("frame-ancestors 'none'", policy)

    def test_the_setup_link_and_the_root_land_in_the_app(self):
        for path, target in (("/", "/app/repos"), ("/setup", "/app/setup")):
            response = self.client.get(path, follow_redirects=False)
            self.assertEqual(response.headers["location"], target)

    def test_a_cookie_write_from_another_origin_is_refused_and_a_token_one_is_not(self):
        foreign = {"Origin": "https://preview.elsewhere.example"}
        self.assertEqual(self.client.put("/api/settings/sandboxes", json={"value": {"paused_days": 9}},
                                         headers=foreign).status_code, 403)
        own = self.client.put("/api/settings/sandboxes", json={"value": {"paused_days": 9}},
                              headers={"Origin": "https://203.0.113.10"})
        self.assertEqual(own.status_code, 200, own.text)
        token = self.client.put("/api/settings/sandboxes", json={"value": {"paused_days": 9}},
                                headers={**foreign, **self.headers})
        self.assertNotEqual(token.status_code, 403, token.text)

    def test_connections_list_what_people_act_on_grouped(self):
        listed = {c["kind"]: c for c in self.client.get("/api/connections").json()["connections"]}
        self.assertNotIn("store", listed)
        self.assertNotIn("runtime", listed)
        self.assertEqual({listed[k]["group"] for k in ("domain", "sign_in", "github")}, {"platform"})
        self.assertEqual(listed["claude"]["group"], "agent")
        shown = {d["label"]: d["value"] for d in listed["sign_in"]["display"]}
        self.assertTrue(shown["Redirect uri"].endswith("/auth/callback"))
        self.assertNotIn("Method", shown)  # the summary already says it: one shared password
