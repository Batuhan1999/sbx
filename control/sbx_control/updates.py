"""Newer releases of sbx: what admins are offered, and installing one.

sbx-control looks at the published releases of the settings' updates
repository on GitHub every few hours (watch) and offers admins the newest
one past the installed release (available): never an older one, and none
to an installation that runs no release (a checkout before the first tag).
An admin's Update names that release's tag (request): sbx-control checks
again that it is the newest, then asks the host for the upgrade effect with
the tag, since installing stops sbx-control itself. sbx-apply upgrade runs
install() as root, outside sbx-control: it asks GitHub for that release
(published, not a draft) and downloads its archive and .sha256, checks one
against the other, unpacks it into <state>/upgrades/sbx-<tag>/ and runs its
install.sh, the ordinary upgrade (settings kept, the state backed up first,
running sandboxes left running), logging to LOG.

  GET  /api/updates         admins: the installed release, the newer ones, the last
                            upgrade and its progress
  POST /api/updates         admins: {"tag": ...} install the newest release
  POST /api/updates/check   admins: look at GitHub now
  sbx-admin upgrade [TAG]   the same on the host
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import paths, settings, setup
from .auth import admin_user

API = "https://api.github.com"
CHECK_SECONDS = 6 * 3600      # how often watch looks at the releases
RETRY_SECONDS = 3600          # after a look that failed
LOG = pathlib.Path("/var/log/sbx-upgrade.log")
KEEP_SOURCES = 2              # unpacked releases kept in <state>/upgrades
INSTALL_STEPS = 10            # the steps install.sh announces (==> ...): the progress bar's estimate

TAG = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?")
# What git describe adds to the tag a checkout is past: -<commits>-g<commit>, and -dirty.
PAST_TAG = re.compile(r"(?:-\d+-g[0-9a-f]{4,})?(?:-dirty)?$")
# The section the `breaking` label makes (.github/release.yml), or the same heading by hand.
BREAKING = re.compile(r"^#{1,6}\s*breaking changes?\b", re.I | re.M)


class Refused(Exception):
    """Not the newest release, or nothing to upgrade to."""


class Busy(Refused):
    """An upgrade is asked for or running already."""


class Unavailable(Exception):
    """GitHub did not answer with the releases."""


def key(version: str) -> tuple | None:
    """A sortable key for a release tag, in SemVer's order (v1.2.3-rc.2
    before v1.2.3), or for a checkout past one (git describe:
    v1.2.3-4-g<commit>), that tag's; None for anything else (a checkout
    before the first tag, dev)."""
    found = TAG.fullmatch(PAST_TAG.sub("", version.strip(), count=1))
    if not found:
        return None
    major, minor, patch, pre = found.groups()
    if pre is None:
        return int(major), int(minor), int(patch), 1, ()
    # Numeric identifiers sort numerically and before alphanumeric ones.
    return int(major), int(minor), int(patch), 0, tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part) for part in pre.split("."))


@dataclass(frozen=True)
class Release:
    tag: str
    url: str                  # its page: the notes
    notes: str                # upgrade steps first, then the changes (release.yml)
    prerelease: bool
    published: str = ""       # when, ISO 8601

    @property
    def breaking(self) -> bool:
        return bool(BREAKING.search(self.notes))

    def view(self) -> dict:
        return {"tag": self.tag, "url": self.url, "notes": self.notes, "breaking": self.breaking,
                "prerelease": self.prerelease, "published_at": self.published}


def _github(path: str, **params):
    """GitHub's REST API, unauthenticated: the repository is public."""
    response = httpx.get(f"{API}/repos/{settings.current().updates.repository}{path}", params=params, timeout=20,
                         follow_redirects=True, headers={"accept": "application/vnd.github+json",
                                                         "x-github-api-version": "2022-11-28",
                                                         "user-agent": f"sbx/{setup.version()}"})
    response.raise_for_status()
    return response.json()


def _assets(item: dict) -> dict[str, str] | None:
    """A published release's archive and checksum URLs, by name; None when it
    is a draft, its tag no release's, or either file is missing."""
    tag = item.get("tag_name") or ""
    assets = {a.get("name"): a.get("browser_download_url") or "" for a in item.get("assets") or ()}
    names = (f"sbx-{tag}.tar.gz", f"sbx-{tag}.tar.gz.sha256")
    if item.get("draft") or not TAG.fullmatch(tag) or not all(assets.get(n, "").startswith("https://") for n in names):
        return None
    return {n: assets[n] for n in names}


