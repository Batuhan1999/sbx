"""The GitHub App's values are the installation's own secrets, not env
variables, and every module that keeps rows is migrated."""
import importlib
import pkgutil

from support import Installation


class GitHubAppValues(Installation):
    def test_the_apps_values_are_not_installation_env_variables(self):
        from sbx_control import github_app, variables
        github_app.save({"id": "7", "pem": "-----BEGIN KEY-----", "slug": "sbx-test"}, by="test")
        self.assertEqual(github_app.settings()["slug"], "sbx-test")
        self.assertEqual(variables.values(None), {})
        github_app.clear(by="test")
        self.assertEqual(github_app.settings(), {})

    def test_the_app_is_read_with_one_lookup(self):
        from unittest.mock import patch
        from sbx_control import github_app, vault
        github_app.save({"id": "7", "pem": "-----BEGIN KEY-----"}, by="test")
        github_app.save({"slug": "sbx-test"}, by="test")
        with patch.object(vault, "get", wraps=vault.get) as get:
            self.assertEqual(github_app.settings()["slug"], "sbx-test")
        self.assertEqual(get.call_count, 1)

    def test_operators_see_the_apps_secret_among_the_installations(self):
        import contextlib
        import io
        from sbx_control import admin, github_app
        github_app.save({"id": "7", "pem": "-----BEGIN KEY-----"}, by="test")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(admin.main(["secret", "ls"]), 0)
        listed = {line.split()[0]: line for line in out.getvalue().splitlines()}
        self.assertIn("set", listed["github:APP"])
        self.assertNotIn("BEGIN KEY", out.getvalue())
        self.assertIn("not set", listed["LOGIN_PASSWORD"])



class SchemaOwners(Installation):
    def test_every_module_with_a_schema_is_migrated(self):
        import sbx_control
        from sbx_control import state
        found = set()
        for module in pkgutil.walk_packages(sbx_control.__path__, "sbx_control."):
            if ".template_files" in module.name or module.name.endswith("__main__"):
                continue
            try:
                loaded = importlib.import_module(module.name)
            except ModuleNotFoundError as error:  # a script that runs inside a VM (sbx_guest)
                if error.name.startswith("sbx_control"):
                    raise
                continue
            if isinstance(getattr(loaded, "SCHEMA", None), state.Schema):
                found.add(module.name)
        self.assertEqual(found - set(state.OWNERS), set())
