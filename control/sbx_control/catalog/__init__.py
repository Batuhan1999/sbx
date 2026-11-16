"""Catalog packages: one model, Package, for what the catalog lists, what an
attachment names and what made a sandbox's resource.

A package is a folder with a package.yaml and its scripts, of one of two
kinds: a `connection` (a server or account the installation saves once:
attachments.py) or a `provision` package (a resource per sandbox, usually
made on a connection: resources.py). Packages come from, first match wins:
an admin's edited version (package_versions), the operator's folder
(paths.packages()), and the platform's own (catalog/ in a release). A
trusted repo may carry its own in .sbx/services/<name>/ (repo_package).

  attachments.py  connections and a repo's attachments (one table)
  resources.py    what packages made for sandboxes, and running their scripts
  connections.py  saving and removing connections (in use: refused)"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import importlib.util
import json
import pathlib
import re
import subprocess
import types
import xml.etree.ElementTree as ElementTree

import yaml

from .. import paths, recipes, state

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KINDS = ("connection", "provision")
ACTIONS = ("create", "delete", "list")
HOOKS = ("pause",)
KEYS = {"kind", "name", "title", "description", "docs", "inputs", "outputs", "env", "requires", "runs", "hooks",
        "actions", "runtime", "template", "check", "connect", "identity", "live", "scrub", "timeout", "host_tools",
        "logo"}
TEMPLATE_KEYS = ("apt", "files", "root", "user")
RESERVED = {"connections"}   # a route of its own under /api/catalog
MAX_BYTES = 1 << 20
LOGO_BYTES = 32 << 10        # every listing carries its packages' logos
SVG_ROOT = "{http://www.w3.org/2000/svg}svg"
CHECK_TIMEOUT = 20
REPO_USES = re.compile(r"^\./\.sbx/services/([a-z0-9][a-z0-9_-]*)/?$")
SHIPPED = paths.shipped("catalog")


class PackageError(Exception):
    pass


def svg_document(text: str) -> bool:
    """Whether text is an SVG document whose root a browser shows as an image:
    <svg> in the SVG namespace. A mark needs no entities, so declaring one is
    refused before parsing."""
    if "<!ENTITY" in text:
        return False
    try:
        return ElementTree.fromstring(text).tag == SVG_ROOT
    except (ElementTree.ParseError, ValueError):
        return False


class NoSuchPackage(PackageError):
    """What an attachment or a call names is not a package here."""


class MissingConnection(PackageError):
    """A package needs a connection that is not there (none, several to
    choose from, or one that is gone or disabled)."""


def seconds(value) -> int:
    match = re.fullmatch(r"(\d+)\s*([smh]?)", str(value).strip())
    if not match:
        raise PackageError(f"timeout {value!r}: use seconds or 30s, 5m, 1h")
    return int(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[match[2]]


def _text(value) -> str:
    return "true" if value is True else "false" if value is False else str(value)


@dataclasses.dataclass(frozen=True)
class Package:
    name: str
    kind: str
    title: str
    description: str
    docs: str
    source: str                  # shipped | operator | edited | repo
    files: dict                  # relative path -> text: package.yaml, the scripts, the rest of the folder
    inputs: dict                 # name -> {required, default, secret, agent, help}; scripts get INPUT_<NAME>
    outputs: dict                # name -> {sensitive}
    env: dict                    # NAME -> template with ${outputs.x}
    requires: dict               # slot -> {kind: <connection package>, expose: {input: ENV}}
    runs: dict                   # action -> script; the script gets its action as $SBX_ACTION and argument
    hooks: dict                  # pause -> script, run like runs after the sandbox paused
    actions: dict                # name -> .py file whose function of that name a sandbox may call
    runtime: str                 # .py file whose runtime(context) writes files and env into the sandbox
    template: dict               # what attached repos' templates install: apt, files {path: {content, mode}}, root, user
    check: str                   # shell text run with a connection's values, or a .py file's check(values)
    connect: str                 # .py file whose connect(before, given) settles a connection's inputs
    identity: tuple              # a connection's endpoint inputs: fixed while resources use it
    live: bool                   # attachments reach existing sandboxes when they are prepared
    scrub: tuple                 # paths in the sandbox no reusable snapshot keeps
    timeout: int                 # seconds, per script
    repo: str = ""               # a repo's own package: that repo
    # Commands its scripts or check run on the host: name -> the root shell
    # command that installs it (host.install_tools, at install and upgrade).
    host_tools: dict = dataclasses.field(default_factory=dict)
    logo: str = ""               # an .svg file in the folder: the provider's mark

    def with_defaults(self, given: dict, label: str) -> dict:
        """The inputs its scripts get: the given values over the defaults. A
        package that declares no inputs takes any (static values)."""
        for name in given:
            if not VAR_RE.match(name):
                raise PackageError(f"{label}: input {name} is not a variable name")
            if self.inputs and name not in self.inputs:
                raise PackageError(f"{label}: {self.title} has no input {name} ({', '.join(self.inputs)})")
        out = {k: v["default"] for k, v in self.inputs.items() if "default" in v}
        out.update({k: _text(v) for k, v in given.items() if v != ""})
        missing = [k for k, v in self.inputs.items() if v["required"] and k not in out]
        if missing:
            raise PackageError(f"{label}: needs {', '.join(missing)}")
        return out

    def secret(self, name: str) -> bool:
        return bool(self.inputs.get(name, {}).get("secret"))

    def snapshot(self) -> str:
        """What a resource keeps of the package that made it, for its later
        scripts (delete, hooks): the same version, even if it changes since."""
        return json.dumps({"name": self.name, "files": self.files, "source": self.source, "repo": self.repo})

    @classmethod
    def from_snapshot(cls, text: str) -> "Package":
        found = json.loads(text)
        return parse(found["name"], found["files"], found["source"], found.get("repo", ""))

    def logo_url(self) -> str:
        """The logo as a data: URL, or "". Clients show it as an image, where
        an SVG runs no script and loads nothing."""
        if not self.logo:
            return ""
        return "data:image/svg+xml;base64," + base64.b64encode(self.files[self.logo].encode()).decode()

    def as_dict(self, used_by=()) -> dict:
        return {"name": self.name, "kind": self.kind, "title": self.title, "description": self.description,
                "source": self.source, "inputs": self.inputs, "outputs": self.outputs, "env": self.env,
                "requires": self.requires, "runs": self.runs, "checks": bool(self.check), "docs": self.docs,
                "logo": self.logo_url(), "used_by": list(used_by)}


def _input(name, v) -> dict:
    v = v or {}
    if not VAR_RE.match(str(name)) or not isinstance(v, dict):
        raise PackageError(f"{name} is not an input name")
    out = {"required": bool(v.get("required")), "secret": bool(v.get("secret")), "agent": v.get("agent", True) is not False,
           "help": str(v.get("help") or v.get("description") or "")}
    if "default" in v:
        out["default"] = _text(v["default"])
    return out


def parse(name: str, files: dict, source: str, repo: str = "") -> Package:
    if not NAME_RE.match(name or "") or name in RESERVED:
        raise PackageError(f"{name!r} is not a package name (a-z, 0-9, - and _)")
    if not isinstance(files, dict) or "package.yaml" not in files:
        raise PackageError(f"{name}: no package.yaml")
    for rel, text in files.items():
        path = pathlib.PurePosixPath(rel)
        if rel.startswith("/") or ".." in path.parts or str(path) != rel or rel in ("", "."):
            raise PackageError(f"{name}: {rel!r} is not a path inside its folder")
        if not isinstance(text, str):
            raise PackageError(f"{name}: {rel} is not text")
    if sum(len(text.encode()) for text in files.values()) > MAX_BYTES:
        raise PackageError(f"{name}: its files are over {MAX_BYTES} bytes")
    try:
        spec = yaml.safe_load(files["package.yaml"]) or {}
    except yaml.YAMLError as error:
        raise PackageError(f"{name}: package.yaml: {error}") from error
    if not isinstance(spec, dict):
        raise PackageError(f"{name}: package.yaml is not a mapping")
    unknown = sorted(set(spec) - KEYS)
    if unknown:
        raise PackageError(f"{name}: package.yaml has no {', '.join(unknown)}")
    kind = spec.get("kind")
    if kind not in KINDS:
        raise PackageError(f"{name}: package.yaml kind must be one of {', '.join(KINDS)}")
    for field in ("inputs", "outputs", "runs", "env", "hooks", "actions", "requires"):
        if not isinstance(spec.get(field) or {}, dict):
            raise PackageError(f"{name}: {field} is not a mapping")
    runs, hooks, actions = dict(spec.get("runs") or {}), dict(spec.get("hooks") or {}), dict(spec.get("actions") or {})
    runtime, connect, check_ = (str(spec.get(k) or "") for k in ("runtime", "connect", "check"))
    if kind == "provision" and "create" not in runs:
        raise PackageError(f"{name}: a provision package needs runs: create")
    if kind == "connection" and (runs or hooks or actions or runtime or spec.get("live")):
        raise PackageError(f"{name}: a connection has no scripts, hooks, actions, runtime or live attachments")
    if kind == "provision" and (connect or spec.get("identity")):
        raise PackageError(f"{name}: only a connection has connect or identity")
    for group, allowed in ((runs, ACTIONS), (hooks, HOOKS)):
        for what in group:
            if what not in allowed:
                raise PackageError(f"{name}: {what}: only {', '.join(allowed)}")
    for script in [*runs.values(), *hooks.values(), *actions.values(), runtime, connect]:
        if script and (not isinstance(script, str) or script not in files):
            raise PackageError(f"{name}: package.yaml names {script}, which is not in its folder")
    for script in [*actions.values(), runtime, connect]:
        if script and not script.endswith(".py"):
            raise PackageError(f"{name}: runtime, actions and connect name a .py file")
    if source == "repo" and (actions or runtime or connect or check_.endswith(".py")):
        raise PackageError(f"{name}: a repo's own package has no Python code (runtime, actions, connect, check)")
    if source == "repo" and spec.get("requires"):
        raise PackageError(f"{name}: a repo's own package never sees the installation's connections (requires)")
    requires = {}
    for slot, req in (spec.get("requires") or {}).items():
        if not isinstance(req, dict) or not req.get("kind"):
            raise PackageError(f"{name}: requires.{slot} needs kind: <connection package>")
        requires[slot] = {"kind": str(req["kind"]), "expose": {str(k): str(v) for k, v in (req.get("expose") or {}).items()}}
    template = _template(name, spec.get("template"), kind)
    tools = spec.get("host_tools") or {}
    if not isinstance(tools, dict) or not all(NAME_RE.match(str(k)) and isinstance(v, str) and v.strip()
                                               for k, v in tools.items()):
        raise PackageError(f"{name}: host_tools maps a command's name to the shell that installs it")
    if source == "repo" and tools:
        raise PackageError(f"{name}: a repo's own package installs nothing on the host (host_tools)")
    logo = spec.get("logo") or ""
    if logo and (not isinstance(logo, str) or not logo.endswith(".svg") or logo not in files):
        raise PackageError(f"{name}: logo names an .svg file in its folder")
    if logo and (len(files[logo].encode()) > LOGO_BYTES or not svg_document(files[logo])):
        raise PackageError(f"{name}: {logo} is not an SVG of at most {LOGO_BYTES >> 10} KiB")
    outputs = {}
    for out, v in (spec.get("outputs") or {}).items():
        if not VAR_RE.match(str(out)) or not isinstance(v or {}, dict):
            raise PackageError(f"{name}: output {out} is not a variable name")
        outputs[str(out)] = dict(v or {})
    return Package(name=name, kind=kind, title=str(spec.get("title") or name),
                   description=str(spec.get("description") or "").strip(), docs=str(spec.get("docs") or ""),
                   source=source, files=dict(files), inputs={k: _input(k, v) for k, v in (spec.get("inputs") or {}).items()},
                   outputs=outputs, env={str(k): str(v) for k, v in (spec.get("env") or {}).items()},
                   requires=requires, runs=runs, hooks=hooks, actions=actions, runtime=runtime, template=template,
                   check=check_,
                   connect=connect, identity=tuple(str(f) for f in spec.get("identity") or ()),
                   live=bool(spec.get("live")), scrub=tuple(str(p) for p in spec.get("scrub") or ()),
                   timeout=seconds(spec.get("timeout", "5m")), repo=repo,
                   host_tools={str(k): v.strip() for k, v in tools.items()}, logo=logo)


def _template(name: str, value, kind: str) -> dict:
    """package.yaml's template: as data: apt and commands as lists of text,
    files as absolute path -> {content, mode}."""
    if not value:
        return {}
    if kind != "provision":
        raise PackageError(f"{name}: only a provision package contributes to templates")
    if not isinstance(value, dict) or set(value) - set(TEMPLATE_KEYS):
        raise PackageError(f"{name}: template has {', '.join(TEMPLATE_KEYS)}")
    out = {}
    for field in ("apt", "root", "user"):
        items = value.get(field) or []
        if not isinstance(items, list) or not all(isinstance(i, str) and i.strip() for i in items):
            raise PackageError(f"{name}: template.{field} is a list of text")
        if items:
            out[field] = list(items)
    files = value.get("files") or {}
    if not isinstance(files, dict):
        raise PackageError(f"{name}: template.files maps absolute paths to their content")
    for path, file in files.items():
        file = file if isinstance(file, dict) else {"content": file}
        mode = file.get("mode", 0o644)
        try:
            mode = int(mode, 8) if isinstance(mode, str) else int(mode)
        except ValueError:
            raise PackageError(f"{name}: template.files {path}: mode is octal, like 0755") from None
        if not str(path).startswith("/") or ".." in pathlib.PurePosixPath(str(path)).parts \
                or not isinstance(file.get("content"), str):
            raise PackageError(f"{name}: template.files maps absolute paths to their content")
        out.setdefault("files", {})[str(path)] = {"content": file["content"], "mode": mode}
    return out


def folder_files(folder: pathlib.Path, name: str) -> dict:
    """A package folder's files (symlinks read as the file they point at)."""
    found = [p for p in sorted(folder.rglob("*")) if p.is_file() and "__pycache__" not in p.parts
             and p.name != ".DS_Store" and not p.name.startswith("._")]
    if sum(p.stat().st_size for p in found) > MAX_BYTES:
        raise PackageError(f"{name}: its folder is over {MAX_BYTES} bytes")
    try:
        return {str(p.relative_to(folder)): p.read_text() for p in found}
    except UnicodeDecodeError as error:
        raise PackageError(f"{name}: a file in its folder is not text: {error}") from error


