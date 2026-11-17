"""A preview link always shows a page: what the gate answers for each state
a sandbox can be in (signed out: the login, whatever is there), the page
and its status for browsers and for anything else, the poll that hands
over to the app, and the wake that never blocks nginx."""
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from support import Server
from test_runtimes import TwoRuntimes


class Template(unittest.TestCase):
    def test_every_state_has_one_line_and_its_status(self):
        from sbx_control.preview_pages import STATUS, Page
        for state in STATUS:
            page = Page(state, "memos-a", step="Waking up", port=5173)
            self.assertTrue(page.title() and page.line(), state)
        self.assertEqual(Page("waking", "memos-a", step="Waking up").title(), "Starting memos-a…")
        self.assertEqual(Page("no_listener", "memos-a", port=5173).title(),
                         "Nothing is listening on port 5173 in memos-a")
        self.assertEqual(Page("app_stopped", "memos-a").title(), "memos-a's app stopped")
        self.assertEqual(Page("missing", "memos-x").title(), "No sandbox named memos-x")
        self.assertEqual((Page("starting").status, Page("deleted").status, Page("forbidden").status), (503, 404, 403))

    def test_the_page_escapes_names_waits_calmly_and_hands_over_to_the_exact_url(self):
        from sbx_control.preview_pages import Page, render
        text = render(Page("starting", "<b>x</b>", step="Running setup"))
        self.assertNotIn("<b>x</b>", text)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)
        self.assertIn("prefers-color-scheme: dark", text)
        self.assertIn("prefers-reduced-motion: reduce", text)
        self.assertIn("location.replace(location.href)", text)   # path, query and hash kept; Back skips the page
        self.assertIn('"/.sbx/status"', text)
        self.assertIn("Math.min(wait * 1.5, 5000)", text)          # backs off, no cap on how long
        self.assertNotIn("http-equiv", text)                       # no meta refresh
        done = render(Page("deleted", "memos-a"))
        self.assertIn('aria-busy="false"', done)

    def test_a_visitor_who_may_not_know_gets_no_names(self):
        from sbx_control.preview_pages import Page
        neutral = Page("app_stopped", "memos-a", action=("Restart", "https://x")).neutral()
        self.assertEqual((neutral.name, neutral.action, neutral.state), ("", (), "starting"))


