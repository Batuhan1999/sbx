"""What a repo still needs before its sandboxes work, and who has to act:
the coding agent (it writes .sbx/ and fixes recipe problems), a person
(secret values), an admin (GitHub access, package values and shared
connections, template builds) or nobody (the platform is working on it).

The repo page's checklist, the repo list's one-line state and the template
badge all read the rules here; the web app only renders them."""
from __future__ import annotations

from . import auth, env, recipes, repos, runtime, runtimes, settings, setup_pr, template_builds, views
from .catalog import attachments

STEPS = ("served", "github", "configured", "services", "env", "template", "sandbox", "recipe_pr")

# A template build's state: (step status, who acts, level, label, detail).
TEMPLATE = {
    "default": ("done", None, "idle", "Platform default", "Platform default image; no build needed"),
    "building": ("working", "platform", "progress", "Building", "Building from the default branch"),
    "failed": ("todo", "agent", "bad", "Build failed", "The last build failed; read its log, fix the recipe, build again"),
    "missing": ("todo", "admin", "warn", "Not built", "Not built yet"),
    "stale": ("todo", "admin", "warn", "Stale", "Stale: the recipe or an extension changed since the last build. "
              "It rebuilds on its own; an admin can rebuild now"),
    "built": ("done", None, "ok", "Built", "Built and current"),
}

# Only admins change a repo's attachments; a package the recipe names wrongly is the agent's to fix.
PROBLEM_ACTOR = {"needs_values": "admin", "needs_connection": "admin", "check_failed": "admin", "error": "agent"}


def template_key(prebuilt: bool, build: dict) -> str:
    """The one rule for a repo's template, from the latest build of the
    template its sandboxes start from (template_builds.current); prebuilt:
    whether that is one of its own (template_builds.own)."""
    status = build.get("status")
    if not prebuilt:  # the platform's default: usable once any build of it is
        return "default" if build.get("template") else "building" if status == "building" else "missing"
    if status in ("building", "failed"):
        return status
    if status != "built" or not build.get("template"):   # a row without a built template: none to start from
        return "missing"
    return "stale" if build.get("stale") else "built"


def problems(atts: list) -> list:
    return [a for a in atts if a["state"] not in ("ready", "disabled")]


def _step(step, status, detail, who=None, missing=(), **extra) -> dict:
    return {"step": step, "status": status, **({"who": who} if who and status != "done" else {}),
            "missing": list(missing), "detail": detail, **extra}


def steps(info: dict, atts: list, missing: list, build: dict, live: bool, admin: bool = True) -> dict:
    """The checklist from what is known about the repo: info (repos.info),
    its attachments, the values its recipe misses, its template build, and
    whether one of its sandboxes is up; for an admin or not (the repo's
    values are its admins' to set)."""
    served, recipe, public = bool(info["trust"]), info["configured"], info["trust"] == "public"
    out = [_step("served", "done", "Trusted: sandboxes get its secrets and services" if info["trust"] == "trusted"
                 else "Public: cloned anonymously, no secrets or services") if served
           else _step("served", "todo", "This server does not serve the repository yet", "admin")]
    if public:
        out.append(_step("github", "done", "Cloned anonymously"))
    elif info["github"] in ("app", "key"):
        out.append(_step("github", "done", "GitHub App installed" if info["github"] == "app"
                         else "Deploy key (the GitHub App is recommended)"))
    elif info.get("github_anonymous"):
        out.append(_step("github", "done", "Cloned anonymously; install the GitHub App to push or for private repos"))
    else:
        out.append(_step("github", "todo" if served else "blocked",
                         "Install the GitHub App on this repository so sandboxes can push (and clone it, if it is private)", "admin",
                         **({"action": info["github_app_install"]} if info.get("github_app_install") else {})))
    out.append(_step("configured", "done", f"Previews: {', '.join(info['previews'])}" if info["previews"] else "Recipe found")
               if recipe else _step("configured", "todo" if served else "blocked",
                                    "Your coding agent writes .sbx/ and verifies it in a sandbox", "agent"))
    if not recipe:
        out += [_step(step, "blocked", "Available once the recipe exists") for step in ("services", "env", "template")]
    else:
        found = problems(atts)
        if public and info.get("services"):  # create refuses them (core._create_reserved)
            out.append(_step("services", "todo", "Public repos get no services: remove services: from .sbx/sbx.yaml, "
                             "or have an admin trust the repo", "agent", info["services"]))
        elif found:
            actors = {PROBLEM_ACTOR.get(a["state"], "person") for a in found}
            who = next(a for a in ("agent", "admin", "person") if a in actors)
            out.append(_step("services", "todo", "; ".join(f"{a['key']}: {a['problem'] or a['state'].replace('_', ' ')}"
                                                          for a in found), who, [a["key"] for a in found]))
        else:
            out.append(_step("services", "done", f"{len(atts)} attached and healthy" if atts else "None needed"))
        if public:
            out.append(_step("env", "done", "Public: its sandboxes get no secrets"))
        elif missing:
            out.append(_step("env", "todo", "A value the recipe asks for is not set" if len(missing) == 1
                             else f"{len(missing)} values the recipe asks for are not set",
                             "person" if admin else "admin", missing))
        else:
            out.append(_step("env", "done", "Every value the recipe asks for is set"))
        key = template_key(info["prebuilt"], build)
        status, who, _, _, detail = TEMPLATE[key]
        out.append(_step("template", status, (key == "failed" and build.get("error")) or detail, who))
    out.append(_step("sandbox", "done", "A sandbox is up") if live
               else _step("sandbox", "todo" if recipe else "blocked", "Start a sandbox and open its preview", "person"))
    out.append(published(info))
    before = [s for s in out if s["step"] not in ("sandbox", "recipe_pr")]   # what sandboxes need
    todo = [s for s in before if s["status"] == "todo"]
    stage = ("agent" if any(s.get("who") == "agent" for s in todo) else "person" if todo else
             "working" if any(s["status"] == "working" for s in before) else "live" if live else "ready")
    first = next((s for s in out if s["status"] in ("todo", "working")), None)
    return {"steps": out, "stage": stage, "done": sum(s["status"] == "done" for s in out), "total": len(out),
            "next": first["step"] if first else None}