def _folders(root: pathlib.Path) -> list:
    return [p for p in sorted(root.iterdir()) if p.is_dir() and NAME_RE.match(p.name)] if root.is_dir() else []


def shipped() -> list[Package]:
    """The platform's own packages. They are part of the release: one that
    does not parse is an error, not a package to skip."""
    return [parse(p.name, folder_files(p, p.name), "shipped") for p in _folders(SHIPPED)]


def _found() -> tuple[dict, list]:
    """name -> package, first source wins; and the problems of the
    operator's and admins' packages that do not parse (they are not served)."""
    found, problems = {}, []
    with state.db() as conn:
        edited = conn.execute("select v.name, v.files from package_versions v join package_current c "
                              "on c.name = v.name and c.version = v.version").fetchall()
    candidates = [(name, lambda files=files: json.loads(files), "edited") for name, files in edited]
    candidates += [(p.name, lambda p=p: folder_files(p, p.name), "operator") for p in _folders(paths.packages())]
    for name, files, source in candidates:
        if name in found:
            continue
        try:
            found[name] = parse(name, files(), source)
        except PackageError as error:
            problems.append(f"{source} {name}: {error}")
    for package in shipped():
        found.setdefault(package.name, package)
    return found, problems


def listing(kind: str | None = None) -> list[Package]:
    found = sorted(_found()[0].values(), key=lambda p: (KINDS.index(p.kind), p.title.lower()))
    return [p for p in found if kind is None or p.kind == kind]


