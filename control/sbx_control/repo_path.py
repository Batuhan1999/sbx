"""The repo a route names (/api/repos/{owner}/{name}/...), checked once:
FastAPI dependencies for a repo name, a served repo, and a trusted one."""
from urllib.parse import urlparse

from fastapi import HTTPException

from . import settings


def normalize(value: str) -> str:
    """owner/name from what a person pastes (owner/name or its github.com
    URL), lowercased; ValueError when it names no GitHub repo."""
    value = value.strip().rstrip("/")
    if value.startswith("https://"):
        url = urlparse(value)
        if url.netloc.lower() != "github.com" or url.query or url.fragment:
            raise ValueError("Use a github.com repo URL or owner/repo.")
        value = url.path.lstrip("/")
    value = value.removesuffix(".git")
    if not settings.REPO_NAME.fullmatch(value) or value.rpartition("/")[2] in (".", ".."):
        raise ValueError("Use a GitHub repo such as owner/repo.")
    return value.lower()


def named(owner: str, name: str) -> str:
    try:
        return normalize(f"{owner}/{name}")
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


def served(owner: str, name: str) -> str:
    """A repo the installation serves, trusted or public."""
    repo = named(owner, name)
    if not settings.trust(repo):
        raise HTTPException(400, f"{repo} is not served here")
    return repo


def trusted(owner: str, name: str) -> str:
    """A repo whose sandboxes get its secrets and services."""
    repo = named(owner, name)
    if settings.trust(repo) != "trusted":
        raise HTTPException(400, f"{repo} is not a trusted repo here")
    return repo
