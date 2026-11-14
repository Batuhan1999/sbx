"""The rules that say what a repo needs next and who acts (readiness.py),
and which operations a sandbox takes now (lifecycle.actions): what the web
app shows instead of working them out itself."""
import unittest

from sbx_control import lifecycle, readiness

INFO = {"repo": "usememos/memos", "trust": "trusted", "configured": True, "prebuilt": True, "previews": ["web"],
        "secrets_missing": [], "github": "app", "github_app_install": "https://github.com/apps/x/installations/new"}
BUILT = {"status": "built", "stale": False, "template": "owner-app-abc"}


def attachment(state, problem=None):
    return {"key": "db", "state": state, "problem": problem}


def progress(info=None, atts=(), missing=(), build=BUILT, live=False):
    return readiness.steps({**INFO, **(info or {})}, list(atts), list(missing), build, live)


def step(found, name):
    return next(s for s in found["steps"] if s["step"] == name)


class ReadinessTests(unittest.TestCase):
    def test_a_repo_without_a_recipe_goes_to_the_agent_and_blocks_what_depends_on_it(self):
        found = progress({"configured": False, "prebuilt": False}, build={})
        self.assertEqual((found["stage"], found["next"]), ("agent", "configured"))
        self.assertEqual((step(found, "configured")["status"], step(found, "configured")["who"]), ("todo", "agent"))
        for name in ("services", "env", "template", "sandbox"):
            self.assertEqual(step(found, name)["status"], "blocked")

    def test_a_person_is_asked_for_the_missing_values_by_name(self):
        found = progress(missing=["APP_SECRET", "STRIPE_SECRET_KEY"])
        self.assertEqual(found["stage"], "person")
        env = step(found, "env")
        self.assertEqual((env["status"], env["who"], env["missing"]), ("todo", "person", ["APP_SECRET", "STRIPE_SECRET_KEY"]))

    def test_each_attachment_problem_goes_to_whoever_can_fix_it(self):
        values = step(progress(atts=[attachment("needs_values", "needs STRIPE_SECRET_KEY")]), "services")
        self.assertEqual((values["who"], values["missing"]), ("admin", ["db"]))
        self.assertEqual(step(progress(atts=[attachment("needs_connection")]), "services")["who"], "admin")
        broken = progress(atts=[attachment("error", "no package pg in the catalog")])
        self.assertEqual((step(broken, "services")["who"], broken["stage"]), ("agent", "agent"))
        self.assertEqual(step(progress(atts=[attachment("disabled")]), "services")["status"], "done")

    def test_the_template_step_follows_its_build(self):
        building = progress(build={"status": "building"})
        self.assertEqual((step(building, "template")["status"], step(building, "template")["who"], building["stage"]),
                         ("working", "platform", "working"))
        failed = step(progress(build={"status": "failed", "error": "pnpm install failed"}), "template")
        self.assertEqual((failed["who"], failed["detail"]), ("agent", "pnpm install failed"))
        self.assertEqual(step(progress(build={**BUILT, "stale": True}), "template")["who"], "admin")
        self.assertEqual(step(progress(build={}), "template")["status"], "todo")

    def test_a_repo_without_its_own_template_waits_for_the_default_one(self):
        # Its sandboxes start from the default template, which create refuses until it is built.
        default = {"status": "built", "stale": True, "template": "sbx-default-2g-abc"}
        self.assertEqual(step(progress({"prebuilt": False}, build=default), "template")["status"], "done")
        missing = step(progress({"prebuilt": False}, build={}), "template")
        self.assertEqual((missing["status"], missing["who"]), ("todo", "admin"))
        failed = progress({"prebuilt": False}, build={"status": "failed", "error": "apt failed"})
        self.assertEqual((step(failed, "template")["who"], failed["stage"]), ("admin", "person"))
        self.assertEqual(step(progress({"prebuilt": False}, build={"status": "building"}), "template")["status"], "working")

    def test_ready_leaves_only_the_first_sandbox_and_live_has_nothing_left(self):
        ready = progress()
        self.assertEqual((ready["stage"], ready["next"]), ("ready", "sandbox"))
        live = progress(live=True)
        self.assertEqual((live["stage"], live["done"], live["next"]), ("live", live["total"], None))

    def test_a_deploy_key_is_github_access_and_anonymous_is_only_for_a_reachable_repo(self):
        self.assertEqual(step(progress({"github": "key"}), "github")["status"], "done")
        unknown = step(progress({"github": "anonymous"}), "github")
        self.assertEqual((unknown["status"], unknown["who"], unknown["action"]),
                         ("todo", "admin", INFO["github_app_install"]))
        # A trusted public repo without the App: cloned anonymously, ready without it.
        found = progress({"github": "anonymous", "github_anonymous": True})
        self.assertEqual(step(found, "github")["status"], "done")
        self.assertIn("install the GitHub App to push", step(found, "github")["detail"])
        self.assertEqual(found["stage"], "ready")

    def test_a_public_repo_needs_no_github_access_and_no_secrets(self):
        # Cloned anonymously; the env routes refuse values for it, so none can be missing.
        found = progress({"trust": "public", "github": "anonymous", "prebuilt": False}, missing=["APP_SECRET"],
                         build={"status": "built", "template": "sbx-default-2g-abc"})
        self.assertEqual((step(found, "github")["status"], step(found, "env")["status"]), ("done", "done"))
        self.assertEqual(step(found, "env")["missing"], [])
        self.assertEqual(found["stage"], "ready")

    def test_a_public_repo_that_declares_services_goes_to_the_agent(self):
        # Create refuses services for a repo the installation does not trust.
        found = progress({"trust": "public", "github": "anonymous", "prebuilt": False, "services": ["db"]},
                         build={"status": "built", "template": "sbx-default-2g-abc"})
        services = step(found, "services")
        self.assertEqual((services["status"], services["who"], services["missing"]), ("todo", "agent", ["db"]))
        self.assertEqual(found["stage"], "agent")

    def test_the_list_says_the_first_thing_a_repo_needs(self):
        def state(trust="trusted", configured=True, prebuilt=True, build=BUILT, env=0, packages=0):
            return readiness.list_state(trust, configured, prebuilt, build, env, packages)
        self.assertEqual(state(trust=None)["label"], "Not served")
        self.assertEqual(state(configured=False), {"label": "Needs a recipe", "level": "warn", "who": "agent"})
        self.assertEqual(state(build={"status": "building"})["level"], "progress")
        self.assertEqual(state(build={"status": "failed"})["level"], "bad")
        self.assertEqual(state(build={})["label"], "Template not built")
        self.assertEqual(state(build={**BUILT, "stale": True})["label"], "Template stale")
        self.assertEqual(state(build={"status": "built", "stale": False})["label"], "Template not built")
        self.assertEqual(state(prebuilt=False, build={"status": "built", "template": "sbx-default-2g-abc"})["label"],
                         "Ready")
        self.assertEqual(state(prebuilt=False, build={})["label"], "Template not built")
        self.assertEqual(state(env=2), {"label": "2 values missing", "level": "warn", "who": "person"})
        self.assertEqual(state(packages=1, env=1)["label"], "A package needs attention")


def record(**over):
    return {"active": False, "operation": "create", "phase": "app_ready", **over}


class ActionsTests(unittest.TestCase):
    def test_a_settled_sandbox_offers_what_its_state_allows(self):
        self.assertEqual(lifecycle.actions(record(), "up", "running"),
                         {"pause": True, "resume": False, "restart": True, "remove": True})
        self.assertEqual(lifecycle.actions(record(), "paused", "paused"),
                         {"pause": False, "resume": True, "restart": False, "remove": True})
        self.assertEqual(lifecycle.actions(record(phase="failed"), "failed", "running"),
                         {"pause": False, "resume": False, "restart": True, "remove": True})

    def test_a_running_operation_allows_only_delete_unless_it_is_the_delete(self):
        busy = record(active=True, operation="restart", phase="starting_app")
        self.assertEqual(lifecycle.actions(busy, "starting", "running"),
                         {"pause": False, "resume": False, "restart": False, "remove": True})
        deleting = record(active=True, operation="delete", phase="deleting")
        self.assertFalse(lifecycle.actions(deleting, "starting", "running")["remove"])
        self.assertFalse(any(lifecycle.actions(record(phase="deleted"), "deleted").values()))


if __name__ == "__main__":
    unittest.main()
