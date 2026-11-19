"""The GitHub extension's HTTP side (connections/github.py): the App's
webhook, which rebuilds templates and drives PR previews, and the credential
helper sandboxes ask for a repo token. The App is optional; an admin sets it
up from the Extensions page.

PR previews: a PR labelled `preview` gets a sandbox pr-<number> on its
branch, updated on every push and deleted when the label goes or the PR
closes. PRs of repos other than the default get the repo's hash in the
name, as their numbers overlap. A preview is the team's (shared), owned by
PREVIEW_OWNER, and gets the repo's values for previews (audience)."""
import hashlib
import hmac
import json
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from . import addressing, core, github_app, lifecycle, platform, recipes, sandboxes, settings, setup_pr, template_builds

router = APIRouter()
# Who owns PR previews: no person, the GitHub extension.
PREVIEW_OWNER = "github-preview"
PREVIEW = {"connector": "github", "audience": "previews", "shared": "team"}


@router.get("/api/sandbox/git-credential", response_class=PlainTextResponse)
def sandbox_git_credential(request: Request):
    """git's credential helper in a sandbox asks here (github_app.py): a token
    for that sandbox's repo, if it is trusted and the App is installed on it."""
    name, _, credential = request.headers.get("authorization", "").removeprefix("Bearer ").partition(":")
    if not name or not hmac.compare_digest(credential, github_app.sandbox_credential(name, platform.session_key())):
        raise HTTPException(401)
    info = core.find(name)
    if not info:
        raise HTTPException(404)
    repo = core.app_of(info).repo
    found = github_app.token(repo, write=True) if settings.trust(repo) == "trusted" else None
    if not found:
        raise HTTPException(404, f"the GitHub App can't reach {repo}")
    return f"username=x-access-token\npassword={found}\n"


