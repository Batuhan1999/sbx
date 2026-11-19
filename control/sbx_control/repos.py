"""What the installation knows about a repo, and the brief an agent gets to
set one up."""
import json
import pathlib
import posixpath
import re

from . import addressing, catalog, env, github_app, paths, recipes, runtimes, settings, setup_pr, template_builds
from .catalog import attachments, connections

BRIEFS = pathlib.Path(__file__).parent / "template_files"
RESOURCE_GUIDE = BRIEFS / "resource-guide.md"
# Where the docs the brief carries link to: the agent reading it has no sbx source.
SOURCE = "https://github.com/Batuhan1999/sbx/blob/main"


def render_brief(name: str, **values) -> str:
    """An agent brief from template_files/<name> with its {placeholders}
    filled in; any other braces are the brief's own text."""
    text = (BRIEFS / name).read_text()
    return re.sub(r"\{([a-z_]+)\}", lambda m: str(values[m[1]]) if m[1] in values else m[0], text)


def info(repo: str) -> dict:
    """Whether and how the installation serves the repo, its sbx.yaml as the
    server reads it, which secrets its sbx.yaml asks for that aren't set, and
    the runtime its sandboxes run on when an admin chose one (runtime; null:
    the installation's default)."""
    trust = settings.trust(repo)
    a = recipes.load(repo)
    build = template_builds.current(repo, runtimes.of_repo(repo))
    sha = recipes.fingerprint(a)
    return {"repo": repo, "trust": trust, "configured": a.has_recipe, "prebuilt": template_builds.own(repo),
            "template": build.get("template") or "", "recipe_sha256": sha,
            "recipe_source": ("server" if a.operator else "repository") if a.has_recipe else "",
            "recipe_pr": recipe_pr(repo, sha),
            "previews": sorted(k.lstrip("-") or "web" for k, p in addressing.previews(a).items() if p["link"]), "secrets_missing": env.missing(repo),
            "services": sorted(a.spec.get("services") or {}),
            "template_build": build.get("status") or "-", "template_stale": bool(build.get("stale")),
            "github": github_app.access(repo) if trust else "-", "github_app_install": github_app.install_url(),
            "github_anonymous": bool(trust) and github_app.reachable_anonymously(repo),
            "runtime": runtimes.setting(repo)}


def recipe_pr(repo: str, sha: str) -> dict | None:
    """The recipe's pull request as last recorded (setup_pr.record), and
    whether it carries the recipe the server runs now (current); None
    before sbx tried to open one."""
    found = setup_pr.record(repo)
    return {**found, "current": bool(sha) and found["recipe_sha256"] == sha} if found else None


def setup_brief(repo: str, email: str) -> str:
    """What an agent gets to set a repo up: the .sbx/ format, the loop, and
    what the server knows about the repo (its env's names, never values)."""
    names = []
    for e in catalog.listing("provision"):
        about = re.split(r"\.\s", " ".join(e.description.split()))[0]
        names.append(f"`{e.name}` ({about[:140]})" + (f", needs a {', '.join(r['kind'] for r in e.requires.values())}" if e.requires else ""))
    env_now = ""
    connected = connections.listing()
    if connected:
        env_now += '\n\nSaved connections (inspect these before creating infrastructure):\n'
        env_now += '\n'.join(f"- `{s['key']}`: `{s['package']}`; check {s.get('check_status') or 'not checked'}." for s in connected)
    if settings.trust(repo) == "trusted":
        view = env.repo_env(repo, email)
        have = sorted({row["name"] for row in view["variables"]})
        env_now = (f" Set already ({len(have)}): {', '.join(have)}." if have else " Nothing is set yet.") + env_now
        if view["missing"]:
            env_now += f" Asked for by sbx.yaml but missing: {', '.join(view['missing'])}."
        attached = attachments.effective(repo)
        if attached:
            env_now += "\n\nServices already initialized on this installation (reuse these exact keys and packages; their inputs and credentials stay on the server):\n"
            env_now += "\n".join(f"- `{a['key']}`: uses `{a['package']}`; connections {json.dumps(a['connections'])}; exports {', '.join(a['exports']) or 'none'}." for a in attached)
    brief = render_brief("setup-brief.md", repo=repo, control=settings.current().base_url,
                         services=", ".join(names) or "none", env_now=env_now)
    return "\n\n".join([brief, *guides()])


MADE = "How this recipe was made"


def guides() -> list:
    """The canonical docs the setup brief carries after its steps: the
    worked examples (their index, and each one's MADE section, where it has
    one), service selection, the resource guide;
    their relative links point into the sbx repository on GitHub."""
    docs, examples = paths.shipped("docs"), paths.shipped("examples")
    parts = []
    if (examples / "README.md").is_file():
        parts.append(linked((examples / "README.md").read_text().strip(), "examples"))
    for readme in sorted(examples.glob("*/README.md")):
        text = readme.read_text()
        made = section(text, MADE)
        if made:
            title = re.search(r"^# (.+)$", text, re.M)
            folder = f"examples/{readme.parent.name}"
            parts.append(f"# {MADE}: {title[1].strip() if title else readme.parent.name} "
                         f"({SOURCE}/{folder})\n\n{linked(made, folder)}")
    return [*parts, linked((docs / "service-selection.md").read_text().strip(), "docs"),
            linked(RESOURCE_GUIDE.read_text().strip(), "control/sbx_control/template_files")]


def linked(markdown: str, folder: str) -> str:
    """markdown, whose relative links are relative to folder (in the sbx
    repository), with those links on GitHub; anchors and URLs stay."""
    def absolute(match):
        path, _, anchor = match[1].partition("#")
        return f"]({SOURCE}/{posixpath.normpath(posixpath.join(folder, path))}{'#' + anchor if anchor else ''})"
    return re.sub(r"\]\((?![a-z][a-z0-9+.-]*:|#)([^)\s]+)\)", absolute, markdown)


def section(markdown: str, heading: str) -> str:
    """The body of a document's `## heading` section, up to the next `## `;
    "" when it has none."""
    match = re.search(rf"^## {re.escape(heading)}[ \t]*\n(.*?)(?=^## |\Z)", markdown, re.M | re.S)
    return match[1].strip() if match else ""
