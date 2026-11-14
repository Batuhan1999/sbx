"""Publish reviewed .sbx files through the installation's GitHub App."""
import hashlib
import json
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import github_app, recipes, repo_path, state
from .auth import admin_user

router = APIRouter()


def validate_files(files: dict[str, str]):
    if '.sbx/sbx.yaml' not in files:
        raise HTTPException(400, 'Include .sbx/sbx.yaml in the setup proposal.')
    try:
        entries = recipes.checked(recipes.from_proposal(files))
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
    token = github_app.token(repo, write=True, pull_requests=True)
    if not token:
        raise HTTPException(400, 'Install this server’s GitHub App on a repo you can write to, or use your own fork.')
    return httpx.Client(base_url=f"{github_app.API}/repos/{repo}/", timeout=30,
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})


def create_pr(repo: str, files: dict[str, str], title: str) -> dict:
    entries = validate_files(files)
    github_app.forget(repo)
    branch = 'sbx/setup-' + hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:12]
    try:
        with _client(repo) as client:
            def request(method: str, path: str, **kwargs):
                response = client.request(method, path or f"{github_app.API}/repos/{repo}", **kwargs)
                response.raise_for_status()
                return response.json()
            opened = request('GET', 'pulls', params={"state": "open", "head": f"{repo.split('/')[0]}:{branch}"})
            if opened:
                pr = opened[0]
            else:
                base = request('GET', '')['default_branch']
                previous = client.get('git/ref/heads/' + quote(branch, safe=''))
                if previous.status_code == 404:
                    head = request('GET', 'git/ref/heads/' + quote(base, safe=''))['object']['sha']
                    tree = request('GET', f'git/commits/{head}')['tree']['sha']
                    tree = request('POST', 'git/trees', json={"base_tree": tree, "tree": [
                        {"path": ".sbx/" + e.path, "mode": "100755" if recipes.executable(e) else "100644", "type": "blob",
                         "content": e.read().decode()} for e in sorted(entries, key=lambda e: e.path)]})['sha']
                    commit = request('POST', 'git/commits', json={"message": title, "tree": tree, "parents": [head]})['sha']
                    request('POST', 'git/refs', json={"ref": f"refs/heads/{branch}", "sha": commit})
                else:
                    previous.raise_for_status()
                body = "Prepare this repository for sbx.\n\nReview the commands, external service mappings, and health checks in `.sbx/`. Credentials remain on the sandbox server. Verify the app from a clean sandbox before merging. A prebuilt template is optional.\n\nFiles:\n" + '\n'.join(f'- `{p}`' for p in sorted(files))
                pr = request('POST', 'pulls', json={"title": title, "head": branch, "base": base, "body": body, "draft": True})
    except httpx.HTTPError:
        raise HTTPException(502, 'GitHub could not publish the setup PR. Check App permissions and whether the proposal changes the default branch, then retry.') from None
    result = _summary(pr)
    with state.db() as conn:
        conn.execute('insert into repo_setup_prs (repo, number) values (?, ?) on conflict(repo) do update set number=excluded.number', (repo, result['number']))
    return result


class Proposal(BaseModel):
    files: dict[str, str]
    title: str = Field(default='Set up dev sandboxes', min_length=1, max_length=120)


@router.post('/api/repos/{owner}/{name}/setup-pr')
def api_create(body: Proposal, email: str = Depends(admin_user), repo: str = Depends(repo_path.trusted)):
    return create_pr(repo, body.files, body.title)


@router.get('/api/repos/{owner}/{name}/setup-pr')
def api_status(email: str = Depends(admin_user), repo: str = Depends(repo_path.trusted)):
    with state.db() as conn:
        row = conn.execute('select number from repo_setup_prs where repo = ?', (repo,)).fetchone()
    if not row:
        return {"available": bool(github_app.installation(repo)), "pull_request": None}
    try:
        with _client(repo) as client:
            response = client.get(f'pulls/{row[0]}')
            response.raise_for_status()
            return {"available": True, "pull_request": _summary(response.json())}
    except httpx.HTTPError:
        raise HTTPException(502, 'Could not refresh the setup PR from GitHub. Try again.') from None


SCHEMA = state.Schema(tables="""
create table if not exists repo_setup_prs (repo text primary key, number integer not null);
""")