def problems() -> list[str]:
    return _found()[1]


def host_tools() -> dict[str, str]:
    """Every host tool the catalog's packages declare: name -> install command."""
    found: dict = {}
    for package in listing():
        for tool, command in package.host_tools.items():
            found.setdefault(tool, command)
    return found


def get(name: str) -> Package:
    if not NAME_RE.match(name or ""):
        raise PackageError(f"{name!r} is not a package name")
    found = _found()[0].get(name)
    if found is None:
        raise NoSuchPackage(f"no package {name} in the catalog")
    return found


def repo_package(uses: str, repo: str) -> Package:
    """A trusted repo's own package, ./.sbx/services/<name>, from its recipe."""
    from .. import settings
    match = REPO_USES.match(uses or "")
    if not match:
        raise PackageError(f"{uses!r}: a repo's own package is ./.sbx/services/<name>")
    if settings.trust(repo) != "trusted":
        raise PackageError(f"{uses}: only repos this installation trusts have packages of their own")
    folder = recipes.directory(repo) / "services" / match[1]
    if not (folder / "package.yaml").is_file():
        raise NoSuchPackage(f"{repo} has no .sbx/services/{match[1]}/package.yaml as its recipe was last read")
    return parse(match[1], folder_files(folder, uses), "repo", repo)


