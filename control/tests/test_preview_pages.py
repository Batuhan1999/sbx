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
        self.assertIn('<span class="mark" aria-hidden="true">sb</span>', text)   # the sbx mark above the title
        self.assertIn("text-align: center", text)
        done = render(Page("deleted", "memos-a"))
        self.assertIn('aria-busy="false"', done)

    def test_a_visitor_who_may_not_know_gets_no_names(self):
        from sbx_control.preview_pages import Page
        neutral = Page("app_stopped", "memos-a", action=("Restart", "https://x")).neutral()
        self.assertEqual((neutral.name, neutral.action, neutral.state), ("", (), "starting"))


class GateCase(TwoRuntimes, Server):
    def setUp(self):
        super().setUp()
        from sbx_control import auth, core, gate, recipes, settings, waking
        self.gate, self.waking = gate, waking
        self.repo_admin()
        settings.update("domain", {**settings.current().domain.model_dump(), "name": "example.test"}, by="test")
        self.client.cookies.clear()
        self.app = recipes.App("owner/app", Path("/unused"), {"previews": {
            "": {"port": 3000, "access": "team"}, "desktop": {"port": 6080, "access": "owner"}}})
        self.enterContext(patch.object(core, "app_of", return_value=self.app))
        self.enterContext(patch.object(gate, "_snapshot", gate.Snapshot(0.0, {})))
        for kept in (waking._waking, waking._wake_errors, gate._listening, waking._last_access):
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


class Gate(GateCase):
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
        self.assertIn(">Open sbx<", shown.text)                 # to the web app's sandboxes page
        self.assertIn("/app/sandboxes\"", shown.text)
        self.assertNotIn("sbx ls", shown.text)
        self.assertEqual(self.page("memos-x.example.test", self.owner, html=False).json()["error"], "missing")

    def test_an_unavailable_service_host_is_a_starting_page(self):
        from sbx_control import addressing
        with patch.object(addressing, "service_hosts", return_value={"svc.example.test"}):
            shown = self.page("svc.example.test", self.owner, **{"X-Sbx-Reason": "unavailable"})
        self.assertEqual(shown.status_code, 503, shown.text)

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
        with patch.object(core, "attach", side_effect=slow_resume), patch.object(self.waking, "WAKE_WAIT_SECONDS", 0.05):
            response = self.ask("memos-a.example.test", self.owner)
            self.assertEqual((response.status_code, response.headers.get("x-sbx-page")), (403, "waking"))
            shown = self.page("memos-a.example.test", self.owner)
            self.assertIn("Waking up", shown.text)
            self.assertEqual(self.ask("memos-a.example.test", self.owner, "/gate/status").json()["state"], "waking")
            release.set()
            self.waking._waking[self.vm].join(5)
            with patch.object(self.gate, "_listens", return_value=False):
                self.assertEqual(self.ask("memos-a.example.test", self.owner, "/gate/status").json()["line"],
                                 "Starting the app")
            with patch.object(self.gate, "_listens", return_value=True):
                self.assertEqual(self.ask("memos-a.example.test", self.owner, "/gate/status").json()["state"], "ready")
            self.assertEqual(self.ask("memos-a.example.test", self.owner).status_code, 200)
        self.assertTrue(self.waking.resumed_for_preview(self.vm))
        self.waking.forget_preview_resume(self.vm)

    def test_a_resume_that_fails_says_what_to_do(self):
        from sbx_control import core
        self.runtime.pause(self.vm)
        self.gate.sandbox_owner(self.vm, fresh=True)
        with patch.object(core, "attach", side_effect=RuntimeError("no machine")):
            response = self.ask("memos-a.example.test", self.owner)
        self.assertEqual(response.headers.get("x-sbx-page"), "failed")

    def test_a_sandbox_running_again_drops_the_last_resume_s_error(self):
        from types import SimpleNamespace
        self.waking._wake_errors[self.vm] = "RuntimeError: the listing after the resume failed"
        self.assertTrue(self.waking.wake(self.vm, lambda fresh: SimpleNamespace(state="running")))
        self.assertIsNone(self.waking.failed(self.vm))

    def test_an_app_that_stopped_offers_its_owner_a_restart(self):
        from sbx_control import lifecycle
        op = lifecycle.begin("memos-a", "restart")
        op.advance("starting_app")
        op.advance("app_ready", finish=True)
        self.assertTrue(lifecycle.app_stopped("memos-a", self.vm, "its processes exited"))
        shown = self.page("memos-a.example.test", self.owner)
        self.assertEqual(shown.status_code, 502)
        self.assertIn("memos-a&#x27;s app stopped", shown.text)
        self.assertIn("/app/sandboxes/memos-a", shown.text)
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


