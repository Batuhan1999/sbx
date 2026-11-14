"""Template builds: the one owner of their state, start and schedule.

Each template (templates.BASE, the default one at each size the served
repos ask for, every repo that has one of its own (own)) has a row
in template_builds: its latest build, a small state machine (TRANSITIONS,
moved only by _transition), and the template that build produced last, with
its fingerprint. A template is stale when the plan
it would build now (templates.plan) has another fingerprint than the one
built: a connector enabled, a version bumped, .sbx/ changed, the platform
layer changed. The scheduler builds stale templates one at a time (a build
that failed is tried again when its inputs change, or on request), and at
night those whose repo's default branch moved since (template_moved): a
build takes minutes and writes gigabytes, which stalls running sandboxes.

Sandboxes start from the template a key built last (template_for): a
repo's own when it has one (own), else the default one of its size. A
create before that template is built starts its build and is refused (503). Every template is built
at its sandboxes' size, which the row keeps for capacity (size_of).

  python -m sbx_control.template_builds base|default [OWNER/REPO]|app OWNER/REPO
      build one now (install.sh builds base and default); default with a
      repo: the default template at that repo's size"""
import argparse
import os
import sys
import threading
import time

from . import extensions, recipes, settings, templates
from .state import Schema, db

NIGHTLY_REBUILD_HOUR = 2  # host time
CHECK_SECONDS = 300
FIELDS = ("status", "started_at", "finished_at", "error", "fingerprint", "built_fingerprint", "built_template", "pid",
          "built_memory_mb")
# The machine: a build starts from any settled state and settles as built or
# failed. A build whose process is gone reads as failed (status()).
TRANSITIONS = {None: {"building"}, "built": {"building"}, "failed": {"building"}, "building": {"built", "failed"}}

_lock = threading.Lock()
_running: set = set()  # keys this process is building


class NotBuilt(Exception):
    """No template a sandbox could start from is built yet."""


def _utc() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _alive(pid) -> bool:
    if not pid:
        return False
    if pid == os.getpid():
        return False  # this process: only what it is running now (_running) counts
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def status(key: str, row: dict | None) -> str | None:
    """The build's status: building, built, failed, or None for no build. A
    build that no process is running any more was cut off."""
    if row is None:
        return None
    if row["status"] == "building" and key not in _running and not _alive(row["pid"]):
        return "failed"
    return row["status"]


def _read(conn, key: str) -> dict | None:
    row = conn.execute(f"select {', '.join(FIELDS)} from template_builds where repo = ?", (key,)).fetchone()
    return dict(zip(FIELDS, row)) if row else None


def _transition(key: str, to: str, **facts):
    with db() as conn:
        conn.execute("begin immediate")
        try:
            row = _read(conn, key)
            before = status(key, row)
            if to not in TRANSITIONS[before]:
                raise ValueError(f"template build {key}: {before} -> {to} is not allowed")
            values = {**(row or {}), **facts, "status": to}
            conn.execute(f"insert into template_builds (repo, {', '.join(FIELDS)}) values (?{', ?' * len(FIELDS)}) "
                         f"on conflict(repo) do update set {', '.join(f'{f}=excluded.{f}' for f in FIELDS)}",
                         (key, *(values.get(f) for f in FIELDS)))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def plan(key: str) -> templates.Plan:
    platform_owned = key == templates.BASE or templates.is_default(key)
    target = (extensions.Target(repo="") if platform_owned else
              extensions.Target(repo=key, trusted=settings.trust(key) == "trusted"))
    return templates.plan(key, extensions.contributions_for(target))


def state(key: str) -> dict:
    """{status, started_at, finished_at, error, template, stale} of key's
    latest build; {} for none."""
    with db() as conn:
        row = _read(conn, key)
    if row is None:
        return {}
    out = {f: row[f] for f in ("started_at", "finished_at", "error", "built_template")}
    out["status"] = status(key, row)
    if row["status"] == "building" and out["status"] == "failed":
        out["error"] = "The control service restarted during this build. Build the template again."
    out["template"] = row["built_template"]
    try:
        out["stale"] = bool(row["built_fingerprint"]) and plan(key).fingerprint != row["built_fingerprint"]
    except (recipes.RecipeError, OSError):
        out["stale"] = False
    return out


def builds() -> dict:
    with db() as conn:
        keys = [row[0] for row in conn.execute("select repo from template_builds")]
    return {key: state(key) for key in keys}


def building(key: str) -> bool:
    return key in _running


def built(key: str) -> str:
    with db() as conn:
        row = _read(conn, key)
    if not row or not row["built_template"]:
        raise NotBuilt(f"the {key} template is not built yet; it builds by itself shortly")
    return row["built_template"]