def fetch() -> list[Release]:
    """The published releases of the settings' repository that carry the
    release archive and its checksum, newest first: every page of them."""
    items: list = []
    for page in range(1, 21):
        found = _github("/releases", per_page=100, page=page)
        items += found
        if len(found) < 100:
            break
    found = [Release(item["tag_name"], item.get("html_url") or "", item.get("body") or "", bool(item.get("prerelease")),
                     item.get("published_at") or "")
             for item in items if _assets(item)]
    return sorted(found, key=lambda release: key(release.tag), reverse=True)


_cache: dict = {"repository": None, "at": 0.0, "checked_at": "", "releases": [], "error": ""}
_cache_lock = threading.Lock()


def refresh(force: bool = False) -> list[Release]:
    """The releases of the repository the settings name: looked at again when
    due (CHECK_SECONDS, RETRY_SECONDS after a failure), the repository
    changed, or force. A forced look that fails raises Unavailable; an
    unforced one keeps what it had."""
    repository = settings.current().updates.repository
    with _cache_lock:
        wait = RETRY_SECONDS if _cache["error"] else CHECK_SECONDS
        if not force and _cache["repository"] == repository and time.monotonic() - _cache["at"] < wait:
            return _cache["releases"]
        kept = _cache["releases"] if _cache["repository"] == repository else []
        try:
            releases = fetch()
        except (httpx.HTTPError, ValueError) as error:
            reason = f"could not read the releases of {repository} from GitHub: {error}"
            _cache.update(repository=repository, at=time.monotonic(), checked_at=_now(), releases=kept, error=reason)
            if force:
                raise Unavailable(reason) from None
            print(f"updates: {reason}", file=sys.stderr, flush=True)
            return kept
        _cache.update(repository=repository, at=time.monotonic(), checked_at=_now(), releases=releases, error="")
        return releases


def watch(stop: threading.Event):
    """sbx-control's loop: the releases, while the check is on; the first look
    a minute after start."""
    while not stop.wait(60):
        if settings.current().updates.check:
            refresh()


def newer(releases: list[Release], installed: str, prereleases: bool) -> list[Release]:
    """The releases past installed, newest first: full releases, and release
    candidates with prereleases; none when installed is no release."""
    base = key(installed)
    if base is None:
        return []
    return [r for r in releases if key(r.tag) > base and (prereleases or not r.prerelease)]


def available() -> dict | None:
    """What to offer: the newest release past the installed one, and every
    release in between (their notes and breaking changes apply too)."""
    s = settings.current().updates
    if not s.check:
        return None
    with _cache_lock:
        known = _cache["releases"] if _cache["repository"] == s.repository else []
    found = newer(known, setup.version(), s.prereleases)
    if not found:
        return None
    return {"latest": found[0].tag, "url": found[0].url, "breaking": any(r.breaking for r in found),
            "releases": [r.view() for r in found]}


def _requested() -> pathlib.Path:
    """The host effect's request (settings.request_effects); this one holds the tag."""
    return paths.run() / "apply" / settings.HostUnit.UPGRADE


def _lock_file() -> pathlib.Path:
    return paths.state_dir() / "upgrade.lock"


def _record_file() -> pathlib.Path:
    return paths.state_dir() / "upgrade.json"