class Gate(TwoRuntimes, Server):
    def setUp(self):
        super().setUp()
        from sbx_control import auth, core, gate, recipes, settings
        self.gate = gate
        self.repo_admin()
        settings.update("domain", {**settings.current().domain.model_dump(), "name": "example.test"}, by="test")
        self.client.cookies.clear()
        self.app = recipes.App("owner/app", Path("/unused"), {"previews": {
            "": {"port": 3000, "access": "team"}, "desktop": {"port": 6080, "access": "owner"}}})
        self.enterContext(patch.object(core, "app_of", return_value=self.app))
        self.enterContext(patch.dict(gate._owners, {"fetched_at": 0.0, "by_id": {}, "state": {}, "app": {}, "info": {}}))
        for kept in (gate._waking, gate._wake_errors, gate._listening, gate._last_access):
            self.enterContext(patch.dict(kept, clear=True))
        self.owner = auth.mint_token("operator@example.com", "test")
        self.other = auth.mint_token("other@example.com", "test")
        self.vm = self.add("memos-a")

    def add(self, name, state="up"):
        """A sandbox of the fake runtime, registered, with its lifecycle up to state."""
        from sbx_control import lifecycle, sandboxes
        handle = self.runtime.create("tpl", timeout=600, kind="fake", metadata={
            "sbx": name, "slug": name, "owner": "operator@example.com", "repo": "owner/app"})
        sandboxes.register(name, "operator@example.com", "main", name, kind="fake")
        op = lifecycle.begin(name, "create")
        op.advance("provisioning_services")
        op.advance("services_ready")
        op.advance("allocating")
        op.advance("preparing_workspace", sandbox_id=handle.sandbox_id, runtime="running")
        op.advance("workspace_ready", finish=True)
        self.gate.sandbox_owner(handle.sandbox_id, fresh=True)
        return handle.sandbox_id

    def ask(self, host, token=None, path="/gate", **headers):
        headers = {"X-Original-Host": host, "X-Original-Method": "GET", **headers}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return self.client.get(path, headers=headers)

    def page(self, host, token=None, html=True, **headers):
        accept = {"Accept": "text/html,application/xhtml+xml"} if html else {"Accept": "application/json"}
        return self.ask(host, token, "/gate/page", **accept, **headers)

    def test_signed_out_is_the_login_whatever_the_name(self):
        for host in ("memos-a.example.test", "nothing-here.example.test"):
            self.assertEqual(self.ask(host).status_code, 401, host)

    def test_a_running_app_is_let_through(self):
        response = self.ask("memos-a.example.test", self.owner)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["x-sbx-runtime"], "fake")

    def test_no_such_name_is_a_friendly_page_not_a_bare_404(self):
        response = self.ask("memos-x.example.test", self.owner)
        self.assertEqual((response.status_code, response.headers.get("x-sbx-page")), (403, "missing"))
        shown = self.page("memos-x.example.test", self.owner)
        self.assertEqual(shown.status_code, 404)
        self.assertIn("No sandbox named memos-x", shown.text)
        self.assertEqual(self.page("memos-x.example.test", self.owner, html=False).json()["error"], "missing")

    def test_a_deleted_sandbox_says_so(self):
        from sbx_control import lifecycle
        op = lifecycle.begin("memos-a", "delete")
        op.advance("deleting")
        op.advance("deleted", finish=True)
        shown = self.page("memos-a.example.test", self.owner)
        self.assertEqual(shown.status_code, 404)
        self.assertIn("memos-a was deleted", shown.text)

    def test_a_sandbox_being_created_shows_the_step(self):
        from sbx_control import lifecycle, sandboxes
        sandboxes.register("memos-b", "operator@example.com", "main", "memos-b", kind="fake")
        op = lifecycle.begin("memos-b", "create")
        op.advance("allocating")
        response = self.ask("memos-b.example.test", self.owner)
        self.assertEqual((response.headers.get("x-sbx-page"), response.headers.get("x-sbx-wait")), ("starting", "1"))
        shown = self.page("memos-b.example.test", self.owner)
        self.assertEqual(shown.status_code, 503)
        self.assertIn("Starting memos-b…", shown.text)
        self.assertIn("Creating the sandbox", shown.text)
        api = self.page("memos-b.example.test", self.owner, html=False)
        self.assertEqual((api.status_code, api.headers["retry-after"]), (503, "2"))   # not HTML: status + Retry-After
        status = self.ask("memos-b.example.test", self.owner, "/gate/status").json()
        self.assertEqual((status["state"], status["line"], status["waiting"]), ("starting", "Creating the sandbox", True))

    def test_each_running_operation_names_its_step(self):
        from sbx_control import lifecycle
        op = lifecycle.begin("memos-a", "restart")
        op.advance("starting_app")
        self.assertEqual(self.ask("memos-a.example.test", self.owner, "/gate/status").json()["line"], "Starting the app")
        lifecycle.recover_interrupted()
        op = lifecycle.begin("memos-a", "prepare")
        op.advance("preparing_workspace", sandbox_id=self.vm, runtime="running")
        op.advance("workspace_ready")
        op.advance("setting_up")
        self.assertEqual(self.ask("memos-a.example.test", self.owner, "/gate/status").json()["line"], "Running setup")

    def test_a_paused_sandbox_wakes_without_blocking_and_hands_over_when_its_app_listens(self):
        from sbx_control import core
        self.runtime.pause(self.vm)
        self.gate.sandbox_owner(self.vm, fresh=True)
        release = threading.Event()

        def slow_resume(name, why, info=None):
            release.wait(10)
            self.runtime.connect(self.vm)
        with patch.object(core, "attach", side_effect=slow_resume), patch.object(self.gate, "WAKE_WAIT_SECONDS", 0.05):
            response = self.ask("memos-a.example.test", self.owner)
            self.assertEqual((response.status_code, response.headers.get("x-sbx-page")), (403, "waking"))
            shown = self.page("memos-a.example.test", self.owner)
            self.assertIn("Waking up", shown.text)
            self.assertEqual(self.ask("memos-a.example.test", self.owner, "/gate/status").json()["state"], "waking")
            release.set()
            self.gate._waking[self.vm].join(5)
            with patch.object(self.gate, "_listens", return_value=False):
                self.assertEqual(self.ask("memos-a.example.test", self.owner, "/gate/status").json()["line"],
                                 "Starting the app")
            with patch.object(self.gate, "_listens", return_value=True):
                self.assertEqual(self.ask("memos-a.example.test", self.owner, "/gate/status").json()["state"], "ready")
            self.assertEqual(self.ask("memos-a.example.test", self.owner).status_code, 200)
        self.assertTrue(self.gate.resumed_for_preview(self.vm))
        self.gate.forget_preview_resume(self.vm)

    def test_a_resume_that_fails_says_what_to_do(self):
        from sbx_control import core
        self.runtime.pause(self.vm)
        self.gate.sandbox_owner(self.vm, fresh=True)
        with patch.object(core, "attach", side_effect=RuntimeError("no machine")):
            response = self.ask("memos-a.example.test", self.owner)
        self.assertEqual(response.headers.get("x-sbx-page"), "failed")

    def test_an_app_that_stopped_offers_its_owner_a_restart(self):
        from sbx_control import lifecycle
        op = lifecycle.begin("memos-a", "restart")
        op.advance("starting_app")
        op.advance("app_ready", finish=True)
        self.assertTrue(lifecycle.app_stopped("memos-a", self.vm, "its processes exited"))
        shown = self.page("memos-a.example.test", self.owner)
        self.assertEqual(shown.status_code, 502)
        self.assertIn("memos-a&#x27;s app stopped", shown.text)
        self.assertIn("/sandboxes/memos-a", shown.text)
        self.assertIn(">Restart<", shown.text)
        self.assertNotIn(">Restart<", self.page("memos-a.example.test", self.other).text)

    def test_a_failed_sandbox_says_what_happened(self):
        from sbx_control import lifecycle
        op = lifecycle.begin("memos-a", "restart")
        op.advance("starting_app")
        op.fail(RuntimeError("boom"))
        shown = self.page("memos-a.example.test", self.owner)
        self.assertEqual(shown.status_code, 502)
        self.assertIn("memos-a failed", shown.text)
        self.assertIn("failed while starting the app", shown.text)

    def test_someone_elses_owner_only_preview_is_no_access(self):
        response = self.ask("memos-a-desktop.example.test", self.other)
        self.assertEqual(response.headers.get("x-sbx-page"), "forbidden")
        self.assertIn("You don&#x27;t have access", self.page("memos-a-desktop.example.test", self.other).text)

    def test_nothing_on_a_port_says_which(self):
        response = self.ask(f"5173-{self.vm}.example.test", self.owner)
        self.assertEqual(response.headers.get("x-sbx-page"), "no_listener")
        self.assertIn("Nothing is listening on port 5173 in memos-a", self.page(f"5173-{self.vm}.example.test",
                                                                                     self.owner).text)

    def test_an_upstream_error_while_the_app_starts_is_the_starting_page(self):
        with patch.object(self.gate, "_listens", return_value=False):
            shown = self.page("memos-a.example.test", self.owner, **{"X-Sbx-Reason": "unavailable"})
        self.assertEqual(shown.status_code, 503)
        self.assertIn("Starting the app", shown.text)
        self.assertIn('"reason": "unavailable"', shown.text)   # the page counts a handover that meets it again

    def test_a_provider_desktop_opens_the_providers_own(self):
        self.fake.desktop = "provider"
        self.fake.desktop_url = lambda sandbox_id: f"https://desk.example.test/{sandbox_id}#token=t"
        response = self.ask("memos-a-desktop.example.test", self.owner)
        self.assertEqual(response.headers.get("x-sbx-page"), "desktop")
        redirect = self.client.get("/gate/page", headers={"X-Original-Host": "memos-a-desktop.example.test",
                                                          "X-Original-Method": "GET", "Accept": "text/html",
                                                          "Authorization": f"Bearer {self.owner}"},
                                   follow_redirects=False)
        self.assertEqual(redirect.status_code, 302)
        self.assertEqual(redirect.headers["location"], f"https://desk.example.test/{self.vm}#token=t")


