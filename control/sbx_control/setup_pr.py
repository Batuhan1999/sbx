"""The recipe's pull request: .sbx/ committed to the repository through the
installation's GitHub App, as a draft PR on one branch (BRANCH).

An agent opens it with `sbx recipe pr`. A recipe the server holds as the
operator's copy (`sbx recipe apply`) is published on its own once a sandbox
came up healthy from it (after_launch), and the same PR follows later
changes, so a recipe that works ends up in the repository. What happened
last, the PR or why it could not be opened, is kept per repo (record) and
shown by readiness, repos.info and GET /api/repos/{repo}/setup-pr."""
import dataclasses
import threading
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import github_app, recipes, repo_path, state
from .auth import admin_user

router = APIRouter()

BRANCH = "sbx/setup"
TITLE = "Set up dev sandboxes"
BODY = ("Prepare this repository for sbx.\n\nReview the commands, external service mappings, and health checks "
        "in `.sbx/`. Credentials remain on the sandbox server. Verify the app from a clean sandbox before merging. "
        "A prebuilt template is optional.\n\nFiles:\n")

# Why the App can't publish, as the owner reads it.
NO_APP = ("No GitHub App is connected to this server, so sbx can't open the recipe's pull request: "
          "an admin connects one (Extensions, GitHub) and installs it on {repo}.")
NOT_INSTALLED = ("The server's GitHub App is not installed on {repo}, so sbx can't open the recipe's pull request: "
                 "install it on the repository with Contents and Pull requests write access.")
NO_PUSH = ("The server's GitHub App may not push to {repo} or open pull requests there: give it Contents and "
           "Pull requests write access, and accept the new permissions on its installation.")


class Unpublished(Exception):
    """GitHub access is missing; the message says what to change."""


def validate_files(files: dict[str, str], executable=()) -> list:
    if '.sbx/sbx.yaml' not in files:
        raise HTTPException(400, 'Include .sbx/sbx.yaml in the setup proposal.')
    executable = set(executable)
    try:
        entries = recipes.checked([dataclasses.replace(e, executable=f".sbx/{e.path}" in executable)
                                   for e in recipes.from_proposal(files)])
    except recipes.RecipeError as error:
        raise HTTPException(400, str(error)) from None
    for entry in entries:
        if any(p.startswith('.env') or p.endswith(('.pem', '.key')) for p in entry.path.split('/')):
            raise HTTPException(400, 'Keep credentials on the server; do not include environment or key files.')
    return entries


def _summary(pr: dict) -> dict:
    return {"number": pr["number"], "url": pr["html_url"], "state": "merged" if pr.get("merged_at") else pr["state"],
            "draft": bool(pr.get("draft")), "branch": pr["head"]["ref"]}


def _client(repo: str):
    if not github_app.settings():
        raise Unpublished(NO_APP.format(repo=repo))
    try:
        token = github_app.token(repo, write=True, pull_requests=True)
    except httpx.HTTPStatusError as error:
        if error.response.status_code in (403, 422):   # the installation lacks a permission asked for
            raise Unpublished(NO_PUSH.format(repo=repo)) from None
        raise
    if not token:
        raise Unpublished(NOT_INSTALLED.format(repo=repo))
    return httpx.Client(base_url=f"{github_app.API}/repos/{repo}/", timeout=30,
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})


def record(repo: str) -> dict | None:
    """What was published last for the repo: the PR (number, url, state:
    open, draft, merged, closed, or in_repository when the default branch
    already had the recipe), the recipe's fingerprint (recipe_sha256) and,
    when the last try failed, why (problem); None before any."""
    with state.db() as conn:
        row = conn.execute('select number, url, state, draft, recipe_sha256, problem, at from repo_setup_prs '
                           'where repo = ?', (repo,)).fetchone()
    if not row:
        return None
    number, url, word, draft, sha, problem, at = row
    return {"number": number or None, "url": url or None, "state": word or None, "draft": bool(draft),
            "recipe_sha256": sha, "problem": problem or None, "at": at}


def _keep(repo: str, **values):
    """Write the given fields of the repo's row; the others stay (a new row
    has number 0: no PR yet)."""
    row = {"number": 0, **values}
    with state.db() as conn:
        conn.execute(f"insert into repo_setup_prs (repo, {', '.join(row)}, at) "
                     f"values (?, {', '.join('?' * len(row))}, current_timestamp) on conflict(repo) do update set "
                     + "".join(f"{n}=excluded.{n}, " for n in values) + "at=excluded.at",
                     (repo, *row.values()))


