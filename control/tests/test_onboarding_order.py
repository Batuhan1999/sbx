"""The onboarding order: templates build as soon as there is a runtime and
an address, during the setup too (every template has every coding agent's
layer whether chosen or not, and the setup's reachability check needs a
built base); choosing coding agents is optional; a later change to an
extension says what it rebuilds."""
import io
import os
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from support import Server

from sbx_control import reachability


class TemplatesDuringTheSetup(Server):
    def setUp(self):
        super().setUp()
        from sbx_control import setup, template_builds, template_scheduler, templates
        self.setup, self.builds, self.scheduler, self.templates = setup, template_builds, template_scheduler, templates

    def amp(self):
        from sbx_control import extensions
        return extensions.get("amp")

    def test_templates_build_before_the_setup_is_finished(self):
        """The setup's reachability check starts a sandbox from the base template, so nothing may wait
        for the setup to finish: the scheduler, a runtime just added and install.sh's builds all start."""
        self.choose_ip()
        self.create_admin()
        self.assertFalse(self.setup.complete())
        self.assertNotIn("blocked", self.client.get("/api/templates", headers=self.headers).json())
        with patch.object(self.scheduler, "_build_quietly") as build, \
                patch.object(self.scheduler, "start_detached", return_value=("unit", True)) as detached, \
                patch.object(self.builds, "build_now") as build_now:
            self.scheduler.build_stale(kinds=["embed"])          # the scheduler's round
            with redirect_stdout(io.StringIO()):
                self.scheduler.start_template_builds("embed")   # a runtime just added
            with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                self.assertEqual(self.builds.main(["base", "--if-stale"]), 0)   # install.sh's
        # No coding agents chosen: finishing does not wait for them.
        self.assertTrue(self.client.get("/api/setup", headers=self.headers).json()["can_complete"])
        self.assertEqual([c.args for c in build.call_args_list],
                         [(self.templates.BASE, "embed"), (self.templates.DEFAULT, "embed")])
        detached.assert_called_once_with("embed")
        build_now.assert_called_once_with(self.templates.BASE, None)

    def test_a_runtime_added_during_the_setup_starts_the_base_template(self):
        from sbx_control import runtime_changes
        self.choose_ip()
        self.create_admin()
        with patch.object(self.scheduler, "start_detached", return_value=("unit", True)) as detached, \
                patch.object(runtime_changes, "perform", return_value="E2B Embed (embed) is enabled"), \
                patch.object(runtime_changes.settings, "request_effects"), redirect_stdout(io.StringIO()) as said:
            runtime_changes.commit("add", "embed", {})
        self.assertFalse(self.setup.complete())
        detached.assert_called_once_with("embed")
        self.assertIn(f"building {self.templates.BASE}", said.getvalue())

    def test_the_setup_finishes_while_the_base_template_is_building(self):
        """The deadlock this replaces: finishing waited for a base template that built only after finishing."""
        from sbx_control import template_builds
        self.choose_ip()
        self.create_admin()
        self.choose_agents("amp")
        template_builds._transition(self.templates.BASE, "building", started_at=template_builds._utc(),
                                    pid=os.getpid())
        reach = reachability.check("embed")                    # what the setup's checks run once the runtime answers
        self.assertTrue(reach.ok, reach)
        self.assertIn("not checked yet: the base template is still building", reach.detail)
        self.assertTrue(self.client.get("/api/setup/checks", headers=self.headers).json()["ok"])
        done = self.client.post("/api/setup/complete", headers=self.headers)
        self.assertEqual(done.status_code, 200, done.text)
        self.assertTrue(done.json()["complete"])
        self.kick.assert_called()                              # what is still stale builds now

    def test_a_round_now_whether_or_not_the_setup_is_finished(self):
        import threading
        ran = threading.Event()
        with patch.object(self.scheduler, "build_stale", side_effect=lambda: ran.set()):
            self.assertFalse(self.setup.complete())
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

    def test_an_installation_set_up_before_the_step_counts_as_having_chosen(self):
        self.setup.mark_complete("test")   # no choice recorded: set up before the step existed
        self.assertTrue(self.setup.agents_chosen())
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

    def test_turning_amp_off_during_the_setup_rebuilds_nothing(self):
        self.choose_ip()
        self.create_admin()
        self.choose_agents("amp")
        report = self.client.delete("/api/connections/amp", headers=self.headers).json()["rebuilds"]
        self.assertFalse(report["building"])
        self.kick.assert_not_called()

    def test_the_templates_and_where_their_builds_stand(self):
        self.repo_admin()
        found = self.client.get("/api/templates").json()
        self.assertEqual(list(found), ["runtimes"])
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
        self.kick.assert_called()
