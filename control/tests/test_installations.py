"""Two installations on one provider account: each sees, finds, prunes and
deletes only its own sandboxes and templates. They share one hosted
backend (the fake, as E2B Cloud's team would be shared) and differ only in
their installation id. Sandboxes from before the id tag are this
installation's when its own records know them by id."""
import json
from unittest.mock import patch

from support import Installation

FAKE = "sbx_control.runtime.fake:Fake"
ONE, OTHER = "1111111111111111", "2222222222222222"


class SharedAccount(Installation):
    RUNTIME_ENV = "SBX_RUNTIMES=fake\nFAKE_RUNTIME_TOKEN=test-only-fake\n"

    @classmethod
    def setUpClass(cls):
        from sbx_control import runtime
        cls.builtin = patch.dict(runtime.BUILTIN, {"fake": FAKE})
        cls.builtin.start()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.builtin.stop()

    def setUp(self):
        super().setUp()
        from sbx_control import runtime
        from sbx_control.runtime.fake import Fake
        self.runtime, self.shared = runtime, Fake()     # one provider account
        self.shared.built["tpl"] = "ready"
        self.enterContext(patch.dict(runtime._backends, {"fake": self.shared}))
        self.enterContext(patch.dict(runtime._index, clear=True))
        self.as_installation(ONE)

    def as_installation(self, installation_id):
        if getattr(self, "_as", None):
            self._as.stop()
        self._as = patch.object(self.runtime, "installation_id", return_value=installation_id)
        self._as.start()
        self.addCleanup(lambda: self._as.stop() if self._as else None)
        self.runtime._index.clear()


class TwoInstallations(SharedAccount):
    def test_each_sees_finds_and_deletes_only_its_own_sandboxes(self):
        from sbx_control import core
        mine = self.runtime.create("tpl", timeout=600, metadata={"sbx": "memos-a"}).sandbox_id
        self.as_installation(OTHER)
        theirs = self.runtime.create("tpl", timeout=600, metadata={"sbx": "memos-a"}).sandbox_id
        self.assertEqual(self.shared.boxes[theirs].metadata["sbx_install"], OTHER)
        self.assertEqual([i.sandbox_id for i in core.sandboxes()], [theirs])
        self.assertEqual(core.find("memos-a").sandbox_id, theirs)
        self.assertEqual(self.runtime.too_many(), "")
        with self.assertRaises(self.runtime.NotRunning):
            self.runtime.info(mine)                      # by id too: not this installation's
        self.as_installation(ONE)
        self.assertEqual([i.sandbox_id for i in core.sandboxes()], [mine])
        self.assertEqual(core.find("memos-a").sandbox_id, mine)
        self.assertTrue(self.runtime.kill(core.find("memos-a").sandbox_id))
        self.assertIn(theirs, self.shared.boxes)        # the other's stays
        self.assertIsNone(core.find("memos-a"))

    def test_calls_by_id_refuse_another_installation_s_sandbox(self):
        mine = self.runtime.create("tpl", timeout=600, metadata={"sbx": "a"}).sandbox_id
        self.as_installation(OTHER)          # its own index: it never listed or made mine
        for call in (lambda: self.runtime.pause(mine), lambda: self.runtime.set_timeout(mine, 600),
                     lambda: self.runtime.connect(mine), lambda: self.runtime.snapshot(mine, "s")):
            with self.assertRaises(self.runtime.NotRunning):
                call()
        self.assertFalse(self.runtime.kill(mine))
        self.assertIn(mine, self.shared.boxes)
        self.assertEqual(self.shared.boxes[mine].state, "running")
        self.as_installation(ONE)            # its own sandbox: as before, even unlisted
        self.runtime.pause(mine)
        self.assertEqual(self.shared.boxes[mine].state, "paused")

    def test_templates_are_named_and_pruned_per_installation(self):
        from sbx_control import template_builds, templates
        template_builds.build_now(templates.BASE, "fake")
        mine = template_builds.built(templates.BASE, "fake")
        self.assertTrue(mine.startswith(ONE[:8] + "--"))
        self.assertTrue(templates.named_for(mine, [templates.BASE]))
        theirs = OTHER[:8] + "--" + mine.partition("--")[2]
        stale_theirs = OTHER[:8] + "--sbx-base-00000000"
        self.shared.built[theirs] = self.shared.built[stale_theirs] = "ready"
        self.assertEqual({name for name, _ in self.runtime.templates("fake")}, {mine})
        self.shared.built[ONE[:8] + "--sbx-base-00000000"] = "ready"     # an earlier build of mine
        self.assertEqual(template_builds.prune("fake"), [ONE[:8] + "--sbx-base-00000000"])
        self.assertIn(theirs, self.shared.built)
        self.assertIn(stale_theirs, self.shared.built)                    # never another's
        self.assertEqual(template_builds.remove(templates.BASE, "fake")["deleted"], [mine])
        self.assertIn(theirs, self.shared.built)

    def test_legacy_sandboxes_known_by_id_are_adopted_unknown_ones_ignored(self):
        from sbx_control import core, state
        known = self.shared.create("tpl", timeout=600, metadata={"sbx": "old"}).sandbox_id     # before the tag
        unknown = self.shared.create("tpl", timeout=600, metadata={"sbx": "elsewhere"}).sandbox_id
        with state.db() as conn:
            conn.execute("insert into sandboxes (name, owner, branch, slug, runtime) values "
                         "('old', 'me', 'main', 'old', 'fake')")
            conn.execute("insert into sandbox_lifecycle (name, record) values ('old', ?)",
                         (json.dumps({"sandbox_id": known, "phase": "running"}),))
            conn.execute("delete from settings where key like 'runtime_legacy:%'")
        tagged = self.runtime.create("tpl", timeout=600, metadata={"sbx": "new"}).sandbox_id
        self.assertEqual(sorted(i.sandbox_id for i in core.sandboxes()), sorted([known, tagged]))
        self.assertEqual(core.find("old").sandbox_id, known)
        self.assertIsNone(core.find("elsewhere"))
        self.assertEqual(self.runtime.info(known).sandbox_id, known)
        with self.assertRaises(self.runtime.NotRunning):
            self.runtime.info(unknown)
        # Another installation adopts none of them.
        self.as_installation(OTHER)
        with patch.object(self.runtime, "_legacy", return_value=set()):
            self.assertEqual([i.sandbox_id for i in core.sandboxes()], [])
        # Once the legacy sandbox is gone, a full listing forgets it.
        self.as_installation(ONE)
        self.shared.kill(known)
        core.sandboxes()
        self.assertEqual(self.runtime._legacy("fake"), set())

    def test_a_runtime_on_this_host_keeps_its_names_and_lists_as_before(self):
        self.shared.host = True                       # one installation per host
        self.assertEqual(self.runtime.template_name("sbx-base-12345678", "fake"), "sbx-base-12345678")
        untagged = self.shared.create("tpl", timeout=600, metadata={"sbx": "x"}).sandbox_id
        self.assertEqual([i.sandbox_id for i in self.runtime.sandboxes()], [untagged])


