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


class TwoInstallations(Installation):
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