class Nginx(unittest.TestCase):
    """The nginx side, as tls.sh and ip_previews write it (also run in a real
    nginx:1.29 against fakes while building this)."""
    SCRIPT = (Path(__file__).parents[1] / "tls.sh").read_text()

    def test_refusals_and_upstream_errors_become_the_page_and_the_apps_own_errors_stay_its_own(self):
        sandbox = self.SCRIPT[self.SCRIPT.index("  location @sandbox {"):]
        sandbox = sandbox[:sandbox.index("\n  }\n")]
        self.assertIn("try_files /.sbx-no-such-file @sandbox;", self.SCRIPT)   # after auth_request
        self.assertIn("proxy_intercept_errors on;", sandbox)
        self.assertIn("error_page 502 504 = @unavailable;", sandbox)
        self.assertNotIn("error_page 401", sandbox)   # the app's own 401 and 403 pass
        self.assertIn("rewrite ^ /gate/page? break;", self.SCRIPT)
        self.assertIn("location = /.sbx/status {", self.SCRIPT)
        self.assertIn("location ^~ /gate/ { return 404; }", self.SCRIPT)   # the public never reaches them
        self.assertIn(r"proxy_no_cache \$upstream_http_x_sbx_upstream_token \$upstream_http_x_sbx_wait;",
                      self.SCRIPT)

    def test_a_cookie_runtimes_token_is_appended_to_the_apps_cookies_for_that_runtime_only(self):
        from sbx_control import ip_previews, runtime
        with patch.object(runtime, "ingresses", return_value={"embed": ("http://127.0.0.1:3002", ()),
                                                              "boat": ("https://sbx-ingress.on.boat.dev", ())}), \
                patch.object(runtime, "upstream_token_headers", return_value={}), \
                patch.object(runtime, "upstream_token_cookies", return_value={"boat": "_port_auth"}):
            maps = ip_previews.runtime_upstreams()
            self.assertIn('boat "${sbx_ingress_cookie_sep_1}_port_auth=${sbx_ingress_token}";', maps)
            self.assertIn("default $sandbox_cookie;", maps)   # any other runtime: the app's cookies as they are
            self.assertIn(r'"~^(?<before>.*?);\s*_port_auth=[^;]*(?<after>.*)$"', maps)   # a browser's own dropped
            self.assertEqual(ip_previews.upstream_cookie(), "sbx_ingress_cookie")
            self.assertIn("auth_request_set $sbx_ingress_token", ip_previews.upstream_token_set())
            self.assertEqual(ip_previews.upstream_token_send(), "")   # no header for a cookie runtime
            with patch.object(ip_previews, "port_range", return_value=(21000, 21000)):
                ip = ip_previews.nginx("203.0.113.10", "bootstrap")
        self.assertIn("proxy_set_header Cookie $ip_ingress_cookie;", ip)
        self.assertIn("error_page 502 504 = @ip_unavailable;", ip)
        self.assertIn("try_files /.sbx-no-such-file @ip_sandbox;", ip)
        self.assertIn("location = /.sbx/status {", ip)
        with patch.object(runtime, "ingresses", return_value={"embed": ("http://127.0.0.1:3002", ())}), \
                patch.object(runtime, "upstream_token_headers", return_value={}), \
                patch.object(runtime, "upstream_token_cookies", return_value={}):
            self.assertEqual(ip_previews.upstream_cookie(), "sandbox_cookie")
            self.assertNotIn("_port_auth", ip_previews.runtime_upstreams())
