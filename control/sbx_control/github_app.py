"""GitHub App authentication, repository access, webhooks, and preview comments.

Tokens are scoped to one repository and refreshed before expiry."""
import base64
import calendar
import contextlib
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
import time

import httpx
import jwt

from . import platform, state, vault
from . import settings as sbx_settings

API = "https://api.github.com"
INSTALLATION_SECONDS = 300
_installations: dict = {}  # repo -> (installation id or None, fetched_at)
_owners: dict = {}         # "value" -> (accounts the App is installed on, fetched_at)
_tokens: dict = {}         # (repo, write) -> (token, expires_at)

# The App's values, one sealed JSON object: an installation secret in the
# github namespace (not env variables: nothing in a sandbox reads them),
# read with one lookup on every path that asks whether the App is there.
FIELDS = ("id", "slug", "pem", "webhook_secret", "client_id", "client_secret")
KEY = vault.Key("APP", "github")


def _stored() -> dict:
    found = json.loads(vault.get(KEY) or "{}")
    return {field: str(found.get(field) or "") for field in FIELDS}


def settings() -> dict:
    """The App's id, key, webhook secret, slug and OAuth client, or {}."""
    found = _stored()
    return found if found["id"] and found["pem"] else {}


def save(values: dict, *, by: str):
    """Store the App's values given (FIELDS); the others stay."""
    unknown = set(values) - set(FIELDS)
    if unknown:
        raise KeyError(f"not a GitHub App value: {', '.join(sorted(unknown))}")
    vault.put(KEY, json.dumps({**_stored(), **{f: str(v).strip() for f, v in values.items()}}), by=by)
    forget()


def clear(*, by: str):
    vault.delete(KEY, by=by)
    forget()


def forget(repo: str | None = None):
    """Drop what is cached about where the App is installed (all of it, or
    one repo's), so the next look asks GitHub."""
    if repo is None:
        _installations.clear()
        _owners.clear()
    else:
        _installations.pop(repo, None)


def access(repo: str) -> str:
    """How sandboxes and builds reach the repo: app (the App is installed on
    it; tokens per repo), key (the deploy key over SSH), or anonymous (public
    repos). Only trusted repos get credentials."""
    if sbx_settings.trust(repo) != "trusted":
        return "anonymous"
    try:
        if installation(repo):
            return "app"
    except Exception as error:  # GitHub unreachable: fall back, but say so
        print(f"github app: {repo}: {error!r}", file=sys.stderr)
    return "key" if platform.credentials().github_key else "anonymous"


def _anonymous(repo: str) -> state.Setting:
    return state.Setting(f"github_anonymous:{repo.lower()}", encoded=True)


def reachable_anonymously(repo: str) -> bool:
    """Whether the repo is known to work without credentials: GitHub said it
    is public, or the last anonymous fetch of its mirror succeeded."""
    return bool(_anonymous(repo).get(False))


def note_public(repo: str, public: bool):
    """Record what GitHub or an anonymous fetch said about the repo."""
    _anonymous(repo).set(public)


# What git says when GitHub refuses an anonymous read: a private or missing repo.
REFUSED = re.compile(r"could not read Username|Authentication failed|Repository not found|terminal prompts disabled", re.I)


def check_anonymously(repo: str) -> bool | None:
    """One anonymous `git ls-remote`: True when it reads the repo, False when
    GitHub refuses, None when GitHub did not answer (asked again later)."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false"}
    try:
        result = subprocess.run(["git", "-c", "credential.helper=", "ls-remote", "-q",
                                 f"https://github.com/{repo}.git", "HEAD"],
                                env=env, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return True
    return False if REFUSED.search(result.stderr) else None


def learn_access(repos):
    """For each trusted repo reached anonymously (no App, no deploy key) not
    yet known to work that way: check and keep the answer. The record
    otherwise comes only from a template build's mirror fetch or onboarding,
    so a repo served before either recorded it (or before this release), or
    whose last fetch failed, would never show as reachable. A repo known to
    work is not asked again. Runs on the keeper's thread."""
    for repo in repos:
        if reachable_anonymously(repo) or access(repo) != "anonymous":
            continue
        found = check_anonymously(repo)
        if found is not None:
            note_public(repo, found)


def git_url(repo: str) -> str:
    """SSH with the deploy key, else HTTPS: with the App's tokens through the
    credential helper, or anonymously."""
    return f"git@github.com:{repo}.git" if access(repo) == "key" else f"https://github.com/{repo}.git"