class Review(GateCase):
    """What review found: the poll's sign-in and what it may touch, who
    learns of a sandbox, navigations before the app listens, the token cookie."""

    def test_the_poll_needs_a_sign_in_for_every_method_and_wakes_nothing(self):
        from sbx_control import core
        self.runtime.pause(self.vm)
        self.gate.sandbox_owner(self.vm, fresh=True)
        with patch.object(core, "attach") as resume, patch.object(self.runtime, "connect") as connect:
            for method in ("OPTIONS", "GET"):
                answer = self.ask("memos-a.example.test", None, "/gate/status", **{"X-Original-Method": method})
                self.assertEqual(answer.status_code, 401, method)
            found = self.ask("memos-a.example.test", self.owner, "/gate/status").json()
            resume.assert_not_called()
            connect.assert_not_called()
        self.assertEqual((found["state"], found["reload"]), ("waking", True))   # the page's navigation wakes it

    def test_the_poll_tells_no_one_else_of_an_owner_only_preview(self):
        found = self.ask("memos-a-desktop.example.test", self.other, "/gate/status").json()
        self.assertEqual(found["state"], "missing")
        self.assertNotIn("memos-a", found["title"].replace("memos-a-desktop", ""))

    def test_someone_else_learns_nothing_of_a_sandbox_being_made_or_deleted(self):
        from sbx_control import lifecycle, sandboxes
        sandboxes.register("memos-b", "operator@example.com", "main", "memos-b", kind="fake")
        lifecycle.begin("memos-b", "create").advance("allocating")
        self.assertIn("Starting memos-b", self.page("memos-b.example.test", self.owner).text)
        other = self.page("memos-b.example.test", self.other)
        self.assertEqual(other.status_code, 404)
        self.assertIn("No sandbox named memos-b", other.text)   # as for a name that doesn't exist
        self.assertNotIn("Creating", other.text)

    def test_a_navigation_waits_on_the_page_until_the_port_listens_and_other_requests_pass(self):
        with patch.object(self.gate, "_listens", return_value=False):
            navigation = self.ask("memos-a.example.test", self.owner, Accept="text/html")
            api = self.ask("memos-a.example.test", self.owner, Accept="application/json")
        self.assertEqual((navigation.status_code, navigation.headers.get("x-sbx-page")), (403, "starting"))
        self.assertEqual(api.status_code, 200)   # the app's own answer, whatever it is
        with patch.object(self.gate, "_listens", return_value=True):
            self.assertEqual(self.ask("memos-a.example.test", self.owner, Accept="text/html").status_code, 200)

    def test_a_cookie_runtime_gets_the_apps_cookies_without_any_token_cookie_then_its_own(self):
        self.fake.upstream_token_at = self.runtime.TokenPlace("cookie", "_port_auth")
        self.fake.upstream_token = lambda sandbox_id, port, fetch=True: "tok"
        response = self.ask("memos-a.example.test", self.owner,
                            Cookie="a=1; _port_auth=evil; sbx_session=x; b=2; _port_auth=again")
        self.assertEqual(response.headers["x-sbx-upstream-cookie"], "a=1; b=2; _port_auth=tok")

    def test_a_poll_that_lost_its_session_goes_to_the_login_and_errors_keep_polling(self):
        from sbx_control.preview_pages import Page, render
        text = render(Page("starting", "memos-a"), login="https://sbx.example.test/auth/login")
        self.assertIn("r.status === 401 && d.login", text)
        self.assertIn('"login": "https://sbx.example.test/auth/login"', text)
        self.assertIn("encodeURIComponent(location.href)", text)
        self.assertIn("if (!r.ok) return again();", text)
        self.assertIn(".catch(again)", text)


