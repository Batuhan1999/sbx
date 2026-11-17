"""A repository's recipe: its .sbx/ folder, with sbx.yaml describing how the
platform runs the repo, and the scripts and files beside it. The platform
reads the operator's copy, else one imported from the repo's default branch."""
import dataclasses
import fcntl
import functools
import hashlib
import os
import re
from pathlib import Path, PurePosixPath
import shlex
import shutil
import subprocess
import tempfile
from typing import Callable

import yaml

from . import paths, platform, runtime, settings

MAX_FILES = 128
MAX_BYTES = 1024 * 1024
TOO_BIG = "keep the recipe below 128 files and 1 MiB; keep dependencies outside .sbx/"
NOT_REGULAR = "recipes must contain regular files inside .sbx/ (no symlinks or submodules)"
# Scripts the platform runs by name; also any .sh helper they call.
SCRIPTS = ("setup", "start", "build")
# A repo without .sbx/: the agent starts the app itself.
_NO_RECIPE = {"previews": {"": 3000}, "health": []}
# The VM a sandbox gets when its recipe names no size (App.size).
DEFAULT_SIZE = {"cpu": 2, "memory_mb": 4096, "disk_mb": 8192}
REPO_URL = re.compile(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?$",
                      re.IGNORECASE)


def mirrors() -> Path:
    """Bare mirrors of the repos' default branches."""
    return paths.state_dir() / "repos"


def remote(repo: str) -> str:
    """The repo's origin in a sandbox. The GitHub connection's runtime layer
    makes git reach it (a credential helper, or a deploy key over SSH)."""
    return f"https://github.com/{repo}.git"


def mirror(repo: str, access, *, timeout: int = 300) -> Path:
    """A bare mirror of the repo's branches on this host, cloned or fetched
    through access: a context giving git's (url, env) for the repo
    (extensions.git_access)."""
    path = mirrors() / f"{repo}.git"
    with access as (url, env):
        def git(*args):
            subprocess.run(["git", *args], env=env, check=True, timeout=timeout)
        if path.exists():
            git("-C", path, "remote", "set-url", "origin", url)
            git("-C", path, "fetch", "-q", "--prune", "origin", "+refs/heads/*:refs/heads/*")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            git("clone", "-q", "--mirror", url, path)
    return path


def repo_of(url: str):
    match = REPO_URL.match(url or "")
    return match.group(1).lower() if match else None


class RecipeError(ValueError):
    pass


def operator_dir(repo: str) -> Path:
    """The operator's copy (sbx-admin recipe apply), which overrides the repo's."""
    return paths.apps() / repo / ".sbx"


def imported_dir(repo: str) -> Path:
    """The copy imported from the default branch (repository setup, a template build)."""
    return paths.state_dir() / "apps" / repo / ".sbx"


def directory(repo: str) -> Path:
    """The recipe as the platform reads it: the operator's copy, else the
    imported one. Either may be missing."""
    return next((d for d in (operator_dir(repo), imported_dir(repo)) if (d / "sbx.yaml").exists()), operator_dir(repo))


def with_recipe() -> list:
    """The repos whose .sbx/ the server holds (the operator's or an imported
    copy). Every repo the installation knows: views.known_repos."""
    found = set()
    for root in (paths.apps(), paths.state_dir() / "apps"):
        found |= {f"{p.parents[2].name}/{p.parents[1].name}" for p in root.glob("*/*/.sbx/sbx.yaml")}
    return sorted(found)


def parse_previews(values: dict) -> dict:
    """Previews by host suffix ("" for the main one, -<name> for the rest),
    each with a port, an access (team: anyone signed in; owner: the
    sandbox's owner), a path its link adds, and whether it gets a link."""
    out = {}
    for name, value in (values or {}).items():
        p = {"port": value} if isinstance(value, int) else dict(value)
        p.setdefault("access", "team")
        if p["access"] not in ("team", "owner"):
            raise RecipeError(f"preview {name or '(main)'}: access must be team or owner")
        out["-" + name if name else ""] = {"path": "", "link": True, **p, "port": int(p["port"])}
    return out


