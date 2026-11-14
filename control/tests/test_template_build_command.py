"""The host command that builds a template runs end to end."""
import contextlib
import io
import unittest
from unittest import mock

from support import Installation


class BuildCommand(Installation):
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


if __name__ == "__main__":
    unittest.main()
