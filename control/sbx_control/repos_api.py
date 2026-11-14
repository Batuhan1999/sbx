"""/api/repos: what the installation knows about each repo, its setup
checklist (readiness), its recipe files and its template; the brief an
agent gets to set one up."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from . import readiness, recipes, repo_path, repos, template_builds, views
from .auth import admin_user, api_user, is_admin

router = APIRouter()


@router.get("/api/repos")
async def api_repos(email: str = Depends(api_user)):
    """Every repo the installation knows, with what the Repos list shows."""
    return {"repos": await run_in_threadpool(readiness.summaries)}


@router.get("/api/repos/{owner}/{name}")
def api_repo(email: str = Depends(api_user), repo: str = Depends(repo_path.named)):
    """What the installation knows about a repo, for the CLI (repos.info)."""
    return repos.info(repo)


@router.get("/api/repos/{owner}/{name}/readiness")
async def api_readiness(email: str = Depends(api_user), repo: str = Depends(repo_path.named)):
    """The repo page's checklist: each step's status, who acts, what is
    missing, and the stage the repo is at."""
    return await run_in_threadpool(readiness.of, repo, email)


@router.get("/api/repos/{owner}/{name}/setup-brief", response_class=PlainTextResponse)
def api_setup_brief(email: str = Depends(api_user), repo: str = Depends(repo_path.served)):
    return repos.setup_brief(repo, email)


@router.get("/api/repos/{owner}/{name}/files")
def api_repo_files(email: str = Depends(api_user), repo: str = Depends(repo_path.named)):
    a = recipes.load(repo)
    return {"files": [{"path": p, "text": text} for p, text in views.sbx_files(a)], "source": str(a.dir)}


@router.post("/api/repos/{owner}/{name}/template")
def api_template_build(email: str = Depends(admin_user), repo: str = Depends(repo_path.served)):
    """Build the repo's template from its default branch, in the background.
    That is also what makes a committed .sbx/ take effect."""
    if not template_builds.start(repo):
        raise HTTPException(409, f"{repo}'s template is already building")
    return {"repo": repo, "status": "building"}


@router.delete("/api/repos/{owner}/{name}/template")
def api_template_remove(email: str = Depends(admin_user), repo: str = Depends(repo_path.named)):
    """Delete the repo's templates from the runtime (but those sandboxes run
    or are paused from) and its build record, a failed build's among them.
    A repo whose recipe still asks for one builds it again within minutes."""
    try:
        return template_builds.remove(repo)
    except template_builds.Busy as error:
        raise HTTPException(409, str(error)) from None
    except RuntimeError as error:  # the runtime did not list or delete
        raise HTTPException(503, str(error)) from None


@router.get("/api/repos/{owner}/{name}/template")
def api_repo_template(email: str = Depends(api_user), repo: str = Depends(repo_path.named)):
    return {**readiness.template(repo), "log": views.build_log(repo) if is_admin(email) else []}
