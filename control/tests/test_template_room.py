"""What a template build needs from the host before it starts, why a failed
one failed, and the templates the platform deletes again (prune, remove)."""
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import Installation, _builds_settled


class TemplateRoomTests(Installation):
    def setUp(self):
        super().setUp()
        from sbx_control import core, state, template_builds, templates
        from sbx_control.runtime.embed import storage
        self.core, self.storage, self.state, self.builds, self.templates = core, storage, state, template_builds, templates
        with state.db() as conn:
            conn.execute("delete from template_builds")

    def recipe(self, repo, text):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch("sbx_control.paths.apps", return_value=directory))
        (directory / repo / ".sbx").mkdir(parents=True)
        (directory / repo / ".sbx/sbx.yaml").write_text(text)

    def built(self, key, name):
        with self.state.db() as conn:
            conn.execute("insert into template_builds (runtime, repo, status, built_template, built_fingerprint) "
                         "values ('embed', ?, 'built', ?, 'x')", (key, name))

    # Before a build starts.

    def test_a_build_needs_twice_its_memory_free_and_says_so(self):
        # The launch lab: an 8 GiB build beside two running sandboxes failed as "An internal error occurred".
        with patch.object(self.core, "capacity", return_value={"known": True, "free_mb": 6 * 1024}), \
                patch.object(self.templates, "build") as build:
            with self.assertRaisesRegex(self.builds.NoRoom, r"needs 8\.0 GB of free sandbox memory .* 6\.0 GB is free"):
                self.builds.build_now(self.templates.DEFAULT)
        build.assert_not_called()
        self.assertEqual(self.builds.state(self.templates.DEFAULT), {})

    def test_a_build_needs_free_disk_for_two_of_its_steps(self):
        with patch.object(self.storage, "free_mb", return_value=10 * 1024), patch.object(self.templates, "build") as build:
            with self.assertRaisesRegex(self.builds.NoRoom, r"needs 16\.0 GB of free disk .* 10\.0 GB is free"):
                self.builds.build_now(self.templates.DEFAULT)
        build.assert_not_called()
        with patch.object(self.storage, "free_mb", return_value=40 * 1024), patch.object(self.templates, "build"):
            self.builds.build_now(self.templates.DEFAULT)
        self.assertEqual(self.builds.state(self.templates.DEFAULT)["status"], "built")

    def test_a_create_says_why_its_template_cannot_build_and_the_scheduler_tries_again(self):
        from sbx_control import settings
        self.recipe("owner/app", "template: yes\nresources: {memory_mb: 8192}\n")
        settings.add_repo("owner/app", by="test")
        self.built(self.templates.BASE, self.builds.plan(self.templates.BASE).name)
        with patch.object(self.core, "capacity", return_value={"known": True, "free_mb": 12 * 1024}):
            with self.assertRaisesRegex(self.builds.NotBuilt, "cannot build now: building the owner/app template needs 16.0 GB"):
                self.builds.template_for("owner/app")
            self.assertIn("owner/app", self.builds.stale())   # not failed: the next round tries again
            from sbx_control import template_scheduler
            with patch.object(self.builds, "build_now", side_effect=self.builds.NoRoom("fixture")) as build:
                template_scheduler._build_quietly("owner/app")
            build.assert_called_once()

    def test_a_nightly_rebuild_without_room_is_tried_again_the_next_night(self):
        from sbx_control import runtime, template_scheduler
        self.builds.mark_moved("owner/app")
        stop = SimpleNamespace(waits=0, is_set=lambda: False)
        stop.wait = lambda seconds: (setattr(stop, "waits", stop.waits + 1) or stop.waits > 1)
        night = time.localtime()[:3] + (self.builds.NIGHTLY_REBUILD_HOUR,) + time.localtime()[4:]
        with patch.object(runtime, "enabled", return_value=["embed"]), \
                patch.object(self.builds, "forget_missing"), patch.object(self.builds, "stale", return_value=[]), \
                patch.object(self.builds, "keys", return_value=["owner/app"]), patch.object(self.builds, "prune"), \
                patch.object(template_scheduler.time, "localtime", return_value=time.struct_time(night)), \
                patch.object(self.builds, "build_now", side_effect=self.builds.NoRoom("fixture")) as build:
            template_scheduler.schedule(stop)
        build.assert_called_once_with("owner/app", "embed")
        self.assertEqual(self.builds.moved(), ["owner/app"])

    def test_a_build_left_running_after_a_test_fails_it_by_name(self):
        with patch.object(self.builds, "_running", {("embed", "owner/app")}):
            with self.assertRaisesRegex(AssertionError, "still running after 0.1 s: .*owner/app"):
                _builds_settled(0.1)

    # While it runs.

    def test_a_build_that_filled_the_disk_says_so_and_its_layers_are_collected(self):
        free = iter([40 * 1024] * 3 + [2 * 1024] * 1000)

        def fill(*_, **__):
            time.sleep(0.2)
            raise RuntimeError("polling received unrecoverable error: 127.0.0.1:5008: connection refused")
        with patch.object(self.builds, "WATCH_SECONDS", 0.01), patch.object(self.storage, "free_mb", side_effect=lambda: next(free)), \
                patch.object(self.templates, "build", side_effect=fill), patch.object(self.builds, "collect_storage") as collect, \
                self.assertRaises(self.templates.BuildFailed):
            self.builds.build_now(self.templates.DEFAULT)
        error = self.builds.state(self.templates.DEFAULT)["error"]
        self.assertIn("disk (/var/lib/e2b) fell to 2.0 GB free", error)
        self.assertIn("the runtime said: polling received unrecoverable error", error)
        collect.assert_called_once()

    def test_a_failure_the_watch_cannot_explain_keeps_the_runtime_s_words(self):
        with patch.object(self.templates, "build", side_effect=RuntimeError("step 3 exited 1")), \
                patch.object(self.builds, "collect_storage"), self.assertRaises(RuntimeError):
            self.builds.build_now(self.templates.DEFAULT)
        self.assertEqual(self.builds.state(self.templates.DEFAULT)["error"], "step 3 exited 1")

    # Afterwards.

    def runtime(self, names, used=(), building=()):
        listed = [self.templates.Built(n, "building" if n in building else "ready") for n in names]
        sandboxes = [SimpleNamespace(metadata={"sbx": f"s{i}", "template": n}) for i, n in enumerate(used)]
        deleted = []
        self.enterContext(patch.object(self.templates, "listing", return_value=listed))
        self.enterContext(patch.object(self.templates, "delete", side_effect=lambda n, kind=None: deleted.append(n) or True))
        self.enterContext(patch.object(self.core, "sandboxes", return_value=sandboxes))
        self.enterContext(patch.object(self.builds, "collect_storage"))
        return deleted

    def test_prune_deletes_earlier_builds_and_sizes_nobody_uses_and_nothing_else(self):
        base, default = self.builds.plan(self.templates.BASE).name, self.builds.plan(self.templates.DEFAULT).name
        self.built(self.templates.BASE, base)
        self.built(self.templates.DEFAULT, default)
        self.built("default:4cpu-8192mb-8192mb", "default-4cpu-8192mb-8192mb-33333333")
        deleted = self.runtime(
            [base, "sbx-base-11111111", default, "default-22222222", "default-4cpu-8192mb-8192mb-33333333",
             "default-44444444", "base", "my-own-template-55555555", "sbx-amp-project-0123456789abcdef01234567"],
            used=["default-22222222"], building=["default-44444444"])
        self.assertEqual(self.builds.prune(), ["default-4cpu-8192mb-8192mb-33333333", "sbx-base-11111111"])
        self.assertEqual(sorted(deleted), ["default-4cpu-8192mb-8192mb-33333333", "sbx-base-11111111"])
        self.assertEqual(sorted(self.builds.builds()), sorted([self.templates.BASE, self.templates.DEFAULT]))

    def test_an_unused_size_s_row_stays_while_a_sandbox_runs_from_its_template(self):
        self.built("default:4cpu-8192mb-8192mb", "default-4cpu-8192mb-8192mb-33333333")
        self.runtime(["default-4cpu-8192mb-8192mb-33333333"], used=["default-4cpu-8192mb-8192mb-33333333"])
        self.assertEqual(self.builds.prune(), [])
        self.assertIn("default:4cpu-8192mb-8192mb", self.builds.builds())   # size_of still reads it

    def test_remove_deletes_a_key_s_templates_and_its_failed_build(self):
        with self.state.db() as conn:
            conn.execute("insert into template_builds (runtime, repo, status, error) values ('embed', 'owner/app', 'failed', 'disk full')")
        deleted = self.runtime(["owner-app-aaaaaaaa", "owner-app-bbbbbbbb", "owner-other-cccccccc"],
                               used=["owner-app-aaaaaaaa"])
        self.assertEqual(self.builds.remove("owner/app"), {"key": "owner/app", "deleted": ["owner-app-bbbbbbbb"],
                                                           "kept_in_use": ["owner-app-aaaaaaaa"], "rebuilds": False})
        self.assertEqual(deleted, ["owner-app-bbbbbbbb"])
        self.assertEqual(self.builds.state("owner/app"), {})

    def test_remove_refuses_a_running_build(self):
        self.builds._running.add(("embed", "owner/app"))
        self.addCleanup(self.builds._running.discard, ("embed", "owner/app"))
        self.runtime([])
        with self.assertRaisesRegex(self.builds.Busy, "is building"):
            self.builds.remove("owner/app")