def resolve(uses: str, repo: str) -> Package:
    """What an attachment's uses: names: a repo's own package or the catalog's."""
    return repo_package(uses, repo) if (uses or "").startswith("./") else get(uses)


def save(name: str, files: dict, by: str, message: str = "") -> int:
    parse(name, files, "edited")
    with state.db() as conn:
        conn.execute("begin immediate")
        try:
            version = conn.execute("select coalesce(max(version), 0) + 1 from package_versions where name = ?",
                                   (name,)).fetchone()[0]
            conn.execute("insert into package_versions (name, version, files, by, message) values (?, ?, ?, ?, ?)",
                         (name, version, json.dumps(files), by, message or ""))
            conn.execute("insert into package_current (name, version) values (?, ?) on conflict (name) "
                         "do update set version = excluded.version", (name, version))
            conn.execute("commit")
        except BaseException:
            conn.execute("rollback")
            raise
    return version


_code: dict = {}


def code(package: Package, filename: str):
    """A package's Python file, loaded in this process once per content.
    Only installation packages have code (parse refuses a repo's)."""
    text = package.files[filename]
    key = (package.name, filename, hashlib.sha256(text.encode()).hexdigest())
    if key not in _code:
        module = types.ModuleType(f"sbx_package_{re.sub(r'[^a-z0-9_]', '_', package.name)}")
        module.__file__ = f"<package {package.name}/{filename}>"
        exec(compile(text, module.__file__, "exec"), module.__dict__)
        _code[key] = module
    return _code[key]


