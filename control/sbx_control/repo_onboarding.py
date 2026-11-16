"""First-repo setup: check GitHub access, add a repo, read its recipe,
apply a recipe from a checkout, and scaffold a first recipe from what a
checkout looks like.

None of it runs repository code or provisions a service.
"""
import base64
import dataclasses
import json
import re
from pathlib import PurePosixPath

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import github_app, paths, recipes, repo_path, settings, template_builds
from .auth import admin_user, api_user

router = APIRouter()


def inspect_repo(repo: str) -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    try:
        token = github_app.token(repo) if github_app.settings() else None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = httpx.get(f"{github_app.API}/repos/{repo}", headers=headers, timeout=20)
        if response.status_code == 404:
            raise HTTPException(400, "GitHub could not find this repo. Check the URL, or install this server’s GitHub App on the private repo.")
        if response.status_code in (403, 429):
            raise HTTPException(400, "GitHub refused the request or its rate limit was reached. Connect GitHub, or try again later.")
        response.raise_for_status()
        data = response.json()
        github_app.note_public(repo, not data.get("private"))
    except httpx.HTTPError:
        raise HTTPException(502, "Could not reach GitHub. Try again.") from None
    return {"repo": repo, "description": data.get("description") or "",
            "branch": recipes.default_branch(repo, data.get("default_branch")),
            "private": bool(data.get("private")), "github_app": bool(token),
            "registered": settings.current().serves(repo), "url": f"https://github.com/{repo}"}


def recipe_from_github(repo: str) -> list:
    """The default branch's .sbx/ through GitHub's API, with the App's token
    when it is installed on the repo (no checkout, no code run)."""
    headers = {"Accept": "application/vnd.github+json"}
    token = github_app.token(repo) if github_app.settings() else None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = httpx.get(f"{github_app.API}/repos/{repo}/git/trees/HEAD", headers=headers, timeout=20)
    if response.status_code == 404:
        return []
    response.raise_for_status()
    folder = next((e for e in response.json()["tree"] if e["path"] == ".sbx"), None)
    if folder is None:
        return []
    if folder["type"] != "tree":
        raise recipes.RecipeError(".sbx must be a directory")
    tree = httpx.get(f"{github_app.API}/repos/{repo}/git/trees/{folder['sha']}",
                     params={"recursive": "1"}, headers=headers, timeout=20)
    tree.raise_for_status()
    data = tree.json()
    if data.get("truncated"):
        raise recipes.RecipeError(recipes.TOO_BIG)

    def blob(sha, size):
        def read():
            got = httpx.get(f"{github_app.API}/repos/{repo}/git/blobs/{sha}", headers=headers, timeout=20)
            got.raise_for_status()
            content = base64.b64decode(got.json()["content"].replace("\n", ""), validate=True)
            if len(content) != size:
                raise recipes.RecipeError("GitHub returned a recipe file of another size than it listed")
            return content
        return read
    return [recipes.Entry(e["path"], e.get("size", 0), blob(e["sha"], e.get("size", 0)), e["mode"] == "100755",
                  e["type"] == "blob" and e["mode"] in ("100644", "100755"))
            for e in data["tree"] if e["type"] != "tree"]


class RepoIn(BaseModel):
    repo: str


def _name(body: RepoIn) -> str:
    try:
        return repo_path.normalize(body.repo)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@router.get("/api/github/repositories")
def api_github_repositories(email: str = Depends(admin_user)):
    try:
        return {"repositories": github_app.repositories()}
    except httpx.HTTPStatusError as error:
        if error.response.status_code in (401, 403):
            raise HTTPException(502, "GitHub refused access. Check the App credentials and installation permissions in Extensions.") from None
        raise HTTPException(502, "GitHub could not list repositories. Try again.") from None
    except httpx.HTTPError:
        raise HTTPException(502, "Could not reach GitHub. Try again.") from None


@router.post("/api/repo-onboarding/inspect")
def api_inspect(body: RepoIn, email: str = Depends(admin_user)):
    return inspect_repo(_name(body))


@router.post("/api/repo-onboarding/connect")
def api_connect(body: RepoIn, email: str = Depends(admin_user)):
    repo = _name(body)
    result = inspect_repo(repo)
    settings.add_repo(repo, by=email)
    return {**result, "registered": True}


def read_recipe(repo: str) -> dict:
    """Import the default branch's .sbx/ unless the installation has a recipe for the repo."""
    if recipes.load(repo).has_recipe:
        return {"configured": True, "source": "installation"}
    try:
        configured = recipes.import_from(repo, recipe_from_github(repo))
    except httpx.HTTPError:
        raise HTTPException(502, "Could not read the repo. Check its GitHub access, then try again.") from None
    except (yaml.YAMLError, ValueError, TypeError, KeyError) as error:
        raise HTTPException(400, f"Check .sbx/: {error}") from None
    return {"configured": configured, "source": "github"}


