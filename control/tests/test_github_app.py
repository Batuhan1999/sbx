"""The GitHub App's values are the installation's own secrets, not env
variables, a sandbox's token may work on pull requests, and every module
that keeps rows is migrated."""
import importlib
import pkgutil

from support import Installation, Server


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
        self.assertIn("not set", listed["OIDC_CLIENT_SECRET"])


class SandboxCredential(Server):
    SETTINGS = {"repos": ["owner/app"]}
    SET_UP = True

    def ask(self, credential: str):
        return self.client.get("/api/sandbox/git-credential", headers={"Authorization": f"Bearer box:{credential}"})

    def test_a_sandboxs_token_may_open_pull_requests(self):
        """gh in a sandbox gets git's token (connections/github.py): without
        pull requests, gh pr list, view and create are refused."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from sbx_control import core, github_app, platform
        with patch.object(core, "find", return_value=SimpleNamespace(metadata={})), \
                patch.object(core, "app_of", return_value=SimpleNamespace(repo="owner/app")), \
                patch.object(github_app, "token", return_value="test-only-token") as token:
            self.assertEqual(self.ask("not-this-sandboxs").status_code, 401)
            response = self.ask(github_app.sandbox_credential("box", platform.session_key()))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.text, "username=x-access-token\npassword=test-only-token\n")
        token.assert_called_once_with("owner/app", write=True, pull_requests=True)


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

    def test_the_repositories_it_can_use_come_last_pushed_first(self):
        from unittest.mock import patch
        import httpx
        from sbx_control import github_app
        github_app.save({"id": "7", "pem": "-----BEGIN KEY-----", "slug": "sbx-test"}, by="test")

        def answer(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/app/installations":
                return httpx.Response(200, json=[{"id": 1}])
            if request.url.path.endswith("/access_tokens"):
                return httpx.Response(201, json={"token": "test-only"})
            return httpx.Response(200, json={"repositories": [
                {"full_name": "Acme/Old", "private": True, "default_branch": "main", "pushed_at": "2025-01-02T00:00:00Z"},
                {"full_name": "acme/new", "description": "fresh", "default_branch": "trunk", "pushed_at": "2026-11-01T00:00:00Z"},
                {"full_name": "acme/never", "pushed_at": None}]})
        real = httpx.Client
        with patch.object(github_app, "app_headers", return_value={}), \
                patch.object(github_app.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(answer), **kw)):
            found = github_app.repositories()
        self.assertEqual([(r["repo"], r["pushed_at"]) for r in found], [
            ("acme/new", "2026-11-01T00:00:00Z"), ("acme/old", "2025-01-02T00:00:00Z"), ("acme/never", "")])
        self.assertEqual((found[0]["branch"], found[0]["description"], found[1]["private"]), ("trunk", "fresh", True))

    def search_answers(self, seen: list):
        import httpx

        def answer(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.path, dict(request.url.params), request.headers.get("authorization", "")))
            if request.url.path == "/app/installations":
                return httpx.Response(200, json=[{"id": 1}])
            if request.url.path.endswith("/access_tokens"):
                return httpx.Response(201, json={"token": "test-only"})
            return httpx.Response(200, json={"items": [
                {"full_name": "Vercel/Next.js", "description": "The React framework", "default_branch": "canary",
                 "pushed_at": "2026-11-19T00:00:00Z"},
                {"full_name": "gone/away", "disabled": True}]})
        return answer

    def test_search_finds_public_repositories_by_name_with_the_apps_token(self):
        from unittest.mock import patch
        import httpx
        from sbx_control import github_app
        github_app.save({"id": "7", "pem": "-----BEGIN KEY-----", "slug": "sbx-test"}, by="test")
        seen: list = []
        real = httpx.Client
        with patch.object(github_app, "app_headers", return_value={}), \
                patch.object(github_app.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(self.search_answers(seen)), **kw)):
            found = github_app.search("  next   js ")
        self.assertEqual(found, [{"repo": "vercel/next.js", "description": "The React framework", "private": False,
                                  "branch": "canary", "pushed_at": "2026-11-19T00:00:00Z"}])
        path, params, auth = seen[-1]
        self.assertEqual((path, params["q"], params["per_page"]), ("/search/repositories", "next js in:name is:public", "10"))
        self.assertEqual(auth, "Bearer test-only")   # 30 searches a minute instead of 10

    def test_search_works_without_an_app(self):
        from unittest.mock import patch
        import httpx
        from sbx_control import github_app
        seen: list = []
        real = httpx.Client
        with patch.object(github_app.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(self.search_answers(seen)), **kw)):
            self.assertEqual([r["repo"] for r in github_app.search("next")], ["vercel/next.js"])
            self.assertEqual(github_app.search("   "), [])
        self.assertEqual([(path, auth) for path, _, auth in seen], [("/search/repositories", "")])
