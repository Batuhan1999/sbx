"""What the sandbox lifecycle asks of the catalog: the one interface the
core calls it through (extensions.packages()). Everything a repo's
attachments make for a sandbox, keep up to date, run hooks on and remove,
and what they contribute to its template and runtime."""
from __future__ import annotations

from typing import Callable

from .. import recipes
from . import PackageError, attachments, resources

Error = PackageError


def provision(name: str, repo: str, inputs: Callable[[str, dict, dict], dict]) -> dict:
    """Make the repo's enabled attachments' resources for a new sandbox, in
    order: key -> resources.Created. inputs(key, with, made so far) settles
    an attachment's with: values (they may name what was made above)."""
    if resources.delete_all(name):
        raise PackageError(f"what an earlier sbx-{name} left in its services could not be removed; try another name")
    for key, use in (recipes.load(repo).spec.get("services") or {}).items():
        if not isinstance(use, dict) or not use.get("uses"):
            raise PackageError(f"sbx.yaml services.{key}: needs uses: <service>")
    created: dict = {}
    for key, uses, with_, chosen in attachments.for_create(repo):
        created[key] = resources.create(name, repo, key, uses, inputs(key, with_, created), len(created), chosen)
    return created


def refresh(name: str, repo: str) -> dict:
    """An existing sandbox's resources after its live attachments are
    reconciled: key -> resources.Created."""
    resources.reconcile_live(name, repo)
    return resources.ready(name)


def release(name: str) -> list:
    """Delete the sandbox's resources; the failures, as text (their records
    stay for reconcile)."""
    return resources.delete_all(name)


def paused(name: str) -> list:
    """Run the sandbox's resources' pause hooks; the failures, as text."""
    return resources.run_hooks(name, "pause")


def reconcile(existing: set):
    """Delete what sandboxes that are gone left behind."""
    resources.reconcile(existing)


def contributions(target) -> list:
    return resources.contributions(target)


def scrub_paths(name: str) -> set:
    return resources.scrub_paths(name)