@router.post("/api/repos/{owner}/{name}/prepare")
def api_prepare(email: str = Depends(admin_user), repo: str = Depends(repo_path.trusted)):
    return read_recipe(repo)


def apply_recipe(repo: str, source, by: str) -> dict:
    """Serve the repo with this recipe (a folder, or recipes.Entry), the
    installation's copy, which wins over the branches' .sbx/."""
    count = recipes.install(repo, source, paths.apps())
    settings.add_repo(repo, by=by)
    return {"repo": repo, "configured": True, "files": count, "prebuilt": template_builds.own(repo)}


class Recipe(BaseModel):
    files: dict[str, str]                              # .sbx/<path> -> its text
    executable: list[str] = Field(default_factory=list)  # the paths whose mode is executable


@router.put("/api/repos/{owner}/{name}/recipe")
def api_apply_recipe(body: Recipe, email: str = Depends(admin_user), repo: str = Depends(repo_path.named)):
    """Trust the repo and install this recipe for it, without a write to the repo."""
    executable = set(body.executable)
    try:
        entries = [dataclasses.replace(e, executable=f".sbx/{e.path}" in executable)
                   for e in recipes.from_proposal(body.files)]
        return apply_recipe(repo, entries, by=email)
    except ValueError as error:  # recipes.RecipeError among them
        raise HTTPException(400, str(error)) from None


def remove_recipe(repo: str) -> dict:
    """Remove the operator's recipe; refused while sandboxes of the repo exist
    (their services were made from it). A repo left without a template of
    its own has its templates and build record removed too."""
    from . import core, runtime
    using = sorted((i.metadata or {}).get("sbx") or i.sandbox_id
                   for i in core.sandboxes_complete(state=[runtime.RUNNING, runtime.PAUSED])
                   if (i.metadata or {}).get("repo") == repo)
    if using:
        raise HTTPException(409, f"{repo} has sandboxes ({', '.join(using)}); delete them first (sbx down NAME)")
    if not recipes.remove(repo):
        raise HTTPException(404, f"{repo} has no recipe installed with sbx recipe apply")
    out = {"repo": repo, "removed": True, "recipe": "imported" if recipes.load(repo).has_recipe else "none"}
    if not template_builds.own(repo):
        try:
            out["template"] = template_builds.remove(repo)
        except RuntimeError as error:  # building, or the runtime refused: the scheduler prunes it later
            out["template"] = {"error": str(error)}
    return out


@router.delete("/api/repos/{owner}/{name}/recipe")
def api_remove_recipe(email: str = Depends(admin_user), repo: str = Depends(repo_path.named)):
    """Remove the recipe sbx recipe apply installed; the repo stays served."""
    return remove_recipe(repo)


# A first recipe from what a checkout looks like. The client sends its file
# paths; the answer names the files to read (READ), and once it has them,
# the recipe. Best effort: the agent or the developer edits it.

LOCKFILES = (("pnpm-lock.yaml", "pnpm", "pnpm install --frozen-lockfile"),
             ("yarn.lock", "yarn", "yarn install --frozen-lockfile"),
             ("package-lock.json", "npm", "npm ci"),
             ("uv.lock", "uv", "uv sync"), ("poetry.lock", "poetry", "poetry install"),
             ("Gemfile.lock", "bundle", "bundle install"), ("go.sum", "go", "go mod download"))
TOOLS = {"uv": "curl -LsSf https://astral.sh/uv/install.sh | sh", "poetry": "pip install poetry"}
ENV_EXAMPLES = (".env.example", ".env.sample", ".env.template")
VITE_PORT = 5173
# The folder names that make a monorepo's Vite app the main preview, in order.
MAIN_PREVIEW = ("", "web", "app", "frontend", "client")