def note(repo: str, pr: dict):
    """A pull_request event or a refresh: keep the state of the repo's recipe
    PR when it is that PR."""
    found = record(repo)
    if found and found["number"] == pr["number"]:
        summary = _summary(pr)
        _keep(repo, state=summary["state"], draft=int(summary["draft"]))


def publish(repo: str, entries: list, title: str = TITLE) -> dict:
    """Open the recipe's draft PR, or move the open one to this recipe: one
    commit on BRANCH that makes .sbx/ exactly these entries. Nothing changes
    when the PR (or the default branch) has this recipe already."""
    entries = sorted(entries, key=lambda e: e.path)
    sha = recipes.digest(entries)
    github_app.forget(repo)
    try:
        result = _publish(repo, entries, title)
    except Unpublished as error:
        _keep(repo, recipe_sha256=sha, problem=str(error))
        raise
    except httpx.HTTPError as error:
        _keep(repo, recipe_sha256=sha, problem=f"GitHub could not publish the recipe's pull request ({_why(error)}). "
                                                "Retry with sbx recipe pr.")
        raise
    _keep(repo, number=result["number"] or 0, url=result["url"] or "", state=result["state"],
          draft=int(result["draft"]), recipe_sha256=sha, problem="")
    return {**result, "recipe_sha256": sha}


def _why(error: httpx.HTTPError) -> str:
    return f"HTTP {error.response.status_code}" if isinstance(error, httpx.HTTPStatusError) else type(error).__name__


def _publish(repo: str, entries: list, title: str) -> dict:
    owner = repo.split('/')[0]
    with _client(repo) as client:
        def call(method: str, path: str, **kwargs):
            response = client.request(method, path or f"{github_app.API}/repos/{repo}", **kwargs)
            if method != "GET" and response.status_code in (403, 404):   # read, but not write, access
                raise Unpublished(NO_PUSH.format(repo=repo))
            response.raise_for_status()
            return response.json()

        def ref(branch: str):
            response = client.get('git/ref/heads/' + quote(branch, safe=''))
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()['object']['sha']

        def recipe_paths(tree: str) -> set:
            folder = next((e for e in call('GET', f'git/trees/{tree}')['tree'] if e['path'] == '.sbx'), None)
            if not folder or folder['type'] != 'tree':
                return set()
            found = call('GET', f"git/trees/{folder['sha']}", params={"recursive": "1"})['tree']
            return {'.sbx/' + e['path'] for e in found if e['type'] == 'blob'}

        pr = None
        known = record(repo)
        if known and known["number"]:
            found = client.get(f'pulls/{known["number"]}')
            if found.status_code != 404:   # 404: GitHub no longer has it; look for an open one
                found.raise_for_status()
                pr = found.json() if found.json()['state'] == 'open' else None
        if not pr:
            opened = call('GET', 'pulls', params={"state": "open", "head": f"{owner}:{BRANCH}"})
            pr = opened[0] if opened else None
        base = pr['base']['ref'] if pr else call('GET', '')['default_branch']
        branch = pr['head']['ref'] if pr else BRANCH
        parent = ref(branch) if pr else ref(base)
        parent_tree = call('GET', f'git/commits/{parent}')['tree']['sha']
        wanted = {'.sbx/' + e.path for e in entries}
        tree = call('POST', 'git/trees', json={"base_tree": parent_tree, "tree": [
            *({"path": ".sbx/" + e.path, "mode": "100755" if recipes.executable(e) else "100644", "type": "blob",
               "content": e.read().decode()} for e in entries),
            *({"path": p, "mode": "100644", "type": "blob", "sha": None}
              for p in sorted(recipe_paths(parent_tree) - wanted))]})['sha']
        if tree == parent_tree:   # nothing to change
            return {**_summary(pr), "changed": False} if pr else \
                {"number": None, "url": None, "state": "in_repository", "draft": False, "branch": base, "changed": False}
        commit = call('POST', 'git/commits', json={"message": title, "tree": tree, "parents": [parent]})['sha']
        if pr:
            call('PATCH', 'git/refs/heads/' + quote(branch, safe=''), json={"sha": commit})
            return {**_summary(pr), "changed": True}
        if ref(BRANCH):   # left by a closed PR: sbx's own branch, moved to this commit
            call('PATCH', 'git/refs/heads/' + quote(BRANCH, safe=''), json={"sha": commit, "force": True})
        else:
            call('POST', 'git/refs', json={"ref": f"refs/heads/{BRANCH}", "sha": commit})
        pr = call('POST', 'pulls', json={"title": title, "head": BRANCH, "base": base, "draft": True,
                                         "body": BODY + '\n'.join(f'- `{p}`' for p in sorted(wanted))})
        return {**_summary(pr), "changed": True}


