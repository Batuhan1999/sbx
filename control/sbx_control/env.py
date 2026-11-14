"""What a repo's sandboxes get as env, where each value comes from, and
changing it: an admin the repo's values, anyone their own. A change reaches
the sandboxes it concerns through sandboxes.deliver_env.

Values go in and never come out: the API lists names only."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import auth, catalog, core, recipes, repo_path, sandboxes, secret_store, settings, variables, views
from .catalog import attachments as attached

router = APIRouter()


class SecretValue(BaseModel):
    value: str


class EnvValue(BaseModel):
    value: str | None = None  # None keeps the value, to change only the settings
    secret: bool | None = None  # None preserves the setting; new values are secret.
    audience: str = "all"
    agent: bool = True
    mine: bool = False
    restart: bool = True


class EnvImport(BaseModel):
    text: str
    audience: str = "all"
    agent: bool = True
    mine: bool = False
    restart: bool = True


def secret_refs(value) -> set:
    return {m.group(4) for m in core.ENV_REF.finditer(str(value)) if m.group(1) == "secret"}


def missing(repo: str, person: str | None = None) -> list:
    """The values the repo's recipe asks for that its sandboxes would not get,
    by the rules a sandbox's env is made with (core.app_env, core.create_services):
    a name in sbx.yaml's `secrets:` is there when the repo has a value of that
    name, person has one of their own there (in their sandboxes), or an
    installation variable whose pattern matches the repo gives it; a
    ${secret:NAME} in sbx.yaml's env or a service's with: resolves from the
    repo's values only. An untrusted repo's sandboxes get none of these."""
    a = recipes.load(repo)
    declared = a.spec.get("secrets") or []
    trusted = settings.trust(repo) == "trusted"
    try:
        have = set(variables.values(repo)) if trusted else set()
        own = {e["name"] for e in variables.entries(repo, person) if e["mine"]} if trusted and person else set()
    except secret_store.StoreError:
        have, own = set(), set()
    installation = {name for values in variables.installation_layers(repo) for name in values} if trusted else set()
    referenced = set()
    for value in (a.spec.get("env") or {}).values():
        referenced |= secret_refs(value)
    for use in (a.spec.get("services") or {}).values():
        for value in ((use or {}).get("with") or {}).values() if isinstance(use, dict) else ():
            referenced |= secret_refs(value)
    return sorted((set(declared) - have - own - installation) | (referenced - have))


def repo_env(repo: str, email: str) -> dict:
    """Every variable the repo's sandboxes get, lowest first: its services'
    defaults, sbx.yaml's, the installation's variables, the repo's values,
    the caller's own. The last of a name wins; the ones under it say what overrides them."""
    a = recipes.load(repo)
    admin = auth.is_admin(email)
    rows = []
    for key, use in (a.spec.get("services") or {}).items():
        try:
            package = catalog.resolve((use or {}).get("uses", ""), repo)
        except catalog.PackageError:
            continue
        rows += [{"name": var, "source": f"service {key}", "kind": "service", "template": str(value),
                  "note": f"each sandbox its own ({use.get('uses')})"} for var, value in package.env.items()]
    rows += [{"name": var, "source": "sbx.yaml", "kind": "sbx.yaml", "template": str(value)}
             for var, value in (a.spec.get("env") or {}).items()]
    for values in variables.installation_layers(repo):
        rows += [{"name": var, "source": "server", "kind": "installation",
                  **({"template": str(value)} if admin else {})} for var, value in values.items()]
    store_error = ""
    try:
        entries = variables.entries(repo, email)
    except secret_store.StoreError as error:
        entries, store_error = [], str(error)
    rows += [{**entry, "source": "your own" if entry["mine"] else "this repo",
              "kind": "mine" if entry["mine"] else "repo"} for entry in entries if not entry["mine"]]
    rows += [{**entry, "source": "your own", "kind": "mine"} for entry in entries if entry["mine"]]
    owned = attached.owned_names(repo)
    attachments = attached.effective(repo)
    for row in rows:
        own = owned.get(row["name"])
        if own:
            row["owned_by"] = own["key"]
            row["attachment"] = own["title"]
    # Resolved sandbox state takes precedence over shared development defaults.
    generated = {}
    for row in rows:
        if row['kind'] in ('service', 'sbx.yaml', 'installation'):
            if core.sandbox_env_reference(row.get('template'), generated):
                generated[row['name']] = row
            else:
                generated.pop(row['name'], None)
    protected = {id(row) for row in generated.values()}
    rows = [row for row in rows if id(row) not in protected] + list(generated.values())
    for i, row in enumerate(rows):
        later = [r for r in rows[i + 1:] if r["name"] == row["name"]]
        if later:
            row["overridden_by"] = later[-1]["source"]
    return {"repo": repo, "admin": admin, "variables": rows, "missing": missing(repo, email),
            "attachments": attachments, "store": secret_store.store().name, "store_error": store_error}