def own(repo: str | None) -> bool:
    """Whether the repo's sandboxes start from a template of its own: it is
    trusted (only trusted repos' templates are built), and its sbx.yaml asks
    for one or a package attached to it adds a template layer, which reaches
    only the templates of the repos that attach it."""
    if not repo or settings.trust(repo) != "trusted":
        return False
    a = recipes.load(repo)
    if not a.has_recipe:
        return False
    if a.prebuilt:
        return True
    target = extensions.Target(repo=repo, trusted=True)
    return any(c.template for c in extensions.packages().contributions(target))


def current(repo: str) -> dict:
    """state() of the template the repo's sandboxes start from: its own,
    else the default one of its size."""
    return state(repo if own(repo) else default_for(repo))


def default_for(repo: str | None) -> str:
    """The key of the default template the repo's sandboxes start from without one of their own."""
    return templates.default_key(recipes.load(repo).size) if repo else templates.DEFAULT


def template_for(repo: str | None) -> str:
    """The template a new sandbox of the repo starts from: the repo's own
    built last when it has one of its own (own), else the default one of its
    size. One not built yet starts building here (unless its last build of
    the same plan failed) and the create is refused with NotBuilt (503): it
    is what the repo's readiness shows as missing."""
    key = repo if own(repo) else default_for(repo)
    with db() as conn:
        row = _read(conn, key)
    if row and row["built_template"]:
        return row["built_template"]
    raise NotBuilt(_building(key, row))


def _building(key: str, row: dict | None) -> str:
    """Start key's build unless it is running or failed on this plan; what a refused create says."""
    try:
        failed = status(key, row) == "failed" and row["fingerprint"] == plan(key).fingerprint
    except (recipes.RecipeError, OSError) as error:
        return f"the {key} template is not built yet and cannot be planned: {error}"
    if failed:
        return (f"the {key} template is not built: its last build failed ({(row['error'] or '').strip()[-300:]}); "
                f"fix it and build again: sbx-admin template build {key}")
    if status(key, row) != "building":
        start(key)
    return f"the {key} template is building; try again in a few minutes"


def size_of(template: str) -> int | None:
    """The memory (MiB) of every VM started from a template the platform
    built, or None for one it did not record (a snapshot)."""
    with db() as conn:
        row = conn.execute("select built_memory_mb from template_builds where built_template = ?",
                           (template,)).fetchone()
    return row[0] if row and row[0] else None


def _claim(key: str) -> bool:
    with _lock:
        if key in _running:
            return False
        _running.add(key)
        return True


def build_now(key: str):
    """Build key's template here, the base first when the platform layer changed."""
    _begin(key)
    _build(key)


def start(key: str) -> bool:
    """build_now on a thread; False when one is already running for key."""
    try:
        _begin(key)
    except RuntimeError:
        return False

    def run():
        try:
            _build(key)
        except BaseException as error:  # recorded as the build's error
            print(f"template build {key}: {error!r}", flush=True)
    threading.Thread(target=run, daemon=True).start()
    return True


def _begin(key: str):
    with _lock:
        try:
            if key in _running:
                raise ValueError
            _transition(key, "building", started_at=_utc(), finished_at=None, error=None, fingerprint=None,
                    pid=os.getpid())
        except ValueError:  # here, or in another process
            raise RuntimeError(f"{key}'s template is already building") from None
        _running.add(key)


def _build(key: str):
    """Build key, begun by this process; its claim ends with it."""
    p = None
    try:
        try:
            platform_owned = key == templates.BASE or templates.is_default(key)
            if key != templates.BASE:
                with db() as conn:
                    base = _read(conn, templates.BASE)
                if not base or base["built_fingerprint"] != plan(templates.BASE).fingerprint:
                    build_now(templates.BASE)
            git_dir = None
            if not platform_owned:
                git_dir = recipes.mirror(key, extensions.git_access(key))
                # The default branch's recipe, kept for the platform to read.
                if not recipes.load(key).operator and not recipes.import_from(key, recipes.from_mirror(git_dir)):
                    raise recipes.RecipeError(f"{key}: no .sbx/sbx.yaml on its default branch")
            if not platform_owned and not own(key):
                # Only services, env, previews or a size: its sandboxes start
                # from the default template of its size, built now if it isn't.
                sized = default_for(key)
                if sized in stale():
                    build_now(sized)
                _transition(key, "built", finished_at=_utc(), built_fingerprint=None, built_template=None)
                return
            p = plan(key)
            print(f"template build {key}: {p.name}", flush=True)
            templates.build(p, git_dir)
            _transition(key, "built", finished_at=_utc(), fingerprint=p.fingerprint, built_fingerprint=p.fingerprint,
                        built_template=p.name, built_memory_mb=p.size["memory_mb"])
        except BaseException as error:
            _transition(key, "failed", finished_at=_utc(), error=str(error)[-2000:], fingerprint=p and p.fingerprint)
            raise
    finally:
        _running.discard(key)