def create_pr(repo: str, files: dict[str, str], title: str, executable=()) -> dict:
    entries = validate_files(files, executable)
    try:
        with _lock(repo):   # not while a launch publishes the same repo
            return publish(repo, entries, title)
    except Unpublished as error:
        raise HTTPException(400, str(error)) from None
    except httpx.HTTPError:
        raise HTTPException(502, 'GitHub could not publish the setup PR. Check App permissions and whether the '
                                 'proposal changes the default branch, then retry.') from None


# One publish of a repo at a time: two sandboxes coming up together publish once, and
# `sbx recipe pr` waits for a launch's publish of the same repo.
_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def _lock(repo: str) -> threading.Lock:
    with _guard:
        return _locks.setdefault(repo, threading.Lock())


def _published(repo: str, sha: str) -> bool:
    found = record(repo)
    return bool(found and found["recipe_sha256"] == sha and not found["problem"])


def after_launch(repo: str, background: bool = True):
    """A sandbox of the repo came up healthy: when it ran the operator's copy
    of the recipe (sbx recipe apply) and that recipe is not published yet,
    publish it (on a thread of its own; GitHub may be slow). Without a
    GitHub App that is recorded at once. Never raises: a launch doesn't
    depend on it."""
    try:
        a = recipes.load(repo)
        if not a.has_recipe or not a.operator or _published(repo, recipes.fingerprint(a)):
            return
        if not github_app.settings():
            _keep(repo, recipe_sha256=recipes.fingerprint(a), problem=NO_APP.format(repo=repo))
            return
        if background:
            threading.Thread(target=publish_server_recipe, args=(repo,), name=f"recipe-pr {repo}", daemon=True).start()
        else:
            publish_server_recipe(repo)
    except Exception as error:
        print(f"recipe pr {repo}: {error!r}", flush=True)


def publish_server_recipe(repo: str) -> dict | None:
    """Publish the operator's copy of the repo's recipe unless it is
    already; what happened is in record(repo)."""
    with _lock(repo):
        a = recipes.load(repo)
        if not a.has_recipe or not a.operator:
            return None
        entries = recipes.checked(recipes.walk(a.dir))
        if _published(repo, recipes.digest(entries)):
            return record(repo)
        try:
            return publish(repo, entries)
        except (Unpublished, httpx.HTTPError) as error:
            print(f"recipe pr {repo}: {error}", flush=True)
            return None


class Proposal(BaseModel):
    files: dict[str, str]
    executable: list[str] = Field(default_factory=list)   # the paths whose mode is executable
    title: str = Field(default=TITLE, min_length=1, max_length=120)


@router.post('/api/repos/{owner}/{name}/setup-pr')
def api_create(body: Proposal, email: str = Depends(admin_user), repo: str = Depends(repo_path.trusted)):
    """Open the recipe's draft PR with these files, or update the open one."""
    return create_pr(repo, body.files, body.title, body.executable)


@router.get('/api/repos/{owner}/{name}/setup-pr')
def api_status(email: str = Depends(admin_user), repo: str = Depends(repo_path.trusted)):
    """The recipe's PR as GitHub has it now (pull_request), and what sbx
    recorded (record: the last try's problem among it)."""
    found = record(repo)
    try:
        available = bool(github_app.settings()) and bool(github_app.installation(repo))
    except httpx.HTTPError:
        available = False
    if not found or not found["number"]:
        return {"available": available, "pull_request": None, "record": found}
    try:
        with _client(repo) as client:
            response = client.get(f'pulls/{found["number"]}')
            response.raise_for_status()
    except (Unpublished, httpx.HTTPError):
        raise HTTPException(502, 'Could not refresh the setup PR from GitHub. Try again.') from None
    note(repo, response.json())
    return {"available": True, "pull_request": _summary(response.json()), "record": record(repo)}


SCHEMA = state.Schema(tables="""
create table if not exists repo_setup_prs (repo text primary key, number integer not null);
""", columns=(("repo_setup_prs", "url", "text not null default ''"),
              ("repo_setup_prs", "state", "text not null default ''"),
              ("repo_setup_prs", "draft", "integer not null default 0"),
              ("repo_setup_prs", "recipe_sha256", "text not null default ''"),
              ("repo_setup_prs", "problem", "text not null default ''"),
              ("repo_setup_prs", "at", "text")))
