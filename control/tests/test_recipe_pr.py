"""The recipe's pull request (setup_pr): opened through the GitHub App on
request and once a sandbox comes up healthy from an applied recipe, kept to
one PR per repo that follows the recipe, and every reason it can't be opened
shown to the agent and the owner. GitHub is an in-memory fake behind
httpx.MockTransport."""
import hashlib
import json
import shutil
from unittest.mock import patch

import httpx
from support import Server

RECIPE = {".sbx/sbx.yaml": "health: ['http://localhost:3000/']\nprocesses: {web: npm run dev}\n",
          ".sbx/setup": "#!/bin/sh\nnpm ci\n"}


class FakeGitHub:
    """The parts of GitHub's REST API setup_pr uses, for one repo: refs,
    commits, content-addressed trees (path -> (mode, text)) and pull requests."""

    def __init__(self, repo="owner/app", default="develop", files=None):
        self.repo, self.default = repo, default
        self.trees, self.commits, self.pulls, self.writes = {}, {}, [], []
        self.refuse = set()   # (method, path suffix) answered 403
        root = self._tree(dict(files or {"README.md": ("100644", "hello\n")}))
        self.refs = {default: self._commit(root, [])}

    def _tree(self, files: dict) -> str:
        sha = hashlib.sha1(json.dumps(sorted(files.items())).encode()).hexdigest()
        self.trees[sha] = files
        return sha

    def _commit(self, tree: str, parents: list) -> str:
        sha = hashlib.sha1(f"{tree}{parents}{len(self.commits)}".encode()).hexdigest()
        self.commits[sha] = {"tree": tree, "parents": parents}
        return sha

    def files(self, branch: str) -> dict:
        return self.trees[self.commits[self.refs[branch]]["tree"]]

    def merge(self, number: int):
        pr = self.pulls[number - 1]
        self.refs[pr["base"]["ref"]] = self.refs[pr["head"]["ref"]]
        pr.update(state="closed", merged_at="2026-11-18T00:00:00Z")

    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer fixture-token"
        path = request.url.path.removeprefix(f"/repos/{self.repo}").lstrip("/")
        method, body = request.method, json.loads(request.content) if request.content else None
        if method != "GET":
            self.writes.append((method, path, body))
            if any(method == m and path.endswith(p) for m, p in self.refuse):
                return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        if method == "GET" and path == "":
            return httpx.Response(200, json={"default_branch": self.default})
        if path.startswith("git/ref/heads/"):
            branch = path.removeprefix("git/ref/heads/")
            return httpx.Response(200, json={"object": {"sha": self.refs[branch]}}) if branch in self.refs \
                else httpx.Response(404, json={})
        if method == "GET" and path.startswith("git/commits/"):
            return httpx.Response(200, json={"tree": {"sha": self.commits[path.split("/")[-1]]["tree"]}})
        if method == "GET" and path.startswith("git/trees/"):
            sha = path.removeprefix("git/trees/")
            if sha.endswith(":.sbx"):
                files = self.trees[sha.removesuffix(":.sbx")]
                return httpx.Response(200, json={"tree": [{"path": p.removeprefix(".sbx/"), "type": "blob"}
                                                          for p in files if p.startswith(".sbx/")]})
            files = self.trees[sha]
            top = [{"path": p, "type": "blob", "sha": "x"} for p in files if "/" not in p]
            if any(p.startswith(".sbx/") for p in files):
                top.append({"path": ".sbx", "type": "tree", "sha": f"{sha}:.sbx"})
            return httpx.Response(200, json={"tree": top})
        if method == "POST" and path == "git/trees":
            files = dict(self.trees[body["base_tree"]])
            for entry in body["tree"]:
                if entry.get("sha", "") is None:
                    del files[entry["path"]]
                else:
                    files[entry["path"]] = (entry["mode"], entry["content"])
            return httpx.Response(201, json={"sha": self._tree(files)})
        if method == "POST" and path == "git/commits":
            return httpx.Response(201, json={"sha": self._commit(body["tree"], body["parents"])})
        if method == "POST" and path == "git/refs":
            self.refs[body["ref"].removeprefix("refs/heads/")] = body["sha"]
            return httpx.Response(201, json={})
        if method == "PATCH" and path.startswith("git/refs/heads/"):
            branch = path.removeprefix("git/refs/heads/")
            if not body.get("force") and self.commits[body["sha"]]["parents"] != [self.refs[branch]]:
                return httpx.Response(422, json={"message": "Update is not a fast forward"})
            self.refs[branch] = body["sha"]
            return httpx.Response(200, json={})
        if method == "GET" and path == "pulls":
            owner, _, branch = request.url.params["head"].partition(":")
            return httpx.Response(200, json=[p for p in self.pulls if p["state"] == "open" and p["head"]["ref"] == branch])
        if method == "GET" and path.startswith("pulls/"):
            number = int(path.split("/")[1])
            return httpx.Response(200, json=self.pulls[number - 1]) if number <= len(self.pulls) \
                else httpx.Response(404, json={})
        if method == "POST" and path == "pulls":
            number = len(self.pulls) + 1
            pr = {"number": number, "html_url": f"https://github.com/{self.repo}/pull/{number}", "state": "open",
                  "draft": body["draft"], "title": body["title"], "body": body["body"], "merged_at": None,
                  "head": {"ref": body["head"]}, "base": {"ref": body["base"]}}
            self.pulls.append(pr)
            return httpx.Response(201, json=pr)
        raise AssertionError(f"unexpected {method} {path}")