@dataclasses.dataclass(frozen=True)
class App:
    repo: str
    dir: Path
    spec: dict
    has_recipe: bool = True

    @property
    def operator(self) -> bool:
        """Whether this is the operator's copy: then its scripts run as
        shipped, never a checkout's .sbx/."""
        return self.dir == operator_dir(self.repo)

    @property
    def prebuilt(self) -> bool:
        """Whether sbx.yaml asks for a template of the repo's own (`template:`,
        whatever its value: the platform names templates); else its sandboxes
        start from the default one and clone the repo."""
        return self.has_recipe and bool(self.spec.get("template"))

    @property
    def workdir(self) -> str:
        return self.spec.get("workdir") or f"{platform.HOME}/{self.repo.rpartition('/')[2]}"

    @property
    def health(self) -> list:
        return self.spec.get("health") or []

    @property
    def resume_mode(self) -> str:
        lifecycle = self.spec.get("lifecycle") or {}
        if not isinstance(lifecycle, dict) or lifecycle.get("resume", "preserve") not in ("preserve", "restart"):
            raise RecipeError("lifecycle.resume must be preserve or restart")
        return lifecycle.get("resume", "preserve")

    @property
    def size(self) -> dict:
        """The VM every sandbox of the app gets, {cpu, memory_mb, disk_mb}:
        build_resources, else resources, over DEFAULT_SIZE. The runtime sizes
        a VM by its template, so the platform builds each template at the
        size of the sandboxes that start from it (templates.plan)."""
        given = self.spec.get("build_resources") or self.spec.get("resources") or {}
        size = {key: given.get(key, value) for key, value in DEFAULT_SIZE.items()}
        for key, value in size.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise RecipeError(f"resources.{key} must be a positive integer")
        return size

    @property
    def swap_mb(self) -> int | None:
        swap = (self.spec.get("resources") or {}).get("swap_mb")
        if swap is not None and (isinstance(swap, bool) or not isinstance(swap, int) or not 256 <= swap <= 65536):
            raise RecipeError("resources.swap_mb must be an integer between 256 and 65536 MiB")
        return swap

    def previews(self) -> dict:
        """The previews sbx.yaml declares (addressing.previews adds the installation's)."""
        return parse_previews(self.spec.get("previews") or {})

    def text(self, name: str) -> str:
        path = self.dir / name
        return path.read_text() if self.has_recipe and path.is_file() else ""


def load(repo: str | None = None) -> App:
    repo = (repo or settings.current().default_repo).lower()
    path = directory(repo)
    file = path / "sbx.yaml"
    stamp = file.stat() if file.exists() else None
    return _read(repo, path, (stamp.st_ino, stamp.st_mtime_ns, stamp.st_size) if stamp else None)


@functools.lru_cache(maxsize=128)
def _read(repo, path, identity):
    if identity is None:
        return App(repo, path, _NO_RECIPE, has_recipe=False)
    return App(repo, path, yaml.safe_load((path / "sbx.yaml").read_text()))


SERVICE_FIELDS = {"uses", "with", "connections"}


def validate(repo: str, source: Path) -> dict:
    spec = yaml.safe_load((source / "sbx.yaml").read_text())
    if not isinstance(spec, dict) or not spec:
        raise RecipeError("sbx.yaml must be a nonempty mapping")
    for field in ("services", "env", "processes", "previews", "resources", "build_resources", "lifecycle"):
        if field in spec and not isinstance(spec[field], dict):
            raise RecipeError(f"{field} must be a mapping")
    if "health" in spec and not isinstance(spec["health"], list):
        raise RecipeError("health must be a list of URLs")
    for key, use in (spec.get("services") or {}).items():
        if not isinstance(use, dict) or not isinstance(use.get("uses"), str):
            raise RecipeError("each service needs a uses package")
        if unknown := sorted(set(use) - SERVICE_FIELDS):
            raise RecipeError(f"services.{key}: unknown {', '.join(unknown)}; a service takes uses, with and "
                              "connections: (the connections it uses, by kind)")
    if "secrets" in spec and not isinstance(spec["secrets"], (dict, list)):
        raise RecipeError("secrets must be a list or mapping")
    parsed = App(repo, source, spec)
    parsed.previews()
    parsed.resume_mode
    parsed.swap_mb
    # Each runtime's own limits refuse a create there (runtimes.choose); a
    # recipe is wrong only when no enabled runtime can run it.
    if runtime.enabled() and all(runtime.beyond_limits(parsed.size, kind) for kind in runtime.enabled()):
        raise RecipeError(runtime.beyond_limits(parsed.size))
    return spec