def _read_wanted(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if "node_modules" in parts or len(parts) > 3:
        return False
    if len(parts) == 1 and parts[0] in ("package.json", "Procfile"):
        return True
    if parts[-1].startswith("vite.config.") and len(parts) in (1, 3):
        return True
    return parts[-1] in ENV_EXAMPLES


def dotenv_names(text: str) -> set:
    names = set()
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            names.add(line.removeprefix("export ").partition("=")[0].strip())
    return names


def detect(listed: set, files: dict) -> dict:
    """The package manager, dev command, ports and the variables a
    .env.example names, from the paths and the files READ asked for."""
    found = {"install": None, "lockfile": None, "pm": None, "dev": None, "port": 3000, "previews": {},
             "secrets": [], "tools": []}
    for lockfile, pm, install in LOCKFILES:
        if lockfile in listed:
            found.update(install=install, lockfile=lockfile, pm=pm)
            break
    pkg = json.loads(files["package.json"]) if "package.json" in files else None
    if pkg:
        scripts = pkg.get("scripts") or {}
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        pm = found["pm"] or "npm"
        script = "dev" if "dev" in scripts else "start" if "start" in scripts else None
        if script:
            # Listen on every address: Embed's forwarder resets loopback-only
            # ports under load. npm needs -- before the script's own flags.
            flag = "--host 0.0.0.0" if "vite" in deps else "-H 0.0.0.0" if "next" in deps else ""
            found["dev"] = f"{pm} run {script}" + (f" {'-- ' if pm == 'npm' else ''}{flag}" if flag else "")
        if "vite" in deps:
            found["port"] = VITE_PORT
        for path in sorted(p for p in files if PurePosixPath(p).name.startswith("vite.config.")):
            match = re.search(r"port:\s*(\d{2,5})", files[path])
            if match:
                folder = PurePosixPath(path).parent
                found["previews"].setdefault("" if folder == PurePosixPath(".") else folder.name, int(match.group(1)))
        if found["previews"]:
            main = next((n for n in MAIN_PREVIEW if n in found["previews"]), next(iter(found["previews"])))
            found["port"] = found["previews"].pop(main)
    elif "manage.py" in listed:
        found.update(dev="python manage.py runserver 0.0.0.0:8000", port=8000)
    elif "Procfile" in files:
        web = [line.partition(":")[2].strip() for line in files["Procfile"].splitlines() if line.startswith("web:")]
        found["dev"] = web[0] if web else None
    if found["pm"] in TOOLS:
        found["tools"].append(TOOLS[found["pm"]])
    names = set()
    for path, text in files.items():
        if PurePosixPath(path).name in ENV_EXAMPLES:
            names |= dotenv_names(text)
    found["secrets"] = sorted(names)
    return found


def scaffold(repo: str, found: dict, prebuild: bool = False) -> dict:
    """.sbx/ for the repo from detect(): path -> text, and which are executable."""
    lines = [f"# How {repo} runs in a dev sandbox. Generated by `sbx repo connect`; edit freely."]
    if prebuild:
        lines.append("template: true       # build a template of the repo (the platform names it)")
    lines += ["previews:            # \"\" is the main link; others become <sandbox>-<name>",
              f"  \"\": {found['port']}"]
    lines += [f"  {name}: {port}" for name, port in found["previews"].items()]
    if found["dev"]:
        lines += ["processes:           # each runs as a service, restarted when it fails",
                  f"  web: {found['dev']}",
                  "health:", f"  - http://localhost:{found['port']}/"]
    else:
        lines += ["# processes:         # nothing found to start; the agent starts the app",
                  "#   web: <your dev command>"]
    if found["secrets"]:
        lines += ["secrets:             # set with: sbx env set NAME (values never go in the repo)"]
        lines += [f"  - {name}" for name in found["secrets"]]
    lines += ["env:", f"  PORT: \"{found['port']}\""]
    out = {".sbx/sbx.yaml": "\n".join(lines) + "\n"}
    executable = []
    if prebuild:
        out[".sbx/Dockerfile"] = "\n".join(["FROM sbx-base", *[f"RUN {tool}" for tool in found["tools"]]]) + "\n"
        if found["install"]:
            out[".sbx/build"] = f"#!/bin/bash\nset -eu\n{found['install']}\n"
            executable.append(".sbx/build")
    setup = [] if prebuild else list(found["tools"])
    if found["install"]:
        if prebuild and found["lockfile"]:
            # Reinstall only when the lockfile changed since the template's commit.
            setup += [f'if [ ! -f ~/.template-commit ] || ! git diff --quiet "$(cat ~/.template-commit)" HEAD -- '
                      f'{found["lockfile"]}; then', f'  {found["install"]}', 'fi']
        else:
            setup.append(found["install"])
    if setup:
        out[".sbx/setup"] = "#!/bin/bash\nset -eu\n" + "\n".join(setup) + "\n"
        executable.append(".sbx/setup")
    return {"files": out, "executable": executable}


class Checkout(BaseModel):
    paths: list[str] = Field(max_length=100_000)     # the checkout's files, relative (git ls-files)
    files: dict[str, str] = Field(default_factory=dict)  # the ones a previous answer's read named
    prebuild: bool = False


@router.post("/api/repos/{owner}/{name}/scaffold")
def api_scaffold(body: Checkout, email: str = Depends(api_user), repo: str = Depends(repo_path.named)):
    """{read: [paths]} until the client sent those files, then {files,
    executable, found}: a first .sbx/ for the checkout. Writes nothing here."""
    wanted = sorted(p for p in body.paths if _read_wanted(p))
    missing = [p for p in wanted if p not in body.files]
    if missing:
        return {"read": missing}
    try:
        found = detect(set(body.paths), {p: body.files[p] for p in wanted})
    except ValueError as error:  # package.json is not JSON
        raise HTTPException(400, f"could not read the checkout: {error}") from None
    return {**scaffold(repo, found, body.prebuild), "found": {k: found[k] for k in ("dev", "port", "secrets")}}