class RecipePrTests(Server):
    def setUp(self):
        super().setUp()
        from sbx_control import paths, setup_pr
        self.setup_pr = setup_pr
        for recipes in (paths.apps(), paths.state_dir() / "apps"):   # the class's installation is shared
            self.addCleanup(shutil.rmtree, recipes, ignore_errors=True)
        self.repo_admin()
        self.github = FakeGitHub()
        client = httpx.Client   # GitHub's API through the fake; anything else (the runtime) as it was

        def github_or_not(**kw):
            if str(kw.get("base_url", "")).startswith(setup_pr.github_app.API):
                kw["transport"] = self.github.transport()
            return client(**kw)
        self.enterContext(patch.object(setup_pr.httpx, "Client", side_effect=github_or_not))
        self.app_settings = self.enterContext(patch.object(setup_pr.github_app, "settings",
                                                           return_value={"id": "1", "pem": "fixture"}))
        self.token = self.enterContext(patch.object(setup_pr.github_app, "token", return_value="fixture-token"))
        self.enterContext(patch.object(setup_pr.github_app, "installation", return_value=7))
        self.enterContext(patch("sbx_control.views.collect", return_value=[]))
        self.enterContext(patch("sbx_control.views.sandbox_counts", return_value={}))

    def apply(self, files=RECIPE):
        response = self.client.put("/api/repos/owner/app/recipe",
                                   json={"files": files, "executable": [".sbx/setup"]})
        self.assertEqual(response.status_code, 200, response.text)

    def launched(self):
        """What core does once a sandbox of the repo came up healthy."""
        self.setup_pr.after_launch("owner/app", background=False)

    def step(self):
        found = self.client.get("/api/repos/owner/app/readiness").json()
        return next(s for s in found["steps"] if s["step"] == "recipe_pr")

    def test_a_healthy_launch_from_an_applied_recipe_opens_one_draft_pr_that_follows_the_recipe(self):
        self.apply()
        self.assertEqual(self.step()["status"], "blocked")   # nothing to publish before a healthy launch
        self.launched()
        self.assertEqual(len(self.github.pulls), 1)
        pr = self.github.pulls[0]
        self.assertEqual((pr["draft"], pr["head"]["ref"], pr["base"]["ref"]), (True, "sbx/setup", "develop"))
        self.assertEqual(self.github.files("sbx/setup"), {
            "README.md": ("100644", "hello\n"),
            ".sbx/sbx.yaml": ("100644", RECIPE[".sbx/sbx.yaml"]), ".sbx/setup": ("100755", RECIPE[".sbx/setup"])})
        step = self.step()
        self.assertEqual((step["status"], step["who"], step["url"]),
                         ("todo", "person", "https://github.com/owner/app/pull/1"))
        info = self.client.get("/api/repos/owner/app").json()
        self.assertEqual((info["recipe_source"], info["recipe_pr"]["url"], info["recipe_pr"]["current"]),
                         ("server", "https://github.com/owner/app/pull/1", True))

        # The same recipe again (a second sandbox, another launch): no write to GitHub.
        writes = len(self.github.writes)
        self.launched()
        self.setup_pr.publish_server_recipe("owner/app")
        self.assertEqual(len(self.github.writes), writes)

        # A changed recipe: the open PR gets one commit; a file the recipe dropped goes.
        changed = {".sbx/sbx.yaml": RECIPE[".sbx/sbx.yaml"] + "env: {NODE_ENV: development}\n"}
        self.apply(changed)
        step = self.step()
        self.assertEqual((step["status"], step["who"]), ("todo", "agent"))
        self.assertIn("changed since PR #1", step["detail"])
        head = self.github.refs["sbx/setup"]
        self.launched()
        self.assertEqual(len(self.github.pulls), 1)
        self.assertEqual(self.github.commits[self.github.refs["sbx/setup"]]["parents"], [head])
        self.assertEqual(set(self.github.files("sbx/setup")), {"README.md", ".sbx/sbx.yaml"})
        self.assertEqual(self.step()["status"], "todo")

        # Merged: GET setup-pr refreshes it, and the step is done.
        self.github.merge(1)
        status = self.client.get("/api/repos/owner/app/setup-pr").json()
        self.assertEqual((status["pull_request"]["state"], status["record"]["state"]), ("merged", "merged"))
        self.assertEqual((self.step()["status"], self.step()["detail"]), ("done", "Merged in PR #1"))
        listed = next(r for r in self.client.get("/api/repos").json()["repos"] if r["repo"] == "owner/app")
        self.assertEqual(listed["recipe_pr"]["url"], "https://github.com/owner/app/pull/1")

    def test_a_merged_pr_is_not_reopened_and_a_later_change_opens_a_new_one(self):
        self.apply()
        self.launched()
        self.github.merge(1)
        self.client.get("/api/repos/owner/app/setup-pr")
        self.launched()
        self.assertEqual(len(self.github.pulls), 1)
        self.apply({".sbx/sbx.yaml": RECIPE[".sbx/sbx.yaml"] + "# tuned\n", ".sbx/setup": RECIPE[".sbx/setup"]})
        self.launched()
        self.assertEqual(len(self.github.pulls), 2)
        self.assertEqual(self.github.pulls[1]["state"], "open")
        self.assertEqual(self.github.files("sbx/setup")[".sbx/sbx.yaml"][1], RECIPE[".sbx/sbx.yaml"] + "# tuned\n")

    def test_a_recipe_the_default_branch_has_already_needs_no_pr(self):
        self.github = FakeGitHub(files={".sbx/sbx.yaml": ("100644", RECIPE[".sbx/sbx.yaml"]),
                                        ".sbx/setup": ("100755", RECIPE[".sbx/setup"])})
        self.apply()
        self.launched()
        self.assertEqual(self.github.pulls, [])
        self.assertEqual(self.step()["status"], "done")
        self.assertEqual(self.step()["detail"], "The default branch has this recipe already")

    def test_an_imported_recipe_is_the_repositorys_and_gets_no_pr(self):
        from sbx_control import recipes, settings
        recipes.import_from("owner/app", recipes.from_proposal(RECIPE))
        settings.add_repo("owner/app", by="test")
        self.launched()
        self.assertEqual(self.github.writes, [])
        self.assertEqual(self.step()["status"], "done")

    def test_missing_github_access_is_reported_not_skipped_and_retried_on_the_next_launch(self):
        from sbx_control import setup_pr
        self.apply()
        cases = (
            ("no app", lambda: self.app_settings.configure_mock(return_value={}), "No GitHub App is connected"),
            ("not installed", lambda: self.token.configure_mock(return_value=None), "is not installed on owner/app"),
            ("no permission", lambda: self.token.configure_mock(side_effect=httpx.HTTPStatusError(
                "422", request=httpx.Request("POST", "https://api.github.com"), response=httpx.Response(422))),
             "may not push to owner/app"),
            ("push refused", lambda: self.github.refuse.add(("POST", "git/trees")), "may not push to owner/app"),
        )
        for label, break_it, said in cases:
            with self.subTest(label):
                break_it()
                self.launched()
                self.assertEqual(self.github.pulls, [])
                step = self.step()
                self.assertEqual((step["status"], step["who"]), ("todo", "admin"))
                self.assertIn(said, step["detail"])
                status = self.client.get("/api/repos/owner/app/setup-pr").json()
                self.assertIsNone(status["pull_request"])
                self.assertIn(said, status["record"]["problem"])
                refused = self.client.post("/api/repos/owner/app/setup-pr", json={"files": RECIPE})
                self.assertEqual(refused.status_code, 400)
                self.assertIn(said, refused.json()["detail"])
                self.app_settings.configure_mock(return_value={"id": "1", "pem": "fixture"})
                self.token.configure_mock(return_value="fixture-token", side_effect=None)
                self.github.refuse.clear()
        self.launched()   # access fixed: the next healthy launch opens it
        self.assertEqual(len(self.github.pulls), 1)
        self.assertIsNone(setup_pr.record("owner/app")["problem"])
        self.assertEqual(self.step()["who"], "person")

    def test_sbx_recipe_pr_opens_the_same_pr_and_refuses_credentials(self):
        from sbx_control import settings
        settings.add_repo("owner/app", by="test")
        files = {".sbx/sbx.yaml": "template: owner-app\n", ".sbx/setup": "#!/bin/sh\necho setup\n"}
        one = self.client.post("/api/repos/owner/app/setup-pr", json={"files": files})
        two = self.client.post("/api/repos/owner/app/setup-pr", json={"files": files})
        self.assertEqual(one.status_code, 200, one.text)
        self.assertEqual((one.json()["changed"], two.json()["changed"]), (True, False))
        self.assertEqual(two.json()["url"], one.json()["url"])
        self.assertEqual(len(self.github.pulls), 1)
        for path in ("../file", ".sbx/../file", ".sbx/.env", ".sbx/private.key"):
            result = self.client.post("/api/repos/owner/app/setup-pr", json={"files": {**files, path: "no"}})
            self.assertEqual(result.status_code, 400)
        self.assertEqual(len(self.github.pulls), 1)

    def test_a_pull_request_event_keeps_the_recipe_prs_state(self):
        self.apply()
        self.launched()
        self.github.merge(1)
        self.setup_pr.note("owner/app", self.github.pulls[0])
        self.assertEqual(self.setup_pr.record("owner/app")["state"], "merged")
        self.setup_pr.note("owner/app", {**self.github.pulls[0], "number": 99, "merged_at": None, "state": "open"})
        self.assertEqual(self.setup_pr.record("owner/app")["state"], "merged")   # another PR: not ours