@dataclasses.dataclass(frozen=True)
class Entry:
    path: str                    # relative to .sbx/
    size: int
    read: Callable[[], bytes]
    executable: bool = False     # the source's own mode
    regular: bool = True         # False: a symlink or submodule


def ignored(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return "__pycache__" in parts or parts[-1] == ".DS_Store" or parts[-1].startswith("._")


def executable(entry: Entry) -> bool:
    return entry.executable or entry.path in SCRIPTS or entry.path.endswith(".sh")


def checked(entries) -> list:
    kept = [e for e in entries if not ignored(e.path)]
    for e in kept:
        parts = PurePosixPath(e.path).parts
        if not e.regular or e.path.startswith("/") or "\\" in e.path or any(p in ("", ".", "..") for p in parts) \
                or any(ord(c) < 32 for c in e.path):
            raise RecipeError(NOT_REGULAR)
    if len(kept) > MAX_FILES or sum(e.size for e in kept) > MAX_BYTES:
        raise RecipeError(TOO_BIG)
    return kept


def walk(source: Path) -> list:
    """A folder on this host."""
    out = []
    for path in sorted(source.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        regular = path.is_file() and not path.is_symlink()
        out.append(Entry(str(path.relative_to(source).as_posix()), path.stat().st_size if regular else 0,
                         path.read_bytes, os.access(path, os.X_OK) if regular else False, regular))
    return out


def from_mirror(git_dir: Path) -> list:
    """The default branch's .sbx/ in a bare mirror."""
    tree = subprocess.run(["git", "-C", git_dir, "ls-tree", "-r", "-l", "-z", "HEAD", "--", ".sbx/"],
                          capture_output=True, text=True, check=True).stdout
    out = []
    for item in filter(None, tree.split("\0")):
        meta, _, path = item.partition("\t")
        mode, kind, blob, length = meta.split()
        regular = kind == "blob" and mode in ("100644", "100755")
        out.append(Entry(path.removeprefix(".sbx/"), int(length) if regular else 0,
                         lambda blob=blob: subprocess.run(["git", "-C", git_dir, "cat-file", "blob", blob],
                                                          capture_output=True, check=True).stdout,
                         mode == "100755", regular))
    return out


def from_proposal(files: dict) -> list:
    """Files an agent proposes, by their path in the repo (.sbx/...)."""
    if any(not path.startswith(".sbx/") for path in files):
        raise RecipeError(NOT_REGULAR)
    return [Entry(path.removeprefix(".sbx/"), len(text.encode()), text.encode) for path, text in files.items()]


def install(repo: str, source, root: Path) -> int:
    """Install a complete recipe (a folder, or entries) as <root>/<repo>/.sbx,
    validated before it replaces the one there."""
    entries = checked(walk(source) if isinstance(source, Path) else source)
    if not any(e.path == "sbx.yaml" for e in entries):
        raise RecipeError("the recipe has no sbx.yaml")
    parent = root / repo
    parent.mkdir(parents=True, exist_ok=True)
    target, old = parent / ".sbx", parent / ".recipe-previous"
    with (parent / ".recipe.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        staged = Path(tempfile.mkdtemp(prefix=".recipe-", dir=parent))
        try:
            for entry in entries:
                destination = staged / entry.path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(entry.read())
                destination.chmod(0o700 if executable(entry) else 0o600)
            validate(repo, staged)
            shutil.rmtree(old, ignore_errors=True)
            if target.exists():
                target.rename(old)
            try:
                staged.rename(target)
            except OSError:
                if old.exists():
                    old.rename(target)
                raise
            shutil.rmtree(old, ignore_errors=True)
        finally:
            shutil.rmtree(staged, ignore_errors=True)
    return len(entries)


def remove(repo: str) -> bool:
    """Remove the operator's copy (sbx recipe apply); False when there is none.
    The platform then reads the copy imported from the default branch, if any."""
    parent = operator_dir(repo).parent
    if not (parent / ".sbx").exists():
        return False
    with (parent / ".recipe.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        shutil.rmtree(parent / ".sbx")
        shutil.rmtree(parent / ".recipe-previous", ignore_errors=True)
    shutil.rmtree(parent, ignore_errors=True)
    if not any(parent.parent.iterdir()):
        parent.parent.rmdir()
    return True


def import_from(repo: str, entries: list) -> bool:
    """Keep the default branch's recipe as the imported copy; False when the
    branch has none."""
    if not any(e.path == "sbx.yaml" for e in entries):
        return False
    install(repo, entries, paths.state_dir() / "apps")
    return True


def ship(sbx, a: App, target: str):
    """Put the complete recipe into the sandbox at target, replacing what a
    retry left there."""
    entries = checked(walk(a.dir))
    run = lambda command: sbx.commands.run(command, timeout=60)
    stage = run(f"mktemp -d {target}.XXXXXX").stdout.strip()
    try:
        for entry in entries:
            path = f"{stage}/{entry.path}"
            run(f"mkdir -p {shlex.quote(str(PurePosixPath(path).parent))}")
            sbx.files.write(path, entry.read())
            run(f"chmod {'700' if executable(entry) else '600'} {shlex.quote(path)}")
        run(f"rm -rf {target}.old; [ ! -d {target} ] || mv {target} {target}.old; "
            f"mv {shlex.quote(stage)} {target} && rm -rf {target}.old")
    finally:
        run(f"rm -rf {shlex.quote(stage)}")


def fingerprint(a: App) -> str:
    """sha256 over the recipe's files, their paths and executable bits; "" without one."""
    if not a.has_recipe:
        return ""
    digest = hashlib.sha256()
    for entry in checked(walk(a.dir)):
        digest.update(f"{entry.path}\0{int(executable(entry))}\0{entry.size}\0".encode())
        digest.update(entry.read())
    return digest.hexdigest()


_reported: dict = {}  # repo -> default branch GitHub reported


def default_branch(repo: str | None, reported: str | None = None) -> str:
    """The repo's default branch: what GitHub reported (passed in, or
    earlier), else its mirror's HEAD, else what its remote says its HEAD is
    (asked once; a repo on the default template has no mirror), else main."""
    repo = (repo or settings.current().default_repo).lower()
    if reported:
        _reported[repo] = reported
    if repo in _reported:
        return _reported[repo]
    head = subprocess.run(["git", "-C", mirrors() / f"{repo}.git", "symbolic-ref", "--short", "HEAD"],
                          capture_output=True, text=True) if (mirrors() / f"{repo}.git").is_dir() else None
    if head and head.returncode == 0 and head.stdout.strip():
        return head.stdout.strip()
    found = remote_head(repo)
    if found:
        _reported[repo] = found
    return found or "main"


def remote_head(repo: str) -> str | None:
    """The branch the repo's remote HEAD names (git ls-remote --symref), as
    git on this host reaches it; None when it cannot be asked."""
    from . import extensions
    try:
        with extensions.git_access(repo) as (url, env):
            out = subprocess.run(["git", "ls-remote", "--symref", url, "HEAD"], env=env, capture_output=True,
                                 text=True, timeout=30).stdout
    except (LookupError, OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"^ref: refs/heads/(\S+)\tHEAD$", out, re.MULTILINE)
    return match.group(1) if match else None
