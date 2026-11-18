"""The onboarding order: no template builds before the setup is finished;
choosing coding agents is optional (every template has every agent's
layer); a later change to an extension says what it rebuilds."""
import io
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from support import Server


class TemplateGate(Server):
    def setUp(self):
        super().setUp()
        from sbx_control import setup, template_builds, template_scheduler, templates
        self.setup, self.builds, self.scheduler, self.templates = setup, template_builds, template_scheduler, templates

    def amp(self):
        from sbx_control import extensions
        return extensions.get("amp")

    def test_nothing_builds_until_the_setup_is_finished(self):
        self.choose_ip()
        self.create_admin()
        self.assertEqual(self.setup.templates_blocked(), "templates build once the setup is finished")
        # sbx templates says why while the setup is open (not the setup gate's 503).
        self.assertIn("setup is finished", self.client.get("/api/templates", headers=self.headers).json()["blocked"])
        with patch.object(self.scheduler, "_build_quietly") as build, \
                patch.object(self.scheduler, "start_detached") as detached, \
                patch.object(self.builds, "build_now") as build_now:
            self.scheduler.build_stale()                       # the scheduler's round
            out = io.StringIO()
            with redirect_stdout(out):
                self.scheduler.start_template_builds("embed")   # a runtime just added
            self.assertIn("not yet", out.getvalue())
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                self.assertEqual(self.builds.main(["base", "--if-stale"]), 0)   # install.sh's
            self.assertIn("NOT YET", err.getvalue())
            build.assert_not_called()
            detached.assert_not_called()
            build_now.assert_not_called()
        state = self.client.get("/api/setup", headers=self.headers).json()
        self.assertTrue(state["can_complete"])   # no coding agents chosen: optional
        self.kick.assert_not_called()
        done = self.client.post("/api/setup/complete", headers=self.headers)
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(self.setup.templates_blocked(), "")
        self.kick.assert_called_once()                         # the first templates start now
        with patch.object(self.scheduler, "_build_quietly") as build:
            self.scheduler.build_stale(kinds=["embed"])
        self.assertEqual([c.args for c in build.call_args_list],
                         [(self.templates.BASE, "embed"), (self.templates.DEFAULT, "embed")])

    def test_a_round_now_only_once_templates_may_build(self):
        import threading
        ran = threading.Event()
        with patch.object(self.scheduler, "build_stale", side_effect=lambda: ran.set()) as round_:
            self.assertFalse(self.real_kick())
            round_.assert_not_called()
            self.setup.mark_complete("test")
            self.assertTrue(self.real_kick())
            self.assertTrue(ran.wait(5))

    def test_amp_is_in_the_base_template_whether_it_is_chosen_or_not(self):
        from sbx_control import vault
        from sbx_control.connectors.amp.common import TOKEN
        base, default = self.builds.plan(self.templates.BASE), self.builds.plan(self.templates.DEFAULT)
        self.assertIn("amp", [c.name for c in base.contributions])
        self.assertNotIn("amp", [c.name for c in default.contributions])
        self.choose_ip()
        self.create_admin()
        self.choose_agents("amp", "claude")
        self.assertFalse(vault.facts(TOKEN).set)
        self.assertEqual(self.builds.plan(self.templates.BASE).fingerprint, base.fingerprint)   # choosing it rebuilds nothing
        self.assertEqual(self.builds.plan(self.templates.DEFAULT).fingerprint, default.fingerprint)
        self.assertFalse(self.amp().settings().enabled)        # the provider waits for its token
        self.assertTrue(self.amp().status().facts["chosen"])   # listed as added, waiting on its token
        self.choose_agents()
        self.assertEqual(self.builds.plan(self.templates.BASE).fingerprint, base.fingerprint)

    def test_the_choice_is_checked_and_belongs_to_the_setup(self):
        self.choose_ip()
        self.create_admin()
        unknown = self.client.put("/api/setup/agents", headers=self.headers, json={"agents": ["nope"]})
        self.assertEqual(unknown.status_code, 400)
        self.assertIn("claude, amp", unknown.text)
        chosen = self.choose_agents("amp")
        self.assertEqual({a["kind"]: a["chosen"] for a in chosen["agents"]}, {"claude": False, "amp": True})
        self.assertEqual(self.choose_agents()["steps"][-1], {"step": "agents", "done": True, "optional": True,
                                                             "summary": "none"})
        self.assertEqual(self.client.post("/api/setup/complete", headers=self.headers).status_code, 200)
        late = self.client.put("/api/setup/agents", headers=self.headers, json={"agents": ["amp"]})
        self.assertEqual(late.status_code, 409)
        self.assertIn("Extensions", late.text)

    def test_an_installation_set_up_before_the_step_is_not_blocked(self):
        self.setup.mark_complete("test")   # no choice recorded: set up before the step existed
        self.assertTrue(self.setup.agents_chosen())
        self.assertEqual(self.setup.templates_blocked(), "")
        self.assertEqual(self.setup.state(None)["steps"][-1]["summary"], "as enabled under Extensions")

    def test_turning_amp_off_later_rebuilds_nothing(self):
        self.choose_ip()
        self.create_admin()
        self.choose_agents("amp")
        self.client.post("/api/setup/complete", headers=self.headers)
        self.kick.reset_mock()
        response = self.client.delete("/api/connections/amp")
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()["rebuilds"]
        self.assertEqual((report["runtimes"], report["building"]), ({}, False))
        self.assertIn("nothing rebuilds", report["message"])
        self.kick.assert_not_called()
        self.assertIn("amp", [c.name for c in self.builds.plan(self.templates.BASE).contributions])

    def test_a_change_before_the_setup_is_finished_builds_nothing(self):
        self.choose_ip()
        self.create_admin()
        self.choose_agents("amp")
        report = self.client.delete("/api/connections/amp", headers=self.headers).json()["rebuilds"]
        self.assertFalse(report["building"])
        self.kick.assert_not_called()

    def test_the_templates_and_where_their_builds_stand(self):
        self.repo_admin()
        found = self.client.get("/api/templates").json()
        self.assertEqual(found["blocked"], "")
        self.assertEqual([(t["key"], t["phase"]) for t in found["runtimes"]["embed"]],
                         [(self.templates.BASE, "stale"), (self.templates.DEFAULT, "stale")])
        self.client.get("/logout")
        self.assertEqual(self.client.get("/api/templates").status_code, 401)