def published(info: dict) -> dict:
    """Whether the recipe the server runs is in the repository: one imported
    from the default branch is; the operator's copy (sbx recipe apply) gets
    a pull request once a sandbox came up healthy from it (setup_pr)."""
    if not info["configured"]:
        return _step("recipe_pr", "blocked", "Available once the recipe exists")
    if info.get("recipe_source") != "server":
        return _step("recipe_pr", "done", "The recipe is the repository's .sbx/")
    pr = info.get("recipe_pr")
    if not pr:
        return _step("recipe_pr", "blocked", "sbx opens a pull request with the recipe once a sandbox comes up "
                                             "healthy from it")
    link = {"url": pr["url"]} if pr["url"] else {}
    named = f"PR #{pr['number']}" if pr["number"] else "the pull request"
    if pr["problem"]:
        action = {"action": info["github_app_install"]} if info.get("github_app_install") else {}
        return _step("recipe_pr", "todo", pr["problem"], "admin", **link, **action)
    if not pr["current"]:
        return _step("recipe_pr", "todo", f"The recipe changed since {named}; it is updated once a sandbox comes "
                                          "up healthy from the new one", "agent", **link)
    if pr["state"] == "in_repository":
        return _step("recipe_pr", "done", "The default branch has this recipe already")
    if pr["state"] == "merged":
        return _step("recipe_pr", "done", f"Merged in {named}", **link)
    if pr["state"] == "closed":
        return _step("recipe_pr", "todo", f"{named} was closed without merging: reopen it, or run sbx recipe pr",
                     "person", **link)
    return _step("recipe_pr", "todo", f"Review and merge {named}: it commits .sbx/ to the repository", "person", **link)


def of(repo: str, email: str) -> dict:
    """GET /api/repos/{repo}/readiness: the checklist for the person asking
    (their own values count)."""
    info = repos.info(repo)
    trusted = info["trust"] == "trusted"
    view = env.repo_env(repo, email) if trusted else None
    build = template_builds.current(repo, runtimes.of_repo(repo)) if info["configured"] else {}
    try:
        sandboxes = views.collect()
    except Exception as error:  # the runtime not answering must not hide the rest
        print(f"readiness: listing sandboxes: {error!r}", flush=True)
        sandboxes = []
    live = any((i.get("app") or "").lower() == repo and i.get("status") == "up" for i in sandboxes)
    return {"repo": repo, "info": info,
            **steps(info, view["attachments"] if view else [], view["missing"] if view else info["secrets_missing"],
                    build, live, auth.is_admin(email))}


def list_state(trust, configured: bool, prebuilt: bool, build: dict, env_missing: int, package_problems: int) -> dict:
    """The repo list's one line: the first thing a repo needs, and who acts."""
    if not trust:
        return {"label": "Not served", "level": "idle", "who": "admin"}
    if not configured:
        return {"label": "Needs a recipe", "level": "warn", "who": "agent"}
    if package_problems:
        return {"label": "A package needs attention" if package_problems == 1
                else f"{package_problems} packages need attention", "level": "warn", "who": "admin"}
    if env_missing:
        return {"label": "1 value missing" if env_missing == 1 else f"{env_missing} values missing",
                "level": "warn", "who": "person"}
    status, who, level, label, _ = TEMPLATE[template_key(prebuilt, build)]
    if status != "done":
        return {"label": f"Template {label.lower()}", "level": level, "who": who}
    return {"label": "Ready", "level": "ok"}


def summaries() -> list:
    """GET /api/repos: every repo the installation knows, with its state."""
    counts = views.sandbox_counts()
    out = []
    for repo in views.known_repos():
        a, trust = recipes.load(repo), settings.trust(repo)
        own = a.has_recipe and template_builds.own(repo)
        build = template_builds.current(repo, runtimes.of_repo(repo)) if a.has_recipe else {}
        trusted = trust == "trusted" and a.has_recipe
        out.append({"repo": repo, "trust": trust, "configured": a.has_recipe, "prebuilt": own,
                    "built_at": build.get("finished_at") if build.get("template") else None, "sandboxes": counts.get(repo, 0),
                    "recipe_pr": setup_pr.record(repo),
                    "state": list_state(trust, a.has_recipe, own, build,
                                        len(env.missing(repo)) if trusted else 0,
                                        len(problems(attachments.effective(repo))) if trusted else 0)})
    return out


def template(repo: str) -> dict:
    """GET /api/repos/{repo}/template, without the log: the template its
    sandboxes start from on its runtime (runtime); with several runtimes,
    the same for each other one under runtimes."""
    own = template_builds.own(repo)
    kind = runtimes.of_repo(repo)
    out = {"repo": repo, **_template_on(repo, own, kind)}
    if len(runtime.enabled()) > 1:
        out["runtimes"] = [_template_on(repo, own, other) for other in runtime.enabled() if other != kind]
    return out


def _template_on(repo: str, own: bool, kind: str) -> dict:
    build = template_builds.current(repo, kind)  # its own, else the default one its sandboxes start from
    key = template_key(own, build)
    _, _, level, label, _ = TEMPLATE[key]
    return {**build, "runtime": kind, "status": build.get("status", "-"), "state": key, "label": label,
            "level": level}
