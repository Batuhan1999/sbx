"""Amp's orb jobs: its provider (the connector's host unit) runs dispatch.sh
for every job, which posts it to sbx-control (dispatch); the job route runs
handle_job, which spawns, pauses and deletes the orb's sandbox through the
public sandbox API and starts Amp's bootstrap in it.

  sbx-admin amp dispatch|provider|list|routes|snapshots list|delete KEY|build [--repo R]"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shlex
import sys
import time
from pathlib import Path

import httpx
from ... import addressing, core, lifecycle, platform, recipes, runtime, sandboxes, settings, vault
from . import snapshots
from .common import CLI, KIND, TOKEN, JobError, current, orb_lock

BOOTSTRAP = Path(__file__).parent / "bootstrap.py"
DISPATCH = Path(__file__).parent / "dispatch.sh"
WORKSPACE = f"{platform.HOME}/workspace/repo"  # where Amp's Repository Setup=false expects the checkout
PREVIEWS = ".amp/portals/sbx-previews.json"


def sandboxes_of(orb_id: str | None = None):
    metadata = {"connector": KIND}
    if orb_id is not None:
        metadata["orbId"] = orb_id
    return runtime.sandboxes(metadata, [runtime.RUNNING, runtime.PAUSED])


def stop_bootstrap(sbx):
    """Stop the orb's `amp orb bootstrap` in its VM, keeping the headless
    executor and the app: a snapshot must not keep a bootstrap that would
    retry its expired ORB_TOKEN on every resume (the next spawn brings a
    fresh one). Raises when it would not stop."""
    helper = f"{platform.RUN}/amp-bootstrap.py"
    core.ship_guest(sbx)   # the bootstrap imports it
    sbx.files.write(helper, BOOTSTRAP.read_text(), user="user")
    sbx.commands.run(f"python3 {helper} --stop", user="user", timeout=30)


def validate_job(env: dict) -> dict:
    job = {key: env[key] for key in (
        "ORB_OPERATION", "ORB_ID", "ORB_SNAPSHOT_ID", "ORB_TYPE", "ORB_TOKEN", "AMP_URL",
    ) if key in env}
    if any(not isinstance(value, str) for value in job.values()):
        raise JobError("Amp dispatch fields must be strings")
    if job.get("ORB_OPERATION") not in ("spawn", "pause", "delete"):
        raise JobError("ORB_OPERATION must be spawn, pause, or delete")
    orb_id = job.get("ORB_ID", "")
    if not orb_id or len(orb_id) > 200 or any(ord(c) < 32 for c in orb_id):
        raise JobError("ORB_ID must contain 1–200 printable characters")
    if job["ORB_OPERATION"] == "spawn" and not job.get("ORB_TOKEN"):
        raise JobError("spawn requires ORB_TOKEN")
    return job


def dispatch(env: dict):
    # The short-lived dispatch process must not own an app preparation thread.
    job = validate_job(env)
    # Log names only: setup logs are visible in Amp and must never contain tokens.
    fields = sorted(key for key in env if key == "AMP_URL" or key.startswith("ORB_"))
    print("Amp dispatch fields: " + ", ".join(fields), file=sys.stderr, flush=True)
    started = time.monotonic()
    if job["ORB_OPERATION"] == "pause":
        current = lifecycle.get(sandbox_name(job["ORB_ID"]))
        while current and current["active"]:
            if time.monotonic() - started >= 150:
                raise JobError("application preparation is still running; retry pause")
            time.sleep(0.5)
            current = lifecycle.get(sandbox_name(job["ORB_ID"]))
    # A spawn may confirm its bootstrap started (STARTED_PROBE) after the rest of its work.
    with httpx.Client(timeout=max(1, 270 + STARTED_PROBE - (time.monotonic() - started)),
                      trust_env=False) as client:
        response = client.post(f"http://127.0.0.1:{platform.CONTROL_PORT}/internal/amp/jobs", json=job)
        if response.status_code == 400:
            raise JobError(response.json()["detail"])
        response.raise_for_status()
        return response.json()


STARTED_PROBE = 60   # seconds a spawn waits for its bootstrap to say it started


def sandbox_name(orb_id: str) -> str:
    return "amp-" + hashlib.sha256(orb_id.encode()).hexdigest()[:24]


def routes() -> list[dict]:
    return [{"repo": repo, "orb_type": core.slugify(repo, 80),
             "snapshot_ready": (recipes.load(repo).dir / "snapshot").is_file()
                and os.access(recipes.load(repo).dir / "snapshot", os.X_OK)}
            for repo in recipes.with_recipe() if settings.trust(repo) == "trusted"]


def resolve_repo(orb_type: str = "", snapshot_key: str = "") -> str:
    available = routes()
    cached_repo = snapshots.repo_of(snapshot_key) if snapshot_key and not orb_type else ""
    if orb_type:
        matches = [r["repo"] for r in available if orb_type in (r["orb_type"], r["repo"])]
    elif cached_repo:
        matches = [r["repo"] for r in available if r["repo"] == cached_repo]
    elif current().repo:
        matches = [r["repo"] for r in available if r["repo"] == current().repo]
    else:
        matches = [r["repo"] for r in available]
    if len(matches) != 1:
        raise JobError("select a configured repository using an Amp orb type; see sbx-admin amp routes")
    return matches[0]


def handle_job(env: dict) -> dict:
    job = validate_job(env)
    orb_id, operation = job["ORB_ID"], job["ORB_OPERATION"]
    name = sandbox_name(orb_id)
    with orb_lock(orb_id), core.sandbox_operation(name):
        info = core.find(name)
        if info and ((info.metadata or {}).get("connector") != KIND or info.metadata.get("orbId") != orb_id):
            raise JobError("this sandbox name is already bound to another workload")
        if operation == "delete":
            if info or sandboxes.registered_owner(name):
                sandboxes.delete_locked(name)
            return {"name": name, "state": "deleted"}
        if operation == "pause":
            current = lifecycle.get(name)
            if current and current["active"]:
                raise JobError("application preparation is still running; retry pause")
            checkpoint = None
            if info and job.get("ORB_SNAPSHOT_ID"):
                repo = core.app_of(info).repo
                cached = snapshots.repo_of(job["ORB_SNAPSHOT_ID"])
                if cached and cached != repo:
                    raise JobError("the project snapshot belongs to a different repository")
                if not cached:
                    if core.runtime_state(info) == "paused":
                        core.attach(name, "to snapshot its project for Amp", info=info)
                        info = core.find(name)
                    checkpoint = lambda: snapshots.save(name, job["ORB_SNAPSHOT_ID"], repo)
            if info and core.runtime_state(info) == "running":
                core.pause(name, info, **({"before_pause": checkpoint} if checkpoint else {}))
            return {"name": name, "state": "paused" if info else "missing"}
        if info:
            current = lifecycle.get(name)
            return start_orb(name, info, job, retry_preparation=bool(
                current and not current["active"] and current["phase"] in ("failed", "interrupted", "workspace_ready")))
        repo = resolve_repo(job.get("ORB_TYPE", ""), job.get("ORB_SNAPSHOT_ID", ""))
        owners = settings.current().access.admins
        if not owners:
            raise JobError("finish sbx administrator setup first")
        cache = snapshots.cached(job.get("ORB_SNAPSHOT_ID", ""), repo)
        before = sandboxes.rows().get(name)
        if before and not this_orbs(before["metadata"], orb_id):
            # A registration with no VM that another connector or orb made:
            # never replaced, so a failed spawn cannot delete it either.
            raise JobError("this sandbox name is already bound to another workload")
        try:
            # The app is prepared once the spawn has succeeded: a failed one is
            # never left busy with a preparation its cleanup would wait for.
            sandboxes.create_locked(
                sandboxes.CreateSandbox(name=name, repo=repo, branch=recipes.default_branch(repo), public_keys="",
                                        runtime=cache.runtime if cache else None),
                owners[0], start_app=False, start_from=cache.snapshot_id if cache else None,
                # Registered to an admin, driven by any Amp user: no admin's own values.
                metadata={"connector": KIND, "orbId": orb_id, "personal": "off"})
            info = core.find(name)
            if info is None:
                raise RuntimeError("the core lifecycle did not produce a sandbox")
            result = start_orb(name, info, job, fresh=True)
            sandboxes.restart(name, info, prepare=True)
            return result
        except BootstrapNotStarted as error:
            deleted = discard(name, orb_id, before)
            raise JobError(f"{error}; " + ("the new sandbox was deleted, spawn again" if deleted else
                                           "its sandbox was not deleted: send ORB_OPERATION=delete for this orb, "
                                           "then spawn again")) from None
        except Exception:
            discard(name, orb_id, before)
            raise


class BootstrapNotStarted(JobError):
    """Amp's bootstrap failed before it started Amp in the sandbox."""