def parse_dotenv(text: str) -> dict:
    """KEY=VALUE lines, as .env files have them: export, quotes and comments
    allowed; values with spaces or # quoted."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if value[:1] in ("'", '"') and value.endswith(value[0]) and len(value) > 1:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[key] = value
    return out


# The installation's values (sbx env ... --installation); a repo's go through /api/repos/{repo}/env,
# which checks who gets them.
@router.get("/api/secrets")
def api_secrets(email: str = Depends(auth.admin_user)):
    return variables.names(None)


def apply_everywhere(names) -> dict:
    """deliver_env for every trusted repo: an installation value reaches them
    through the installation's variables (settings variables.by_pattern)."""
    total = {"running": 0, "paused": 0}
    for repo in views.known_repos():
        if settings.trust(repo) != "trusted":
            continue
        done = sandboxes.deliver_env(repo, names)
        total = {**done, "running": total["running"] + done["running"], "paused": total["paused"] + done["paused"]}
    return total


@router.put("/api/secrets/{name}")
def api_secret_set(name: str, body: SecretValue, email: str = Depends(auth.admin_user)):
    try:
        variables.put(None, name, body.value, email)
    except ValueError as error:
        raise HTTPException(400, str(error))
    except secret_store.StoreError as error:
        raise HTTPException(502, f"not saved: {error}")
    return {"name": name, "set": True, **apply_everywhere([name])}


@router.delete("/api/secrets/{name}")
def api_secret_delete(name: str, email: str = Depends(auth.admin_user)):
    if not variables.delete(None, name):
        raise HTTPException(404, f"no secret {name}")
    return {"name": name, "deleted": True, **apply_everywhere([name])}


@router.get("/api/repos/{owner}/{name}/env")
def api_repo_env(email: str = Depends(auth.api_user), repo: str = Depends(repo_path.trusted)):
    return repo_env(repo, email)


@router.put("/api/repos/{owner}/{name}/env/{var}")
def api_repo_env_set(var: str, body: EnvValue, email: str = Depends(auth.api_user), repo: str = Depends(repo_path.trusted)):
    auth.require_may_set(email, body.mine)
    person = email if body.mine else None
    before = next((e for e in variables.entries(repo, email) if e["name"] == var and e["mine"] == body.mine), None)
    secret = body.secret if body.secret is not None else (before["secret"] if before else True)
    try:
        variables.put(repo, var, body.value, email, secret=secret, audience=body.audience, agent=body.agent,
                         person=person)
    except ValueError as error:
        raise HTTPException(400, str(error))
    except secret_store.StoreError as error:
        raise HTTPException(502, f"not saved: {error}")
    audiences = {body.audience, before["audience"]} if before else {body.audience}
    return {"name": var, "secret": secret, **sandboxes.deliver_env(repo, [var], audiences, person, body.restart)}


@router.delete("/api/repos/{owner}/{name}/env/{var}")
def api_repo_env_delete(var: str, mine: bool = False, restart: bool = True, email: str = Depends(auth.api_user), repo: str = Depends(repo_path.trusted)):
    auth.require_may_set(email, mine)
    person = email if mine else None
    before = next((e for e in variables.entries(repo, email) if e["name"] == var and e["mine"] == mine), None)
    try:
        deleted = variables.delete(repo, var, person)
    except secret_store.StoreError as error:
        raise HTTPException(502, f"not deleted: {error}")
    if not deleted:
        raise HTTPException(404, f"no {var} {'of yours ' if mine else ''}in {repo}")
    return {"name": var, "deleted": True, **sandboxes.deliver_env(repo, [var], {before["audience"]} if before else None, person, restart)}


@router.post("/api/repos/{owner}/{name}/env-import")
def api_repo_env_import(body: EnvImport, email: str = Depends(auth.api_user), repo: str = Depends(repo_path.trusted)):
    """Import nonempty values as secrets; visibility can be changed explicitly."""
    auth.require_may_set(email, body.mine)
    values = parse_dotenv(body.text)
    bad = [k for k in values if not variables.NAME_RE.match(k)]
    if bad:
        raise HTTPException(400, f"not variable names: {', '.join(bad)}")
    person = email if body.mine else None
    done = {}
    for var, value in values.items():
        if value:
            secret = True
            variables.put(repo, var, value, email, secret=secret, audience=body.audience, agent=body.agent,
                             person=person)
            done[var] = secret
    if not done:
        raise HTTPException(400, "no KEY=VALUE lines with a value")
    return {"set": done, **sandboxes.deliver_env(repo, list(done), {body.audience}, person, body.restart)}