class DefaultBranchTests(Installation):
    """A repo on the default template has no mirror: after a restart of the
    control service, or when added with sbx recipe apply, its default branch
    was taken to be main (octocat/hello-world on the launch lab: master)."""

    def test_a_repo_without_a_mirror_asks_its_remote_once(self):
        import contextlib
        import subprocess
        from sbx_control import extensions, recipes
        remote_head = self.remote_head
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        remote = directory / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(remote)], check=True)
        work = directory / "work"
        subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "commit", "-q", "--allow-empty", "-m", "one"], check=True)
        subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD:master"], check=True, capture_output=True)
        asked = []

        @contextlib.contextmanager
        def access(repo):
            asked.append(repo)
            yield str(remote), None
        recipes._reported.pop("owner/plain", None)
        self.addCleanup(recipes._reported.pop, "owner/plain", None)
        with patch.object(recipes, "remote_head", new=remote_head), patch.object(extensions, "git_access", side_effect=access):
            self.assertEqual(recipes.default_branch("owner/plain"), "master")
            self.assertEqual(recipes.default_branch("owner/plain"), "master")
        self.assertEqual(asked, ["owner/plain"])

    def test_a_remote_that_cannot_be_asked_still_gives_main(self):
        from sbx_control import extensions, recipes
        remote_head = self.remote_head
        recipes._reported.pop("owner/unreachable", None)
        with patch.object(recipes, "remote_head", new=remote_head), \
                patch.object(extensions, "git_access", side_effect=LookupError("no extension reaches it")):
            self.assertEqual(recipes.default_branch("owner/unreachable"), "main")
        self.assertNotIn("owner/unreachable", recipes._reported)
