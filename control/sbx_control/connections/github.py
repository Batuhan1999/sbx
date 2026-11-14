"""GitHub: the installation's App (created through the manifest flow in
connections_api.py, or pasted from an existing App), or a deploy key. As an
extension it puts gh and the repo's git access into sandboxes, and serves
the App's webhook and PR previews (github_api.py)."""
from __future__ import annotations

import re

import httpx

from .. import contributions, extensions, github_app, platform, settings, vault
from ..contributions import Check as Ready, Contribution, File, RuntimeLayer, TemplateLayer
from .base import Check, Field, Refused, Status



FOLDER = contributions.folder("github")
KEY = f"{FOLDER}/key"
CREDENTIAL = f"{FOLDER}/credential"
# gh with the sandbox's short-lived App token (the credential helper's)
# unless the caller gives one.
GH = f"""#!/bin/sh
if [ -z "${{GH_TOKEN:-}}${{GITHUB_TOKEN:-}}" ] && [ -x {CREDENTIAL} ]; then
  GH_TOKEN=$(printf 'protocol=https\\nhost=github.com\\n\\n' | {CREDENTIAL} get | sed -n 's/^password=//p')
  export GH_TOKEN
fi
exec /usr/bin/gh "$@"
"""
TEMPLATE = TemplateLayer(
    apt=("gh",), files={"/usr/local/bin/gh": File(GH, 0o755)},
    user=("mkdir -p ~/.ssh && chmod 700 ~/.ssh && ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts 2>/dev/null "
          f"&& printf 'Host github.com\\n  IdentityFile {KEY}\\n  IdentitiesOnly yes\\n' >> ~/.ssh/config",))


def connected() -> bool:
    return bool(github_app.settings()) or bool(platform.credentials().github_key)


class GitHub(extensions.Connector):
    kind = "github"
    title = "GitHub"
    group = "platform"
    shown = ("slug",)

    def contribution(self, target) -> Contribution | None:
        """Sandboxes reach their repo at recipes.remote (HTTPS): through the
        App's credential helper, or rewritten to SSH with the deploy key."""
        if not connected():
            return None
        runtime = None
        if target.name and target.trusted:
            how = github_app.access(target.repo)
            files, git, env = {}, {}, {}
            if how == "key":
                files["key"] = File(platform.credentials().github_key)
                git[f"url.git@github.com:{target.repo}.git.insteadOf"] = f"https://github.com/{target.repo}.git"
                # For git that ignores the clone's config, such as an agent runner's own fetch.
                env["GIT_SSH_COMMAND"] = f"ssh -i {KEY} -o IdentitiesOnly=yes"
            elif how == "app":
                credential = github_app.sandbox_credential(target.name, platform.session_key())
                files["credential"] = File(github_app.credential_helper(target.name, credential), 0o700)
                git["credential.https://github.com.helper"] = CREDENTIAL
            runtime = RuntimeLayer(env=env, files=files, git_config=git)
        return Contribution("github", template=TEMPLATE, runtime=runtime, ready=Ready("gh --version"),
                            scrub_paths=("$HOME/.config/gh", "$HOME/.git-credentials"))

    def git_access(self, repo: str):
        return github_app.git_access(repo)

    def router(self):
        from .. import github_api
        return github_api.router

    def origin(self, info) -> dict:
        """Its sandboxes are PR previews, named pr-<number>[-<repo hash>]."""
        match = re.match(r"^pr-(\d+)", (info.metadata or {}).get("sbx", ""))
        number, repo = match.group(1) if match else "", (info.metadata or {}).get("repo", "")
        return {"kind": "pr", "label": f"PR #{number}" if number else "PR preview",
                "href": f"https://github.com/{repo}/pull/{number}" if number and repo else ""}

    def status(self) -> Status:
        s = github_app.settings()
        key = vault.facts(vault.GITHUB_DEPLOY_KEY)
        facts: dict = {}
        if s:
            state_, summary = "connected", f"App {s['slug']}"
            facts["slug"], facts["install_url"] = s["slug"], github_app.install_url()
            found = github_app.owners(fetch=False)
            if found is not None:
                summary += f" · installed on {', '.join(found) or 'nobody'}"
        elif key.set:
            state_, summary = "connected", "deploy key over SSH (no App: no PR previews by label, no push rebuilds)"
        else:
            state_, summary = "not_connected", "no App yet: one click creates it on GitHub"
        fields = (Field("owner", "Organization", value="", help="empty: your own account (for creating the App)"),
                  Field("id", "App id", value=s.get("id", "") if s else ""),
                  Field("slug", "App slug", value=s.get("slug", "") if s else ""),
                  Field("pem", "App private key", secret=True, set=bool(s)),
                  Field("webhook_secret", "Webhook secret", secret=True, set=bool(s and s.get("webhook_secret"))),
                  Field("client_id", "OAuth client id", value=s.get("client_id", "") if s else ""),
                  Field("client_secret", "OAuth client secret", secret=True, set=bool(s and s.get("client_secret"))),
                  Field("deploy_key", "Deploy key (SSH)", secret=True, set=key.set))
        return Status(self.kind, self.title, state_, summary, facts, fields, ("connect", "test", "disconnect"))

    def test(self, values: dict | None = None) -> list[Check]:
        s = github_app.settings()
        if not s:
            key = vault.facts(vault.GITHUB_DEPLOY_KEY)
            return [Check("GitHub App", False, "not set up", "create it from Settings"),
                    Check("deploy key", key.set, "set" if key.set else "not set", "")]
        checks = []
        try:
            app = httpx.get(f"{github_app.API}/app", headers=github_app.app_headers(s), timeout=15)
            app.raise_for_status()
            checks.append(Check("App", True, f"{app.json().get('slug')} · id {s['id']}"))
            github_app.forget()
            owners = github_app.owners()
            checks.append(Check("installations", bool(owners), f"installed on {', '.join(owners)}" if owners else "installed nowhere",
                                "Install it on repos: " + github_app.install_url()))
            hook = app.json().get("external_url", "")
            base = settings.current().base_url
            checks.append(Check("URLs", hook.rstrip("/") == base, f"the App's URL is {hook}" if hook else "no URL",
                                f"the installation is at {base}: recreate the App after a domain change"))
        except Exception as error:
            checks.append(Check("App", False, f"GitHub refused the App's key: {error}", "recreate the App"))
        for repo in settings.current().repos:
            if "*" in repo:
                continue
            found = github_app.installation(repo)
            checks.append(Check(f"access to {repo}", bool(found), "installed" if found else "the App is not installed on it",
                                github_app.install_url()))
        return checks

    def connect(self, values: dict, *, by: str) -> Status:
        """Paste an existing App's values, or a deploy key."""
        given = {field: values[field] for field in github_app.FIELDS if values.get(field)}
        if given and not github_app.settings() and not {"id", "pem"} <= given.keys():
            raise Refused("an App needs at least its id and private key")
        before = github_app.settings()
        if given:
            github_app.save(given, by=by)
        if values.get("deploy_key"):
            vault.put(vault.GITHUB_DEPLOY_KEY, values["deploy_key"].strip() + "\n", by=by)
        if given:
            checks = self.test()
            if not checks[0].ok:
                github_app.clear(by=by)
                if before:
                    github_app.save(before, by=by)
                raise Refused("GitHub refused the App", checks)
        return self.status()

    def disconnect(self, *, by: str) -> Status:
        github_app.clear(by=by)
        vault.delete(vault.GITHUB_DEPLOY_KEY, by=by)
        return self.status()
