"""The host command that builds a template runs end to end."""
import contextlib
import io
import unittest
from unittest import mock

from support import Installation


class BuildCommand(Installation):
    SET_UP = True

    def test_reports_the_built_template(self):
        from sbx_control import template_builds, templates
        stderr = io.StringIO()
        with mock.patch.object(templates, "build") as build, contextlib.redirect_stderr(stderr):  # E2B's builder
            self.assertEqual(template_builds.main(["base"]), 0)
        built = template_builds.plan(templates.BASE).name
        self.assertEqual(build.call_args.args[0].name, built)
        self.assertIn(f"BUILT {built}", stderr.getvalue())
        self.assertEqual(template_builds.built(templates.BASE), built)

    def test_the_installer_builds_only_what_is_missing_or_stale(self):
        from sbx_control import template_builds, templates
        quiet = contextlib.redirect_stderr(io.StringIO())
        with mock.patch.object(templates, "build") as build, quiet:
            for _ in range(2):   # install.sh run twice
                self.assertEqual(template_builds.main(["--if-stale", "base"]), 0)
                self.assertEqual(template_builds.main(["--if-stale", "default"]), 0)
            self.assertEqual([c.args[0].key for c in build.call_args_list], [templates.BASE, templates.DEFAULT])
            build.reset_mock()
            # The platform layer changed: both are stale; a build that failed is tried again.
            with mock.patch.object(template_builds.templates, "base_steps", return_value=[("run", "true")]):
                build.side_effect = [RuntimeError("builder down"), None, None]
                with self.assertRaises(RuntimeError):
                    template_builds.main(["--if-stale", "base"])
                self.assertEqual(template_builds.main(["--if-stale", "base"]), 0)
                self.assertEqual(template_builds.main(["--if-stale", "default"]), 0)
            self.assertEqual([c.args[0].key for c in build.call_args_list], [templates.BASE] * 2 + [templates.DEFAULT])

    def test_the_upgrade_says_which_templates_it_rebuilds(self):
        from sbx_control import template_builds, templates

        def summary():
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(template_builds.main(["summary"]), 0)
            return out.getvalue().strip()
        self.assertIn("about 10 minutes the first time", summary())
        with mock.patch.object(templates, "build"), contextlib.redirect_stderr(io.StringIO()):
            template_builds.main(["base"])
            template_builds.main(["default"])
        self.assertEqual(summary(), "Templates: up to date, none rebuilds")
        with mock.patch.object(template_builds.templates, "DEFAULT_STEPS", ("echo changed",)):
            self.assertEqual(summary(), "Templates: default rebuilds now (about 10 minutes)")
        with mock.patch.object(template_builds.templates, "base_steps", return_value=[("run", "true")]), \
                mock.patch.object(template_builds, "keys", return_value=[templates.BASE, templates.DEFAULT, "owner/app"]):
            self.assertEqual(summary(), "Templates: sbx-base and default rebuild now (about 10 minutes); "
                                        "owner/app rebuilds in the background afterwards")
            # A build that failed on this plan is not the scheduler's to retry.
            with template_builds.db() as conn:
                conn.execute("insert into template_builds (runtime, repo, status, fingerprint) values ('embed', ?, 'failed', ?)",
                             ("owner/app", template_builds.plan("owner/app").fingerprint))
            self.assertEqual(summary(), "Templates: sbx-base and default rebuild now (about 10 minutes); "
                                        "owner/app failed on this plan: fix and build again with "
                                        "sbx-admin jobs run template@<runtime>:<key>")

    def test_an_upgrade_keeps_a_stale_template_the_host_has_no_room_to_rebuild(self):
        # The launch lab: Amp was enabled, the default template went stale, and with 3 GB of
        # sandbox memory free the installer failed on its rebuild although the old one served.
        from sbx_control import template_builds, templates
        with mock.patch.object(templates, "build"), contextlib.redirect_stderr(io.StringIO()):
            template_builds.main(["base"])
        no_room = template_builds.NoRoom("building the default template needs 8.0 GB of free sandbox memory")
        stderr = io.StringIO()
        with mock.patch.object(template_builds, "build_now", side_effect=no_room), contextlib.redirect_stderr(stderr):
            self.assertEqual(template_builds.main(["--if-stale", "default"]), 1)   # none to start from: a first install fails
        self.assertIn("NOT BUILT default", stderr.getvalue())
        with mock.patch.object(templates, "build"), contextlib.redirect_stderr(io.StringIO()):
            template_builds.main(["default"])
        built = template_builds.built(templates.DEFAULT)
        stderr = io.StringIO()
        with mock.patch.object(template_builds.templates, "DEFAULT_STEPS", ("echo changed",)), \
                mock.patch.object(template_builds, "build_now", side_effect=no_room), contextlib.redirect_stderr(stderr):
            self.assertEqual(template_builds.main(["--if-stale", "default"]), 0)
        self.assertIn(f"KEPT {built}: default is stale", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