def this_orbs(metadata, orb_id: str) -> bool:
    return (metadata or {}).get("connector") == KIND and metadata.get("orbId") == orb_id


def discard(name: str, orb_id: str, before: dict | None = None):
    """A spawn that failed after it began creating the orb's sandbox deletes
    it again, as Amp's delete does: a VM no thread will use must not keep
    running. Only a registration and VM tagged as this orb's are deleted;
    one under its name that something else made (create refused it) stays,
    as does a registration with no VM that this attempt did not make
    (before: the row there when it began). A cleanup that fails is logged; the spawn's own error is what Amp sees.
    True when nothing of this attempt's is left."""
    try:
        info, row = core.find(name), sandboxes.rows().get(name)
        if (info and not this_orbs(info.metadata, orb_id)) or (row and not this_orbs(row["metadata"], orb_id)):
            return False
        if info is None and row is not None and row == before:
            return False
        if info or row:
            sandboxes.delete_locked(name)
        return True
    except Exception as error:
        print(f"amp: {name} was not deleted after its spawn failed ({type(error).__name__}); "
              "ORB_OPERATION=delete removes it", file=sys.stderr, flush=True)
        return False


def start_orb(name: str, info, job: dict, retry_preparation: bool = False, fresh: bool = False) -> dict:
    """The orb's sandbox running, with Amp's bootstrap started in it. A fresh
    one is deleted again when this fails, and its app is started only once
    this succeeds (handle_job)."""
    try:
        sandbox = core.attach(name, "for its Amp thread", info=info, run_app=not (retry_preparation or fresh),
                               ready=True)
    except core.NotReady as error:
        raise JobError(f"{error} (sbx-admin amp build)") from None
    if retry_preparation:
        sandboxes.restart(name, info, prepare=True)
    recipe = sandbox.app
    # Amp's Repository Setup=false expects this path and a fresh Git
    # credential helper. Keep the actual .sbx workdir unchanged.
    root = shlex.quote(recipe.workdir)
    link = (f"mkdir -p {Path(WORKSPACE).parent} && test ! -e {WORKSPACE} -o -L {WORKSPACE} && "
            f"ln -sfnT {root} {WORKSPACE} && " if recipe.workdir != WORKSPACE else "")
    sandbox.run(link + f"git -C {root} remote set-head origin -a", timeout=60)
    sandbox.run(f"cd {root} && mkdir -p .amp/portals && "
                f"! git ls-files --error-unmatch {PREVIEWS} >/dev/null 2>&1 && "
                f"(grep -qxF /{PREVIEWS} .git/info/exclude || printf '\\n/{PREVIEWS}\\n' >> .git/info/exclude)",
                timeout=30)
    sandbox.write(recipe.workdir + "/" + PREVIEWS, json.dumps({"links": [
        {"label": label, "url": url} for label, url in addressing.urls(name, recipe).items()
        if label != "desktop"
    ]}))
    launch = secrets.token_hex(8)   # not secret: tells this launch's bootstrap from an earlier one's
    envs = {"ORB_TOKEN": job["ORB_TOKEN"], "AMP_URL": job.get("AMP_URL") or "https://ampcode.com",
            "BASH_ENV": f"{platform.RUN}/env", "SBX_AMP_LAUNCH": launch}
    helper = f"{platform.RUN}/amp-bootstrap.py"
    sandbox.ship_guest()   # the bootstrap imports it
    sandbox.write(helper, BOOTSTRAP.read_text(), user="user")
    sandbox.spawn(f"python3 {helper}", envs)
    # A runtime may return from spawn before the command has run: a helper
    # that fails before it starts Amp shows here, within seconds.
    try:
        sandbox.sbx.commands.run(f"python3 {helper} --started {launch}", user="user", timeout=STARTED_PROBE)
    except runtime.CommandFailed:
        # The log may hold the token, so it stays in the sandbox; a fresh one is deleted with it.
        where = "" if fresh else f" (see {platform.RUN}/amp-bootstrap.log there)"
        raise BootstrapNotStarted(f"Amp's bootstrap did not start in the sandbox{where}") from None
    return {"name": name, "sandbox_id": info.sandbox_id, "repo": recipe.repo,
            "urls": addressing.urls(name, recipe), "state": "running"}