def keys() -> list:
    served = [r for r in recipes.with_recipe() if settings.trust(r)]
    sizes = set()
    for repo in served:
        try:
            sizes.add(templates.default_key(recipes.load(repo).size))
        except recipes.RecipeError as error:
            print(f"template keys: {repo}: {error}", flush=True)
    sizes.discard(templates.DEFAULT)
    return [templates.BASE, templates.DEFAULT, *sorted(sizes),
            *(r for r in served if own(r))]


def stale() -> list:
    """The keys whose template, as planned now, differs from the one built
    (or none was), but for one whose last build of that plan failed."""
    out = []
    with db() as conn:
        rows = {key: _read(conn, key) for key in keys()}
    for key, row in rows.items():
        if key in _running or (row and status(key, row) == "building"):
            continue
        fingerprint = plan(key).fingerprint
        if row and status(key, row) == "failed" and row["fingerprint"] == fingerprint:
            continue
        if not _current(row, fingerprint):
            out.append(key)
    return out


def _current(row: dict | None, fingerprint: str) -> bool:
    """Whether the template built last (row) is the one planned now. A row
    without a built template (built while the repo had none of its own) is not."""
    return bool(row) and bool(row["built_template"]) and row["built_fingerprint"] == fingerprint


def mark_moved(repo: str):
    """The repo's default branch moved since its last build: rebuild at night."""
    with db() as conn:
        conn.execute("insert into template_moved (repo) values (?) on conflict do nothing", (repo,))


def moved() -> list:
    with db() as conn:
        return [row[0] for row in conn.execute("select repo from template_moved order by repo")]


def _unmark(repo: str):
    with db() as conn:
        conn.execute("delete from template_moved where repo = ?", (repo,))


def _build_quietly(key: str):
    try:
        build_now(key)
    except Exception as error:
        print(f"template build {key}: {error!r}", flush=True)


def schedule(stop: threading.Event):
    """Until stop: every CHECK_SECONDS the stale templates, and at
    NIGHTLY_REBUILD_HOUR the moved repos', one at a time, for the disk's sake."""
    night = None
    while not stop.wait(CHECK_SECONDS):
        try:
            for key in stale():
                if stop.is_set():
                    return
                _build_quietly(key)
            today = time.strftime("%Y-%m-%d")
            if time.localtime().tm_hour == NIGHTLY_REBUILD_HOUR and night != today:
                night = today
                for repo in moved():
                    _unmark(repo)
                    if repo in keys():
                        _build_quietly(repo)
        except Exception as error:  # one bad round must not stop the scheduler
            print(f"template scheduler: {error!r}", flush=True)


def main(argv=None) -> int:
    from . import platform
    from .state import migrate
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("which", choices=["base", "default", "app"])
    parser.add_argument("repo", nargs="?", help="app: owner/repo; default: a repo whose size to build it at")
    parser.add_argument("--if-stale", action="store_true", help="only when the built template is not the one planned now")
    args = parser.parse_args(argv)
    migrate()
    platform.connect()
    key = {"base": templates.BASE, "default": default_for((args.repo or "").lower() or None)}.get(args.which) \
        or (args.repo or "").lower()
    if args.which == "app" and not settings.trust(key):
        raise SystemExit(f"app needs one of the repos this installation serves: {', '.join(settings.current().repos)}")
    with db() as conn:
        row = _read(conn, key)
    if args.if_stale and _current(row, plan(key).fingerprint):
        print(f"CURRENT {row['built_template'] or key}", file=sys.stderr)
        return 0
    build_now(key)
    print(f"BUILT {state(key).get('template') or key}", file=sys.stderr)
    return 0


# A template's latest build, keyed by repo or by the platform's own template;
# repos whose default branch moved since their template's last build.
SCHEMA = Schema(tables="""
create table if not exists template_builds (
    repo text primary key, status text not null, started_at text, finished_at text, error text);
create table if not exists template_moved (repo text primary key, since text not null default current_timestamp);
""", columns=(("template_builds", "fingerprint", "text"), ("template_builds", "built_fingerprint", "text"),
              ("template_builds", "built_template", "text"), ("template_builds", "pid", "integer"),
              ("template_builds", "built_memory_mb", "integer")))


if __name__ == "__main__":
    sys.exit(main())