@contextlib.contextmanager
def git_access(repo: str):
    """(url, env) for git on this host to reach the repo as access() says:
    the App's token in an extra header, the deploy key over SSH, or
    anonymously. The credential stays out of remotes, arguments and git's
    errors; the key's file is gone afterwards."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    how = access(repo)
    app_token = token(repo) if how == "app" else None
    if app_token:
        index = int(env.get("GIT_CONFIG_COUNT", "0"))
        authorization = base64.b64encode(f"x-access-token:{app_token}".encode()).decode()
        env.update({"GIT_CONFIG_COUNT": str(index + 1), f"GIT_CONFIG_KEY_{index}": "http.https://github.com/.extraheader",
                    f"GIT_CONFIG_VALUE_{index}": f"Authorization: Basic {authorization}"})
    if how == "anonymous":
        try:
            yield f"https://github.com/{repo}.git", env
        except BaseException:
            note_public(repo, False)
            raise
        note_public(repo, True)
        return
    if how != "key":
        yield f"https://github.com/{repo}.git", env
        return
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write(platform.credentials().github_key)
    try:
        os.chmod(f.name, 0o600)
        env["GIT_SSH_COMMAND"] = f"ssh -i {f.name} -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        yield f"git@github.com:{repo}.git", env
    finally:
        os.unlink(f.name)


def manifest(owner: str, return_to: str) -> dict:
    """What the App asks for: contents (clone and push), pull requests (open
    the .sbx/ PR, previews), metadata; push and pull request events. GitHub
    sends the browser to return_to (a web app page) once it is installed."""
    base = sbx_settings.current().base_url
    return {
        "name": f"sbx {sbx_settings.current().domain.name}"[:34],
        "url": base,
        "hook_attributes": {"url": f"{base}/github/webhook"},
        "redirect_url": f"{base}/connections/github/callback",
        "setup_url": base + return_to,
        "public": False,
        "default_permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"},
        "default_events": ["push", "pull_request"],
        "description": f"Dev sandboxes at {base} for {owner}",
    }


def convert(code: str) -> dict:
    """Finish the manifest flow: GitHub returns the new App once, by code."""
    response = httpx.post(f"{API}/app-manifests/{code}/conversions", headers={"Accept": "application/vnd.github+json"},
                          timeout=30)
    response.raise_for_status()
    app = response.json()
    save({field: app[field] for field in FIELDS}, by="github app manifest")
    return app


def app_headers(s: dict) -> dict:
    """Headers that authenticate as the App itself (a JWT signed with its key)."""
    now = int(time.time())
    token = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(s["id"])}, s["pem"], algorithm="RS256")
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}


def owners(*, fetch: bool = True) -> list | None:
    """The accounts the App is installed on, cached for a while; with
    fetch=False only what is cached (None: not known yet)."""
    cached = _owners.get("value")
    if cached and (not fetch or time.time() - cached[1] < INSTALLATION_SECONDS):
        return cached[0]
    s = settings()
    if not fetch or not s:
        return None
    response = httpx.get(f"{API}/app/installations", headers=app_headers(s), timeout=15)
    response.raise_for_status()
    found = [i["account"]["login"] for i in response.json()]
    _owners["value"] = (found, time.time())
    return found


def repositories() -> list[dict]:
    """Repositories accessible to this App, using short-lived read-only tokens."""
    configured = settings()
    if not configured:
        return []
    headers_app = app_headers(configured)
    found = {}
    with httpx.Client(timeout=20) as client:
        page = 1
        while True:
            response = client.get(f"{API}/app/installations", headers=headers_app, params={"per_page": 100, "page": page})
            response.raise_for_status()
            installations = response.json()
            for install in installations:
                if install.get("suspended_at"):
                    continue
                response = client.post(f"{API}/app/installations/{install['id']}/access_tokens", headers=headers_app,
                                       json={"permissions": {"metadata": "read"}})
                response.raise_for_status()
                headers = {"Authorization": f"Bearer {response.json()['token']}", "Accept": "application/vnd.github+json"}
                repo_page = 1
                while True:
                    response = client.get(f"{API}/installation/repositories", headers=headers,
                                          params={"per_page": 100, "page": repo_page})
                    response.raise_for_status()
                    repos = response.json().get("repositories", [])
                    for repo in repos:
                        if not repo.get("disabled"):
                            name = repo["full_name"].lower()
                            found[name] = {"repo": name, "description": repo.get("description") or "",
                                           "private": bool(repo.get("private")), "branch": repo.get("default_branch") or "main"}
                    if len(repos) < 100:
                        break
                    repo_page += 1
            if len(installations) < 100:
                break
            page += 1
    return sorted(found.values(), key=lambda repo: repo["repo"])


def installation(repo: str):
    """The App's installation id for the repo, or None: not installed there.
    Cached for five minutes, so a fresh install shows up soon."""
    cached = _installations.get(repo)
    if cached and time.time() - cached[1] < INSTALLATION_SECONDS:
        return cached[0]
    s = settings()
    if not s:
        return None
    response = httpx.get(f"{API}/repos/{repo}/installation", timeout=15,
                         headers=app_headers(s))
    found = response.json()["id"] if response.status_code == 200 else None
    _installations[repo] = (found, time.time())
    return found


def token(repo: str, write: bool = False, pull_requests: bool = False):
    """A token for this one repo (contents: read, or write; pull requests
    too for comments), or None when the App isn't set up or not installed on
    it. Reused until ten minutes before it expires."""
    key = (repo, write, pull_requests)
    cached = _tokens.get(key)
    if cached and cached[1] - time.time() > 600:
        return cached[0]
    install = installation(repo)
    if not install:
        return None
    s = settings()
    response = httpx.post(f"{API}/app/installations/{install}/access_tokens", timeout=15,
                          headers=app_headers(s), json={"repositories": [repo.split("/", 1)[1]],
                                "permissions": {"contents": "write" if write else "read", "metadata": "read",
                                                **({"pull_requests": "write"} if pull_requests else {})}})
    response.raise_for_status()
    body = response.json()
    expires = calendar.timegm(time.strptime(body["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
    _tokens[key] = (body["token"], expires)
    return body["token"]


def verify(body: bytes, signature: str) -> bool:
    """X-Hub-Signature-256 of a webhook delivery."""
    secret = settings().get("webhook_secret", "")
    if not secret or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature[7:])


def install_url() -> str:
    s = settings()
    return f"https://github.com/apps/{s['slug']}/installations/new" if s.get("slug") else ""


MARKER = "<!-- sbx-preview -->"


def comment_preview(repo: str, pr: int, body: str):
    """The PR's preview comment, written once and then edited, the same
    comment (by its marker) the preview workflow writes."""
    tok = token(repo, pull_requests=True)
    if not tok:
        return
    headers = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
    response = httpx.get(f"{API}/repos/{repo}/issues/{pr}/comments?per_page=100", headers=headers, timeout=15)
    response.raise_for_status()
    comments = response.json()
    mine = next((c["id"] for c in comments if c.get("body", "").startswith(MARKER)), None)
    if mine:
        response = httpx.patch(f"{API}/repos/{repo}/issues/comments/{mine}", headers=headers, json={"body": body}, timeout=15)
    else:
        response = httpx.post(f"{API}/repos/{repo}/issues/{pr}/comments", headers=headers, json={"body": body}, timeout=15)
    response.raise_for_status()


# A sandbox gets a credential of its own (sandbox_credential), and its git
# asks sbx-control for a token through a credential helper whenever it talks
# to GitHub: a token is valid an hour, a session runs for many.

def sandbox_credential(name: str, session_key: str) -> str:
    return hmac.new(session_key.encode(), f"sandbox:{name}".encode(), hashlib.sha256).hexdigest()


def credential_helper(name: str, credential: str, pin_file: str) -> str:
    """git's credential helper protocol: answer `get` for github.com. A
    self-signed certificate (--dns none) is pinned by its key, as the
    template builds' downloads pin it (templates.pinned_key): read when it
    runs from pin_file, which the runtime layer carries and sbx writes again
    when the certificate changes (tls_pin.redeliver)."""
    url = f"{sbx_settings.current().base_url}/api/sandbox/git-credential"
    return f"""#!/bin/sh
# git credential helper: a fresh token for this sandbox's repo from sbx-control.
[ "$1" = get ] || exit 0
host=$(sed -n 's/^host=//p')
[ "$host" = github.com ] || exit 0
# curl's options that pin this server's self-signed certificate; empty for a public CA's.
pin=$(cat {pin_file} 2>/dev/null || true)
curl -fsS -m 20 $pin -H "Authorization: Bearer {name}:{credential}" "{url}"
"""