class Nginx(unittest.TestCase):
    """The nginx side, as tls.sh and ip_previews write it (also run in a real
    nginx:1.29 against fakes while building this)."""
    SCRIPT = (Path(__file__).parents[1] / "tls.sh").read_text()

    def test_refusals_and_upstream_errors_become_the_page_and_the_apps_own_errors_stay_its_own(self):
        from sbx_control import preview_proxy
        sandbox = self.SCRIPT[self.SCRIPT.index("  location @sandbox {"):]
        sandbox = sandbox[:sandbox.index("\n  }\n")]
        self.assertIn("try_files /.sbx-no-such-file @sandbox;", self.SCRIPT)   # after auth_request
        self.assertNotIn("proxy_intercept_errors on", sandbox)   # an app's own 502 or 504 is the app's
        self.assertIn("error_page 502 504 = @unavailable;", sandbox)   # nginx's own: not reachable, timed out
        self.assertNotIn("error_page 401", sandbox)   # the app's own 401 and 403 pass
        self.assertIn("preview_proxy page-locations", self.SCRIPT)
        pages = preview_proxy.page_locations(host="login.example.test", original_host="$host", preview_port='""',
                                             sandbox_headers=True)
        for location in ("location @denied {", "location @unavailable {", "location = /.sbx/status {"):
            self.assertIn(location, pages)
        self.assertIn("rewrite ^ /gate/page? break;", pages)
        self.assertIn("proxy_set_header X-Sbx-Reason unavailable;", pages)
        self.assertIn("location ^~ /gate/ { return 404; }", self.SCRIPT)   # the public never reaches them
        self.assertIn(r"proxy_no_cache \$upstream_http_x_sbx_upstream_token \$upstream_http_x_sbx_wait;",
                      self.SCRIPT)

    def test_every_upstream_gets_the_cookie_header_the_gate_wrote(self):
        from sbx_control import ip_previews, preview_proxy, runtime
        from sbx_control.runtime.base import TokenPlace
        self.assertNotIn("sandbox_cookie", self.SCRIPT)   # no second way: nginx strips nothing itself
        self.assertEqual(self.SCRIPT.count(r"auth_request_set \$sbx_cookie \$upstream_http_x_sbx_upstream_cookie;"), 2)
        self.assertEqual(self.SCRIPT.count(r"proxy_set_header Cookie \$sbx_cookie;"), 2)   # sandboxes and services
        self.assertIn(r"\$http_cookie|\$http_authorization", self.SCRIPT)   # the cached allow is the cookies'
        with patch.object(runtime, "ingresses", return_value={"embed": ("http://127.0.0.1:3002", ()),
                                                              "boat": ("https://sbx-ingress.on.boat.dev", ())}), \
                patch.object(runtime, "upstream_token_places", return_value={"boat": TokenPlace("cookie", "_port_auth")}):
            self.assertNotIn("_cookie", preview_proxy.runtime_upstreams())
            self.assertEqual(preview_proxy.upstream_token_set(), "")
            self.assertEqual(preview_proxy.upstream_token_send().strip(),
                             "proxy_cookie_flags _port_auth httponly secure samesite=strict;")
            with patch.object(ip_previews, "port_range", return_value=(21000, 21000)):
                ip = ip_previews.nginx("203.0.113.10", "bootstrap")
        self.assertIn("auth_request_set $ip_cookie $upstream_http_x_sbx_upstream_cookie;", ip)
        self.assertIn("proxy_set_header Cookie $ip_cookie;", ip)
        self.assertIn("error_page 502 504 = @ip_unavailable;", ip)
        self.assertNotIn("proxy_intercept_errors on", ip)
        self.assertIn("try_files /.sbx-no-such-file @ip_sandbox;", ip)
        self.assertIn("location = /.sbx/status {", ip)
        self.assertIn("location @ip_denied {", ip)

    def test_the_gate_writes_the_cookie_header(self):
        from sbx_control import preview_proxy
        self.assertEqual(preview_proxy.upstream_cookie("a=1; sbx_session=x; b=2"), "a=1; b=2")
        self.assertEqual(preview_proxy.upstream_cookie("a=1; _port_auth=evil; b=2", "_port_auth", "tok"),
                         "a=1; b=2; _port_auth=tok")
        self.assertEqual(preview_proxy.upstream_cookie("sbx_session=x", "_port_auth", "tok"), "_port_auth=tok")
        self.assertEqual(preview_proxy.upstream_cookie("_port_auth=kept"), "_port_auth=kept")   # not a cookie runtime


class PausedSinceListing(GateCase):
    def test_a_sandbox_paused_since_the_last_listing_gets_the_page_not_a_silent_wake(self):
        import time
        from sbx_control import core, lifecycle
        self.runtime.pause(self.vm)                     # the gate's listing still says running
        op = lifecycle.begin("memos-a", "pause")
        op.advance("pausing")
        op.advance("workspace_ready", finish=True, sandbox_id=self.vm, runtime="paused")
        with patch.object(core, "attach", side_effect=lambda *a, **k: time.sleep(1)), \
                patch.object(self.waking, "WAKE_WAIT_SECONDS", 0.05):
            response = self.ask("memos-a.example.test", self.owner, Accept="text/html")
        self.assertEqual(response.headers.get("x-sbx-page"), "waking")