class TemplateGoneBehindSbxsBack(SharedAccount):
    """A template another installation deleted (or one named before the
    installations were told apart): sbx builds it again instead of failing
    every create on it."""

    def built(self):
        from sbx_control import template_builds, templates
        template_builds.build_now(templates.BASE, "fake")
        template_builds.build_now(templates.DEFAULT, "fake")
        return template_builds, templates, template_builds.built(templates.DEFAULT, "fake")

    def start(self, template):
        from pathlib import Path
        from sbx_control import core, recipes
        a = recipes.App("owner/app", Path("/unused"), {})
        env = {"SBX_NAME": "box", "SBX_SLUG": "box", "SBX_OWNER": "me@example.test", "SBX_BRANCH": "main"}
        with patch.object(core, "runtime_layer", return_value=({}, None)), patch.object(core, "_envs", return_value={}):
            return core._start(a, env, {}, [], metadata={"template": template}, kind="fake")

    def test_a_create_on_a_template_that_is_gone_rebuilds_it_and_says_so(self):
        builds, templates, name = self.built()
        del self.shared.built[name]                     # another installation's prune
        with patch.object(builds, "start") as start, self.assertRaises(builds.NotBuilt) as refused:
            self.start(name)
        self.assertIn(f"the {templates.DEFAULT} template is building", str(refused.exception))
        self.assertIn("try again in a few minutes", str(refused.exception))
        start.assert_called_once_with(templates.DEFAULT, "fake")
        self.assertIsNone(builds.state_row(templates.DEFAULT, "fake")["built_template"])
        self.assertIn(templates.DEFAULT, builds.stale("fake"))
        # Not a build of sbx's (a snapshot, say): the runtime's error as it was.
        with self.assertRaises(self.runtime.TemplateGone):
            self.start("snapshot-that-went")

    def test_every_key_that_had_the_gone_template_starts_again(self):
        from sbx_control.state import db
        builds, templates, name = self.built()
        twin = templates.default_key({"cpu": 4, "memory_mb": 8192, "disk_mb": 20480})
        with db() as conn:   # another key recorded under the same name
            conn.execute("insert into template_builds (runtime, repo, status, built_fingerprint, built_template) "
                         "values ('fake', ?, 'built', 'x', ?)", (twin, name))
        del self.shared.built[name]
        with patch.object(builds, "start") as start:
            said = builds.gone(name, "fake")
        self.assertIn("is building", said)
        self.assertEqual(sorted(c.args for c in start.call_args_list),
                         sorted([(templates.DEFAULT, "fake"), (twin, "fake")]))

    def test_the_scheduler_rebuilds_what_the_runtime_no_longer_lists_once(self):
        builds, templates, name = self.built()
        self.assertEqual(builds.forget_missing("fake"), [])
        del self.shared.built[name]
        self.assertEqual(builds.forget_missing("fake"), [templates.DEFAULT])
        self.assertEqual(builds.forget_missing("fake"), [])          # once: no storm
        self.assertEqual(builds.stale("fake"), [templates.DEFAULT])
        builds.build_now(templates.DEFAULT, "fake")
        self.assertEqual(builds.forget_missing("fake"), [])

    def test_a_template_named_before_installations_were_told_apart_is_rebuilt_with_the_prefix(self):
        from sbx_control.state import db
        builds, templates, name = self.built()
        legacy = name.partition("--")[2]                # unprefixed, as built before
        self.shared.built[legacy] = "ready"
        with db() as conn:
            conn.execute("update template_builds set built_template = ? where runtime = 'fake' and repo = ?",
                         (legacy, templates.DEFAULT))
        self.assertEqual(builds.forget_missing("fake"), [templates.DEFAULT])
        builds.build_now(templates.DEFAULT, "fake")
        self.assertEqual(builds.built(templates.DEFAULT, "fake"), name)   # prefixed now
        self.assertEqual(builds.forget_missing("fake"), [])

    def test_a_runtime_on_this_host_is_not_listed_for_it(self):
        from sbx_control import template_builds
        self.built()
        self.shared.host = True
        with patch.object(self.runtime, "templates") as listed:
            self.assertEqual(template_builds.forget_missing("fake"), [])
        listed.assert_not_called()

    def legacy_base(self):
        """The base only under its name from before (as on the lab), and the
        default's last build failed starting from the prefixed name."""
        from sbx_control.state import db
        builds, templates, _ = self.built()
        prefixed = builds.built(templates.BASE, "fake")
        legacy = prefixed.partition("--")[2]
        del self.shared.built[prefixed]
        self.shared.built[legacy] = "ready"
        with db() as conn:
            conn.execute("update template_builds set built_template = ? where runtime = 'fake' and repo = ?",
                         (legacy, templates.BASE))
            # Never built since (the lab's repo template): nothing to start a sandbox from.
            conn.execute("update template_builds set built_template = null, built_fingerprint = null "
                         "where runtime = 'fake' and repo = ?", (templates.DEFAULT,))
        builds._transition(templates.DEFAULT, "building", "fake", started_at=builds._utc(), pid=None,
                           fingerprint=builds.plan(templates.DEFAULT).fingerprint,
                           error=f"template '{prefixed}' not found")   # no process: failed
        self.assertEqual(builds.status(templates.DEFAULT, builds.state_row(templates.DEFAULT, "fake"), "fake"),
                         "failed")
        return builds, templates, prefixed, legacy

    def test_a_child_waits_for_its_base_under_the_name_planned_now(self):
        builds, templates, prefixed, legacy = self.legacy_base()
        # A create in that window: building (and it starts), not "its last build failed".
        with patch.object(builds, "start") as start, self.assertRaises(builds.NotBuilt) as refused:
            builds.template_for(None, "fake")
        self.assertIn(f"the {templates.DEFAULT} template is building", str(refused.exception))
        start.assert_called_once_with(templates.DEFAULT, "fake")
        # The scheduler's round: the base first, then what starts from it.
        self.assertEqual(builds.stale("fake")[:2], [templates.BASE, templates.DEFAULT])
        builds.build_now(templates.DEFAULT, "fake")      # its own build makes the base first
        self.assertEqual(builds.built(templates.BASE, "fake"), prefixed)
        self.assertIn(prefixed, self.shared.built)
        self.assertEqual(builds.state(templates.DEFAULT, "fake")["status"], "built")
        self.assertEqual(builds.stale("fake"), [])

    def test_a_failure_with_its_base_current_stays_failed(self):
        import time
        builds, templates, prefixed, _ = self.legacy_base()
        builds.build_now(templates.BASE, "fake")
        time.sleep(1.1)                                   # a later attempt, the base unchanged since
        builds._transition(templates.DEFAULT, "building", "fake", started_at=builds._utc(), pid=None,
                           fingerprint=builds.plan(templates.DEFAULT).fingerprint, error="apt failed")
        with patch.object(builds, "start") as start, self.assertRaises(builds.NotBuilt) as refused:
            builds.template_for(None, "fake")
        self.assertIn("its last build failed (apt failed)", str(refused.exception))
        start.assert_not_called()
        self.assertNotIn(templates.DEFAULT, builds.stale("fake"))

    def test_a_base_that_failed_is_not_rebuilt_from_every_child(self):
        from sbx_control.state import db
        builds, templates, _ = self.built()
        # The base's last build failed on the plan of now: final until built again explicitly.
        builds._transition(templates.BASE, "building", "fake", started_at=builds._utc(), pid=None,
                           fingerprint=builds.plan(templates.BASE).fingerprint, error="apt failed")
        with db() as conn:   # the default never built since: a create needs it
            conn.execute("update template_builds set built_template = null, built_fingerprint = null "
                         "where runtime = 'fake' and repo = ?", (templates.DEFAULT,))
        with patch.object(builds, "build_now", wraps=builds.build_now) as build_now, \
                self.assertRaisesRegex(builds.BaseNotReady, "the sbx-base template failed.*apt failed.*fix it"):
            builds.start(templates.DEFAULT, "fake")
        build_now.assert_not_called()                              # the base is not built again from here
        self.assertNotEqual(builds.state(templates.DEFAULT, "fake")["status"], "building")
        with self.assertRaises(builds.NotBuilt) as refused:
            builds.template_for(None, "fake")
        self.assertIn("the sbx-base template failed", str(refused.exception))
        self.assertIn("apt failed", str(refused.exception))
        builds.build_now(templates.BASE, "fake")                   # built again explicitly
        builds.build_now(templates.DEFAULT, "fake")
        self.assertEqual(builds.state(templates.DEFAULT, "fake")["status"], "built")

    def test_a_child_waits_for_a_base_building_elsewhere_without_holding_a_slot(self):
        import os
        import time
        from sbx_control.state import db
        builds, templates, _ = self.built()
        with db() as conn:   # the base being built again, by another live process
            conn.execute("update template_builds set built_template = null where runtime = 'fake' and repo = ?",
                         (templates.BASE,))
        builds._transition(templates.BASE, "building", "fake", started_at=builds._utc(), pid=os.getppid())
        began = time.monotonic()
        with self.assertRaisesRegex(builds.BaseNotReady, "builds once it is built"):
            builds.start(templates.DEFAULT, "fake")
        self.assertLess(time.monotonic() - began, 2)                # no hour-long wait
        self.assertNotIn(("fake", templates.DEFAULT), builds._running)   # no slot held
        self.assertEqual(builds.state(templates.DEFAULT, "fake")["status"], "built")

    def test_the_e2b_adapter_names_a_missing_template(self):
        from e2b import Sandbox
        from e2b.exceptions import SandboxException
        from sbx_control.runtime.e2b_cloud import Cloud
        cloud = Cloud()
        cloud.configure({"E2B_CLOUD_API_KEY": "test-only"})
        missing = SandboxException("404: template 'usememos-memos-6b48cd3f' not found")
        missing.status_code = 404
        with patch.object(Sandbox, "create", side_effect=missing), \
                self.assertRaises(self.runtime.TemplateGone) as raised:
            cloud.create("usememos-memos-6b48cd3f", timeout=600)
        self.assertEqual(raised.exception.name, "usememos-memos-6b48cd3f")
        other = SandboxException("404: sandbox not found")
        other.status_code = 404
        with patch.object(Sandbox, "create", side_effect=other), self.assertRaises(SandboxException):
            cloud.create("tpl", timeout=600)                      # not about a template: as it was