class FinishAsTheSignedInAdmin(Server):
    """The agent signs in with `sbx login` once the owner's account exists, and finishes the setup itself:
    an admin's API token, no password; the setup link with the password still works (test_runtime_settings)."""

    def test_sbx_login_works_during_the_setup_and_its_token_finishes_it(self):
        from fastapi.testclient import TestClient
        from sbx_control import auth, users
        self.choose_ip()
        self.create_admin()
        self.choose_agents("amp")
        agent = TestClient(self.app.app, base_url="https://203.0.113.10")   # no session, no setup link
        self.addCleanup(agent.close)
        login = agent.get("/cli/login", params={"port": 4000, "state": "s", "challenge": "c" * 43},
                          follow_redirects=False)
        self.assertEqual(login.status_code, 307, login.text)                # to the sign-in page, not "being set up"
        self.assertTrue(login.headers["location"].startswith("/login?"), login.headers["location"])
        self.assertEqual(agent.post("/cli/token", json={"code": "x", "verifier": "y"}).status_code, 400)
        self.assertEqual(agent.get("/api/me").status_code, 401)
        self.assertEqual(agent.get("/api/sandboxes").status_code, 503)     # the rest stays closed

        users.set_password("dev@example.com", "test-only-password-456", by="test")   # a person, not an admin
        person = {"Authorization": f"Bearer {auth.mint_token('dev@example.com', 'cli')}"}
        self.assertEqual(agent.post("/api/setup/complete", headers=person).status_code, 403)
        admin = {"Authorization": f"Bearer {auth.mint_token('operator@example.com', 'cli')}"}
        self.assertFalse(agent.get("/api/me", headers=admin).json()["setup_complete"])
        done = agent.post("/api/setup/complete", headers=admin)
        self.assertEqual(done.status_code, 200, done.text)
        self.assertTrue(done.json()["complete"])
        self.kick.assert_called_once()