def provider():
    amp = current()
    token = vault.get(TOKEN)
    if not amp.enabled or not token:
        print("Amp provider is off or has no provider token")
        return
    # The provider's dispatch children load runtime access themselves. Do not
    # give the Amp controller process sbx's platform secret environment.
    env = {**os.environ, "AMP_ORB_PROVIDER_TOKEN": token}
    os.execve(str(CLI), [str(CLI), "orb", "provider", str(DISPATCH)], env)


def build(repo: str | None):
    """Amp's layer is in every template while the connector is enabled."""
    from ... import runtimes, template_builds
    selected = [repo] if repo else [r["repo"] for r in routes()]
    if not selected:
        raise JobError("prepare a repository in sbx before building its Amp template")
    for one in selected:
        if one not in [r["repo"] for r in routes()]:
            raise JobError("connect and prepare the repository in sbx first")
        template_builds.build_now(one if template_builds.own(one) else template_builds.default_for(one),
                                  runtimes.of_repo(one))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("dispatch", help="receive Amp's ORB_* environment")
    sub.add_parser("provider", help="run the outbound controller (systemd)")
    sub.add_parser("list", help="list Amp-owned VMs, including paused ones")
    sub.add_parser("routes", help="show configured repositories and their Amp orb types")
    cache = sub.add_parser("snapshots", help="inspect or remove owned project caches")
    cache.add_argument("operation", choices=["list", "delete"])
    cache.add_argument("key", nargs="?")
    builder = sub.add_parser("build", help="build the templates Amp runs in")
    builder.add_argument("--repo", help="build only this configured repository (default: all)")
    args = parser.parse_args(argv)
    try:
        if args.action == "provider":
            provider()
            return 0
        if args.action == "dispatch":
            result = dispatch(os.environ)
            print(json.dumps({key: result[key] for key in ("name", "state", "urls") if key in result}))
            return 0
        from ... import state
        state.migrate()
        platform.connect()
        if args.action == "routes":
            print(json.dumps(routes()))
        elif args.action == "snapshots":
            if args.operation == "delete":
                if not args.key:
                    raise JobError("snapshot delete needs the key shown by amp snapshots list")
                snapshots.delete(args.key)
            else:
                print(json.dumps(snapshots.records()))
        elif args.action == "build":
            build(args.repo)
        else:
            print(json.dumps([{"orb_id": i.metadata["orbId"], "sandbox_id": i.sandbox_id,
                               "state": i.state} for i in sandboxes_of()]))
        return 0
    except JobError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        # SDK exceptions can contain request details. Provider output is also
        # shown in thread setup logs, so never render the raw exception here.
        print("Amp operation failed; check template/runtime availability and `sbx-admin amp list`. "
              "For dispatch, verify the job and repository routing with sbx-admin amp routes.", file=sys.stderr)
        return 1