def running() -> bool:
    """An upgrade waits for the host, or install() holds its lock."""
    if _requested().exists():
        return True
    try:
        with open(_lock_file(), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    return False


def last() -> dict | None:
    """The last upgrade as install() records it: tag, from, state (running,
    done, failed), log, error, rollback. A requested one is running; one
    recorded as running whose process is gone failed."""
    try:
        record = json.loads(_record_file().read_text())
    except (OSError, ValueError):
        record = None
    if running():
        if record and record.get("state") == "running":
            return record
        try:
            tag = _requested().read_text().strip()
        except OSError:
            tag = ""
        return {"tag": tag, "from": setup.version(), "state": "running", "log": str(LOG)}
    if record and record.get("state") == "running":
        return {**record, "state": "failed", "error": "the upgrade stopped before it finished"}
    return record


def progress(record: dict) -> dict:
    """An upgrade's steps so far: the download (bytes), then each step
    install.sh announces in the log since the upgrade began; an estimated
    fraction done; the log's last lines."""
    lines = []
    if "log_offset" in record:
        try:
            with open(LOG, errors="replace") as log:
                log.seek(record["log_offset"])
                lines = log.read().splitlines()
        except OSError:
            pass
    state, phase = record.get("state"), record.get("phase", "download")
    received, total = record.get("received", 0), record.get("total", 0)
    now = {"running": "running", "failed": "failed"}.get(state, "done")
    steps = [{"title": f"Download sbx-{record.get('tag')}.tar.gz and check its .sha256",
              "state": now if phase == "download" else "done",
              "detail": f"{received / 1e6:.1f} of {total / 1e6:.1f} MB" if total else ""}]
    announced = [line[4:].strip() for line in lines if line.startswith("==> ")]
    steps += [{"title": title, "state": now if index == len(announced) - 1 else "done", "detail": ""}
              for index, title in enumerate(announced)]
    if state == "done":
        fraction = 1.0
    elif phase == "download":
        fraction = 0.1 * (received / total if total else 0)
    else:
        fraction = 0.1 + 0.9 * min(max(len(announced) - 1, 0) / INSTALL_STEPS, 0.95)
    return {"steps": steps, "progress": round(fraction, 3), "log_tail": lines[-40:]}


def view() -> dict:
    """GET /api/updates: the installed release, what is newer, when GitHub
    was last asked, and the last upgrade while the installation runs one of
    its two releases, with its progress (the rollback only while it runs the
    new one)."""
    installed = setup.version()
    record = last()
    if record and record["state"] != "running":
        if installed not in (record.get("from"), record.get("tag")):
            record = None
        elif installed != record.get("tag"):
            record = {**record, "rollback": ""}
    if record:
        record = {**{k: v for k, v in record.items() if k not in ("log_offset", "received", "total", "phase")},
                  **progress(record)}
    with _cache_lock:
        checked = {"checked_at": _cache["checked_at"], "check_error": _cache["error"]}
    return {"installed": installed, "check": settings.current().updates.check, **checked,
            "available": available(), "upgrade": record}


def request(tag: str) -> Release:
    """Ask the host to install tag, when it is the newest release past the
    installed one, as GitHub says now."""
    if running():
        raise Busy(f"an upgrade is running already; see {LOG}")
    installed = setup.version()
    if key(installed) is None:
        raise Refused(f"this installation runs {installed}, not a release: install one by hand "
                      "(docs/reference.md, Operating the host)")
    s = settings.current().updates
    found = newer(refresh(force=True), installed, s.prereleases)
    if not found:
        raise Refused(f"no release of {s.repository} is newer than {installed}")
    if tag != found[0].tag:
        raise Refused(f"{tag} is not the newest release of {s.repository}: {found[0].tag} is")
    _requested().parent.mkdir(parents=True, exist_ok=True)
    # Whole, or not at all: the path unit starts as soon as the name exists.
    written = _requested().with_name(f".{_requested().name}.new")
    written.write_text(f"{tag}\n")
    written.replace(_requested())
    return found[0]


def install_requested() -> int:
    """sbx-apply upgrade, as root: the requested release, under the lock that
    running() looks at, taken before the request goes so the upgrade never
    looks finished in between."""
    lock_path = _lock_file()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            tag = _requested().read_text().strip()
        except FileNotFoundError:
            print("no upgrade was requested (sbx-admin upgrade TAG)", file=sys.stderr)
            return 2
        _requested().unlink(missing_ok=True)
        return install(tag)


def install(tag: str) -> int:
    """Install release tag over the active one, as root, with the lock held:
    its archive, checked and unpacked, then its install.sh. 0 when that
    succeeded."""
    installed = setup.version()
    if not TAG.fullmatch(tag) or key(installed) is None or key(tag) <= key(installed):
        print(f"not upgrading: {tag!r} is not a release newer than the installed {installed}", file=sys.stderr)
        return 1
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as log:
        start = log.tell()
        record = {"tag": tag, "from": installed, "state": "running", "started_at": _now(), "log": str(LOG),
                  "log_offset": start, "phase": "download"}
        _record(record)
        log.write(f"\nsbx-upgrade: {_now()} {installed} -> {tag}\n")
        log.flush()

        def downloading(received: int, total: int):
            record.update(received=received, total=total)
            _record(record)
        try:
            source = _unpack(tag, log, downloading)
            record["phase"] = "install"
            _record(record)
            code = subprocess.call(["sh", "install.sh"], cwd=source, stdout=log, stderr=subprocess.STDOUT)
            error = f"install.sh exited with {code}" if code else ""
        except (httpx.HTTPError, OSError, ValueError, tarfile.TarError) as failure:
            error = str(failure)
        log.write(f"sbx-upgrade: {_now()} {'failed: ' + error if error else 'upgraded to ' + tag}\n")
    with open(LOG) as log:
        log.seek(start)
        lines = log.read().splitlines()
    # install.sh says why it stopped (die: FAILED: ...) and how to go back (rollback: sh ...).
    said = [line.removeprefix("FAILED: ").strip() for line in lines if line.startswith("FAILED: ")]
    rollback = [line.removeprefix("rollback: ").strip() for line in lines if line.startswith("rollback: sh ")]
    _record({**record, "state": "failed" if error else "done", "finished_at": _now(),
             "error": (said[-1] if said and error.startswith("install.sh") else error),
             "rollback": rollback[-1] if rollback else ""})
    _prune()
    return 1 if error else 0


def _unpack(tag: str, log, downloading=lambda received, total: None) -> pathlib.Path:
    """The published release's archive, as GitHub lists it now, checked
    against its .sha256 and unpacked into <state>/upgrades/sbx-<tag>/;
    downloading(received, total) every half second and at the end."""
    name = f"sbx-{tag}"
    assets = _assets(_github(f"/releases/tags/{tag}"))
    if not assets:
        raise ValueError(f"{tag} is no published release with {name}.tar.gz and its .sha256: nothing was installed")
    folder = paths.state_dir() / "upgrades"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive = folder / f"{name}.tar.gz"
    digest = hashlib.sha256()
    with httpx.stream("GET", assets[f"{name}.tar.gz"], follow_redirects=True, timeout=60) as response, \
            open(archive, "wb") as out:
        response.raise_for_status()
        total, received, said = int(response.headers.get("content-length") or 0), 0, 0.0
        for chunk in response.iter_bytes():
            digest.update(chunk)
            out.write(chunk)
            received += len(chunk)
            if time.monotonic() - said > 0.5:
                downloading(received, total)
                said = time.monotonic()
        downloading(received, total or received)
    checksum = httpx.get(assets[f"{name}.tar.gz.sha256"], follow_redirects=True, timeout=60)
    checksum.raise_for_status()
    expected = (checksum.text.split() or [""])[0].lower()
    if digest.hexdigest() != expected:
        archive.unlink()
        raise ValueError(f"{name}.tar.gz does not match its .sha256 ({digest.hexdigest()}, not {expected}): "
                         "nothing was installed")
    target = folder / name
    shutil.rmtree(target, ignore_errors=True)
    with tarfile.open(archive) as tar:
        if any(member.name != name and not member.name.startswith(f"{name}/") for member in tar.getmembers()):
            raise ValueError(f"{name}.tar.gz holds files outside {name}/: nothing was installed")
        tar.extractall(folder, filter="data")
    archive.unlink()
    if (target / "VERSION").read_text().strip() != tag:
        raise ValueError(f"{name}.tar.gz is not release {tag} (its VERSION): nothing was installed")
    log.write(f"{name}.tar.gz: sha256 {expected}, unpacked into {target}\n")
    log.flush()
    return target


def _record(record: dict):
    temporary = _record_file().with_suffix(".new")
    temporary.write_text(json.dumps(record, indent=2) + "\n")
    temporary.replace(_record_file())


def _prune():
    folder = paths.state_dir() / "upgrades"
    found = sorted((p for p in folder.glob("sbx-v*") if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in found[KEEP_SOURCES:]:
        shutil.rmtree(path, ignore_errors=True)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def follow(tag: str) -> int:
    """sbx-admin upgrade TAG: ask for it as the web app does, then print the
    log until the upgrade is done."""
    start = LOG.stat().st_size if LOG.exists() else 0
    try:
        request(tag)
    except (Refused, Unavailable) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"upgrading to {tag} (log: {LOG}); sbx-control restarts, running sandboxes keep running", flush=True)
    while True:
        done = not running()
        if LOG.exists():
            with open(LOG) as log:
                log.seek(start)
                text = log.read()
                start = log.tell()
            print(text, end="", flush=True)
        if done:
            break
        time.sleep(1)
    record = last() or {}
    if record.get("tag") == tag and record.get("state") == "done":
        return 0
    print(f"the upgrade to {tag} failed: {record.get('error') or 'see the log'}", file=sys.stderr)
    if record.get("rollback") and setup.version() != record.get("from"):
        print(f"to go back to {record.get('from')}: {record['rollback']}", file=sys.stderr)
    return 1


router = APIRouter()


@router.get("/api/updates")
def api_updates(email: str = Depends(admin_user)):
    return view()


@router.post("/api/updates/check")
def api_check(email: str = Depends(admin_user)):
    try:
        refresh(force=True)
    except Unavailable as error:
        raise HTTPException(503, str(error)) from None
    return view()


class UpgradeIn(BaseModel):
    tag: str


@router.post("/api/updates", status_code=202)
def api_upgrade(body: UpgradeIn, email: str = Depends(admin_user)):
    try:
        release = request(body.tag)
    except Busy as error:
        raise HTTPException(409, str(error)) from None
    except Refused as error:
        raise HTTPException(400, str(error)) from None
    except Unavailable as error:
        raise HTTPException(503, str(error)) from None
    print(f"updates: {email} asked for the upgrade to {release.tag}", flush=True)
    return view()
