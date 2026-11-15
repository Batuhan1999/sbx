"""Read-only web views combining runtime sandboxes, control state, and recipes."""
import json
import sqlite3
from types import SimpleNamespace

from . import (core, extensions, lifecycle, recipes, runtime, sandboxes, settings, template_builds, templates,
    variables)
from .catalog import attachments
from .state import db

MAX_FILE_BYTES = 64 * 1024


def origin(info=None) -> dict:
    """Where a sandbox came from: the extension that made it says how (an
    agent's session, a PR's preview); else a person (sbx up, or the web app)."""
    found = extensions.origin(info) if info is not None else None
    return found or {"kind": "person", "label": "Manual", "href": ""}


def collect() -> list:
    """Every sandbox: the runtime's, with where it came from and the creation
    time sbx-control keeps; and the ones still being created or that failed
    before their VM existed."""
    infos = [i for i in core.sandboxes() if (i.metadata or {}).get("sbx")]
    rows = sandboxes.rows()
    out = []
    for info in infos:
        name = info.metadata["sbx"]
        row = rows.pop(name, None) or {}
        item = sandboxes.describe(info)
        item.update(vm=core.runtime_state(info), created_at=row.get("created_at") or getattr(info, "started_at", None),
                    updated_at=(item["lifecycle"] or {}).get("updated_at") or row.get("updated_at"),
                    slug=core.slug_of(info), template=info.metadata.get("template", ""),
                    end_at=getattr(info, "end_at", None),
                    origin=origin(info))
        out.append(item)
    for name, row in rows.items():
        current = lifecycle.get(name)
        status = lifecycle.status(current)
        if status in ("creating", "failed"):
            meta = row["metadata"]
            out.append({"name": name, "owner": row["owner"], "branch": row["branch"], "status": status,
                        "error": current["error"], "urls": {}, "ports": [], "app": "", "vm": "", "sandbox_id": "",
                        "audience": core.audience(meta), "shared": core.shared(meta),
                        "actions": lifecycle.actions(current, status),
                        "created_at": row["created_at"], "updated_at": current["updated_at"],
                        "slug": row.get("slug") or name, "template": "", "logs": [],
                        "origin": origin(SimpleNamespace(metadata={**meta, "sbx": name}))})
    return out


def visible(item: dict, email: str, scope: str) -> bool:
    if scope == "previews":
        return item.get("audience") == "previews"
    if scope == "all":
        return True
    return item.get("owner") == email


def find_item(name: str):
    return next((i for i in collect() if i["name"] == name), None)


def snapshot_usage(sandbox_id: str) -> dict:
    return runtime.snapshot_usage(sandbox_id)


def services_of(name: str) -> list:
    """What services hold for the sandbox: keys, packages, status and the
    outputs the package declares not sensitive (the rest stay sealed)."""
    try:
        with db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("select * from sandbox_services where sandbox = ? order by position", (name,)).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        r = dict(r)
        try:
            public = json.loads(r.get("outputs") or "{}")
        except ValueError:
            public = {}
        out.append({"key": r["key"], "uses": r["uses"], "status": r["status"], "kind": r.get("kind") or "installation",
                    "created_at": r.get("created_at"), "outputs": public})
    return out


def known_repos() -> list:
    """Every repo the installation has seen: listed exactly in its repos, in a
    sandbox, holding values or attachments, or with an sbx.yaml (the
    operator's or a template build's). The one answer to "which repos are
    there"."""
    found = {r.lower() for r in settings.current().repos if "*" not in r}
    try:
        found |= {(i.metadata or {}).get("repo") or "" for i in core.sandboxes()}
    except Exception as error:
        print(f"views: listing sandboxes for repos: {error!r}", flush=True)
    found |= variables.repos()
    found |= {r.lower() for r in recipes.with_recipe()}
    found |= set(template_builds.builds())
    found |= set(attachments.repos())
    return sorted(r for r in found if r.count("/") == 1)


def sandbox_counts() -> dict:
    counts: dict = {}
    try:
        for i in core.sandboxes():
            repo = core.app_of(i).repo
            counts[repo] = counts.get(repo, 0) + 1
    except Exception:
        pass
    return counts


def sbx_files(a: recipes.App) -> list:
    """(relative path, text or None for binary/too big) of the repo's .sbx/
    as the server reads it; nothing outside the folder."""
    if not a.has_recipe:
        return []
    out = []
    for entry in recipes.walk(a.dir):
        if not entry.regular or recipes.ignored(entry.path):
            continue
        text = None
        if entry.size <= MAX_FILE_BYTES:
            try:
                text = entry.read().decode()
            except (UnicodeDecodeError, OSError):
                text = None
        out.append((entry.path, text))
    return out


def build_log(repo: str) -> list:
    return templates.build_log(repo)