@router.post("/github/webhook")
async def github_webhook(request: Request):
    """Pushes to a trusted repo's default branch rebuild its template: at once
    when they change .sbx/, otherwise at night when the repo has a .sbx/;
    a PR labelled `preview` gets a preview (on_pull_request)."""
    body = await request.body()
    if not github_app.verify(body, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(401)
    event, payload = request.headers.get("x-github-event", ""), json.loads(body)
    repository = payload.get("repository") or {}
    repo = (repository.get("full_name") or "").lower()
    if not repo or settings.trust(repo) != "trusted":
        return {"ignored": "not a trusted repo"}
    if event == "push":
        default = f"refs/heads/{recipes.default_branch(repo, repository.get('default_branch'))}"
        touched = {path for c in payload.get("commits") or [] for key in ("added", "modified", "removed")
                   for path in c.get(key) or []}
        if payload.get("ref") != default:
            return {"ignored": "not the default branch"}
        if any(p.startswith(".sbx/") for p in touched):
            try:
                from . import runtimes
                if template_builds.start(repo, runtimes.of_repo(repo)):   # the other runtimes' on the next round
                    return {"template": "building"}
            except template_builds.NoRoom as error:  # the scheduler builds it once there is room
                return {"template": f"not now: {error}"}
            template_builds.mark_moved(repo)  # the running build may predate this push
            return {"template": f"already building; again at {template_builds.NIGHTLY_REBUILD_HOUR:02}:00"}
        if recipes.load(repo).prebuilt:
            template_builds.mark_moved(repo)
            return {"template": f"rebuilt at {template_builds.NIGHTLY_REBUILD_HOUR:02}:00"}
        return {"ignored": "no .sbx/"}
    if event == "pull_request":
        return await on_pull_request(repo, payload)
    return {"ignored": event}


def preview_name(pr: int, repo: str | None) -> str:
    repo = (repo or settings.current().default_repo).lower()
    if not settings.current().serves(repo):
        raise HTTPException(400, f"this installation serves {', '.join(settings.current().repos)} only")
    return f"pr-{pr}" if repo == settings.current().default_repo else f"pr-{pr}-{hashlib.sha256(repo.encode()).hexdigest()[:6]}"


def preview_slug(name: str, branch: str) -> str:
    """The branch name as host name, unless it can't be one
    (sandboxes.check_slug); then with the sandbox's name."""
    try:
        return sandboxes.check_slug(core.slugify(branch), name)
    except core.SandboxError:
        return f"{core.slugify(branch, 34)}-{name}"


def upsert(pr: int, branch: str, repo: str | None) -> dict:
    """Create the preview, or move an existing one to the branch's new head,
    discarding what its setup changed in the checkout (nobody edits a
    preview). A push while the preview is busy (still being made or
    prepared) is queued: the keeper's next round after it prepares the
    branch's head then."""
    name = preview_name(pr, repo)
    current = lifecycle.get(name)
    try:
        if current and current["active"]:
            raise lifecycle.Conflict(f"{name} is busy")
        info = core.find(name)
        if not info:
            return sandboxes.create(sandboxes.CreateSandbox(name=name, branch=branch, public_keys="",
                                                            slug=preview_slug(name, branch), repo=repo), PREVIEW_OWNER,
                                    metadata=PREVIEW)
        sandboxes.set_branch(name, branch)
        lifecycle.in_background(name, "prepare",
                                lambda operation: core.prepare_and_start(name, branch, checkout=True,
                                                                         operation=operation, force=True))
        return {"name": name, "status": "starting", "urls": addressing.urls(core.slug_of(info), core.app_of(info))}
    except lifecycle.Conflict:
        if sandboxes.registered_owner(name) != PREVIEW_OWNER:
            raise
        sandboxes.want_prepare(name, branch, force=True)
        return {"name": name, "status": "queued"}


def has_preview(pr: int, repo: str | None) -> bool:
    """Whether the PR has a preview to delete: its VM, or what a create that
    failed before its VM existed left registered."""
    name = preview_name(pr, repo)
    return bool(core.find(name)) or sandboxes.registered_owner(name) == PREVIEW_OWNER


def delete(pr: int, repo: str | None) -> dict:
    """Also clears what a failed create may have left: provisioned services,
    row. A preview busy with an operation (still being made or prepared) is
    deleted by the keeper's next round after it (sandboxes.want_delete), as
    GitHub says only once that a PR closed: {"deleting": name} then."""
    name = preview_name(pr, repo)
    for last in (False, True):
        try:
            sandboxes.delete(name)
            break
        except lifecycle.Conflict:
            if sandboxes.registered_owner(name) != PREVIEW_OWNER:
                raise
            if name in lifecycle.busy():
                sandboxes.want_delete(name)
                return {"deleting": name}
            if last:   # not busy, so waiting would not help: refused for another reason
                raise
    return {"deleted": name}


def comment_when_up(repo: str, pr: int):
    name = preview_name(pr, repo)
    for _ in range(120):  # 20 minutes
        time.sleep(10)
        current = lifecycle.get(name)
        if lifecycle.status(current) in ("up", "failed"):
            break
    if not current:
        return
    if lifecycle.status(current) == "failed":
        body = f"{github_app.MARKER}\n### Preview failed\n\n```\n{(current['error'] or '')[-1500:]}\n```"
    else:
        info = core.find(name)
        if not info or lifecycle.status(current) != "up":
            return
        links = "".join(f"| {label} | {url} |\n" for label, url in addressing.urls(core.slug_of(info), core.app_of(info)).items())
        body = f"{github_app.MARKER}\n### Preview\n\n| | |\n|---|---|\n{links}\nUpdates on every push. Remove the `preview` label to delete it."
    try:
        github_app.comment_preview(repo, pr, body)
    except Exception as error:
        print(f"preview comment {repo}#{pr}: {error!r}", flush=True)


async def on_pull_request(repo: str, payload: dict) -> dict:
    """The App's pull_request event; it also keeps the recipe PR's state
    (merged, closed) for readiness."""
    pull, number = payload["pull_request"], payload["number"]
    await run_in_threadpool(setup_pr.note, repo, pull)
    if (pull["head"]["repo"] or {}).get("full_name", "").lower() != repo:
        return {"ignored": "a fork"}
    wanted = pull["state"] == "open" and "preview" in {label["name"] for label in pull.get("labels") or []}
    if wanted and payload["action"] in ("labeled", "synchronize", "reopened", "opened"):
        try:
            result = await run_in_threadpool(upsert, number, pull["head"]["ref"], repo)
        except Exception:
            try:
                await run_in_threadpool(github_app.comment_preview, repo, number,
                    f"{github_app.MARKER}\n### Preview failed\n\n"
                    "The sandbox could not be created. Check the sbx control plane for the failure details, "
                    "then retry by removing and adding the `preview` label.")
            except Exception as error:
                print(f"preview failure comment {repo}#{number}: {type(error).__name__}", flush=True)
            raise
        if result.get("status") == "queued":  # the running operation's comment covers it
            return {"preview": "queued"}
        threading.Thread(target=comment_when_up, args=(repo, number), daemon=True).start()
        return {"preview": "starting"}
    if not wanted and payload["action"] in ("unlabeled", "closed") and await run_in_threadpool(has_preview, number, repo):
        result = await run_in_threadpool(delete, number, repo)
        return {"preview": "deleting" if "deleting" in result else "deleted"}
    return {"ignored": "pull_request"}