def check(package: Package, values: dict) -> str:
    """Run the package's check with the values; "" when it passes, else why
    not. A shell check sees the values under their names and as INPUT_*,
    PATH and nothing else; a .py check gets them as a dict."""
    if not package.check:
        return ""
    if package.check.endswith(".py") and package.check in package.files:
        return code(package, package.check).check(dict(values)) or ""
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", **values, **{f"INPUT_{k.upper()}": v for k, v in values.items()}}
    try:
        result = subprocess.run(["bash", "-c", package.check], env=env, capture_output=True, text=True,
                                timeout=CHECK_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"the check took over {CHECK_TIMEOUT}s"
    if result.returncode:
        return (result.stdout.strip() or result.stderr.strip() or f"the check failed ({result.returncode})")[-500:]
    return ""


def resource_names(resource_id: str) -> set:
    """The names a provider may give the resource, by the naming contract of
    the shared provider library (catalog/_lib/sbx_provider.py)."""
    spec = importlib.util.spec_from_file_location("sbx_provider", SHIPPED / "_lib" / "sbx_provider.py")
    lib = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lib)
    return {lib.PREFIX + separator + resource_id.replace("-", separator) for separator in ("-", "_")}


SCHEMA = state.Schema(tables="""
create table if not exists package_versions (
    name text not null, version integer not null, files text not null, by text not null, message text not null,
    at text not null default current_timestamp, primary key (name, version));
create table if not exists package_current (name text primary key, version integer not null);
""")
