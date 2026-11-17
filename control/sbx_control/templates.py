"""Sandbox templates: what goes into one (a plan) and building it on the runtime.

A template is three layers: the platform layer (the base template), the
extensions' contributions, and the recipe (a repo's .sbx/Dockerfile, its
checkout and .sbx/build when its sbx.yaml asks for a prebuilt template;
else the default layer). The runtime sizes a VM by the template it starts
from, so every template is built at the size of its sandboxes.

A template is named <slug>-<fingerprint[:8]>, so a changed input is a new
template, never one that overwrites what sandboxes use. A repo's checkout
reaches its build as a git bundle under a one-time token, so no GitHub
credential enters the template."""
import base64
import collections
import dataclasses
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import secrets
import shlex
import subprocess
import time
from urllib.parse import urlsplit

from . import contributions, paths, platform, recipes, runtime, settings
from .contributions import Contribution

FILES = pathlib.Path(__file__).parent / "template_files"
NODE_VERSION = "22.22.2"
NOVNC_VERSION = "1.5.0"
WEBSOCAT_VERSION = "1.14.0"
PLAYWRIGHT_VERSION = "1.58.2"
UV_VERSION = "0.9.7"
# The keys of the templates the platform owns; a repo's is its owner/name.
BASE = "sbx-base"
DEFAULT = "default"
SIZED = re.compile(rf"^{DEFAULT}:(\d+)cpu-(\d+)mb-(\d+)mb$")
BUNDLE_TTL = 3600
TOKEN_HEADER = "X-Sbx-Bundle"

# What no reusable snapshot of any sandbox keeps: the desktop browser's
# profile, keyrings, package registry and Docker logins.
PLATFORM = Contribution(
    "platform",
    scrub_paths=("$HOME/.config/chromium", "$HOME/.cache/chromium", "$HOME/.config/google-chrome",
                 "$HOME/.cache/google-chrome", "$HOME/.local/share/keyrings", "$HOME/.npmrc",
                 "$HOME/.docker/config.json"))



def bundles():
    """Where a template build's one-time downloads wait (dist_api serves them)."""
    return paths.state_dir() / "bundles"

def inline(text: str) -> str:
    """A script as a command that runs it, in the build's own steps rather
    than a file upload, which a runtime's builder may not reach."""
    return f"echo {base64.b64encode(text.encode()).decode()} | base64 -d > /tmp/sbx-step && bash /tmp/sbx-step"


def base_steps() -> list:
    """The platform layer as data, in order: (what, value)."""
    apt = ("openssh-server sudo curl ca-certificates gnupg git xz-utils unzip jq ripgrep less vim-tiny iptables "
           "build-essential python3 python3-websockify xvfb openbox x11vnc dbus-x11 xdg-utils fonts-liberation "
           "fonts-noto-color-emoji xterm x11-xserver-utils locales tmux")
    return [
        ("image", "ubuntu:24.04"),
        ("user", "root"),
        ("run", f"apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {apt} "
                "&& apt-get clean && rm -rf /var/lib/apt/lists/*"),
        # noVNC as static files: Ubuntu's novnc package pulls in nodejs.
        ("run", f"curl -fsSL https://github.com/novnc/noVNC/archive/refs/tags/v{NOVNC_VERSION}.tar.gz | tar -xz -C /opt "
                f"&& mv /opt/noVNC-{NOVNC_VERSION} /opt/novnc && ln -s vnc.html /opt/novnc/index.html"),
        ("run", f"curl -fsSL -o /tmp/sbx-websocat https://github.com/vi/websocat/releases/download/v{WEBSOCAT_VERSION}"
                "/websocat.x86_64-unknown-linux-musl && chmod +x /tmp/sbx-websocat && mv -f /tmp/sbx-websocat /usr/local/bin/websocat"),
        # Node for the agents; corepack gives each repo the pnpm or yarn its package.json names.
        ("run", f"curl -fsSL https://nodejs.org/dist/v{NODE_VERSION}/node-v{NODE_VERSION}-linux-x64.tar.xz | tar -xJ -C /opt && "
                f"for bin in node npm npx corepack; do ln -sfn /opt/node-v{NODE_VERSION}-linux-x64/bin/$bin /usr/local/bin/$bin; done "
                "&& corepack enable --install-directory /usr/local/bin"),
        # Chromium for the streamed desktop and for browser tests, shared by
        # every user. Not Google's Chrome .deb: dl.google.com stalls on large
        # downloads from some networks.
        ("env", {"PLAYWRIGHT_BROWSERS_PATH": "/opt/ms-playwright", "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0"}),
        ("run", f"npx -y playwright@{PLAYWRIGHT_VERSION} install --with-deps chromium && chmod -R a+rX /opt/ms-playwright "
                "&& ln -sf \"$(ls -d /opt/ms-playwright/chromium-*/chrome-linux*/chrome | head -1)\" /usr/local/bin/chromium "
                "&& apt-get clean && rm -rf /var/lib/apt/lists/*"),
        # macOS ssh sends LC_* = en_US.UTF-8.
        ("run", "locale-gen en_US.UTF-8 >/dev/null"),
        # Keys only; host keys are generated per sandbox by START. A runtime's
        # own drop-ins may allow empty passwords and root login, and sshd takes
        # the first value it reads; 0- sorts before them if one comes back.
        ("run", "mkdir -p /run/sshd && rm -f /etc/ssh/ssh_host_* /etc/ssh/sshd_config.d/*.conf "
                "&& printf 'PasswordAuthentication no\\nPermitEmptyPasswords no\\nKbdInteractiveAuthentication no\\n"
                "PermitRootLogin no\\nAllowUsers user\\n' > /etc/ssh/sshd_config.d/0-sandbox.conf"),
        ("run", inline((FILES / "resource-control.sh").read_text())),
        ("user", "user"),
        ("workdir", platform.HOME),
        # The sandbox's env reaches SSH sessions through a tmpfs file
        # sbx-control writes (core.write_runtime); read first, before
        # .bashrc's interactive guard.
        ("run", "for f in .bashrc .profile; do "
                f"printf '[ -r {platform.RUN}/env ] && . {platform.RUN}/env\\n' | cat - $f > $f.new && mv $f.new $f; done "
                "&& mkdir -p .ssh && chmod 700 .ssh"),
    ]


# The default template's layer, for repos without a prebuilt template: Python with uv next to Node.
DEFAULT_STEPS = (f"curl -LsSf https://astral.sh/uv/{UV_VERSION}/install.sh | sh && ~/.local/bin/uv --version",)


def _digest(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def default_key(size: dict) -> str:
    if size == recipes.DEFAULT_SIZE:
        return DEFAULT
    return f"{DEFAULT}:{size['cpu']}cpu-{size['memory_mb']}mb-{size['disk_mb']}mb"


def is_default(key: str) -> bool:
    return key == DEFAULT or bool(SIZED.match(key))


def _default_size(key: str) -> dict:
    match = SIZED.match(key)
    if not match:
        return dict(recipes.DEFAULT_SIZE)
    return dict(zip(("cpu", "memory_mb", "disk_mb"), map(int, match.groups())))


def slug(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:40].strip("-") or "template"


def named_for(name: str, keys) -> bool:
    """Whether name is one the platform gives a template of one of keys:
    <slug(key)>-<8 hex digits> (Plan.name), after the installation's prefix
    on a hosted runtime (runtime.template_name)."""
    head, _, digest = runtime.plain_name(name).rpartition("-")
    return bool(re.fullmatch(r"[0-9a-f]{8}", digest)) and head in {slug(key) for key in keys}


@dataclasses.dataclass(frozen=True)
class Built:
    """A template in the runtime, as listing() reports it."""
    name: str
    status: str   # the runtime's build status: building, waiting, ready or error


def listing(kind: str | None = None) -> list:
    """Every template the runtime of kind (None: the default) holds, by name (its alias)."""
    return [Built(name, status) for name, status in runtime.templates(kind)]


def delete(name: str, kind: str | None = None) -> bool:
    """Delete a template from the runtime; False when it has none of that name.
    The runtime may keep its layers while something still reads them (a
    sandbox running from it, a pause snapshot made from it)."""
    return runtime.delete_template(name, kind)


@dataclasses.dataclass(frozen=True)
class Plan:
    key: str                         # BASE, a default key (default_key) or owner/repo
    fingerprint: str
    base: "Plan | None" = None       # what it builds on; None for the base itself
    contributions: tuple = ()
    recipe: recipes.App | None = None  # a repo's prebuilt template; None: the default layer
    size: dict = dataclasses.field(default_factory=lambda: dict(recipes.DEFAULT_SIZE))  # its sandboxes' VM

    @property
    def name(self) -> str:
        return f"{slug(self.key)}-{self.fingerprint[:8]}"


def plan(key: str, extras: list) -> Plan:
    """The template for key (BASE, a default key or a repo with one of its
    own: its recipe's when sbx.yaml asks for one, else the default layer),
    with the extensions' contributions extras."""
    base = Plan(BASE, _digest("platform", base_steps(), platform.START))
    if key == BASE:
        return base
    extras = tuple(c for c in extras if c.template or c.ready)
    if is_default(key):
        size = _default_size(key)
        return Plan(key, _digest(base.fingerprint, contributions.fingerprint(extras), DEFAULT_STEPS, size),
                    base, extras, size=size)
    a = recipes.load(key)
    if not a.prebuilt:  # the default layer, for the template layers of the packages it attaches
        return Plan(key, _digest(base.fingerprint, contributions.fingerprint(extras), DEFAULT_STEPS, a.size),
                    base, extras, size=a.size)
    return Plan(key, _digest(base.fingerprint, contributions.fingerprint(extras), recipe_inputs(a),
                             a.size, settings.current().git_email), base, extras, a, a.size)


def recipe_inputs(a: recipes.App) -> list:
    """What a prebuilt template's build reads from the recipe (recipe_layer):
    .sbx/Dockerfile, .sbx/build and the workdir. The other files (setup,
    start, helpers, configs, checks) run in sandboxes, not in the build, so
    editing them rebuilds nothing; what the build itself runs from the
    checkout is the repo's content, which the nightly rebuild follows."""
    return [a.text("Dockerfile"), a.text("build"), a.workdir]


def base_template() -> runtime.TemplateSpec:
    t = runtime.TemplateSpec()
    for what, value in base_steps():
        if what == "image":
            t = t.from_image(value)
        elif what == "user":
            t = t.set_user(value)
        elif what == "workdir":
            t = t.set_workdir(value)
        elif what == "env":
            t = t.set_envs(value)
        else:
            t = t.run_cmd(value)
    return t


def contribute(t: runtime.TemplateSpec, extras, served: list) -> runtime.TemplateSpec:
    for c in extras:
        layer = c.template
        if not layer:
            continue
        t = t.set_user("root")
        if layer.apt:
            t = t.run_cmd("apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
                          f"{' '.join(layer.apt)} && apt-get clean && rm -rf /var/lib/apt/lists/*")
        for path, f in sorted(layer.files.items()):
            encoded = base64.b64encode(f.content.encode()).decode()
            t = t.run_cmd(f"mkdir -p {shlex.quote(str(pathlib.PurePosixPath(path).parent))} && echo {encoded} | base64 -d "
                          f"> {shlex.quote(path)} && chmod {f.mode:o} {shlex.quote(path)}")
        for artifact in layer.artifacts:
            token, path = serve_bytes(artifact.produce())
            served.append((token, path))
            t = t.run_cmd(f"mkdir -p {shlex.quote(artifact.into)} && {fetch(token)} "
                          f"| tar -xz -C {shlex.quote(artifact.into)}")
        for command in layer.root:
            t = t.run_cmd(command)
        t = t.set_user("user")
        for command in layer.user:
            t = t.run_cmd(command)
    return t.set_user("user")


def recipe_layer(t: runtime.TemplateSpec, a: recipes.App, fetch: str) -> runtime.TemplateSpec:
    """The recipe's part of a prebuilt template; what it reads is recipe_inputs."""
    dockerfile, build = a.text("Dockerfile"), a.text("build")
    if dockerfile and any(line.split()[:1] in (["COPY"], ["ADD"]) for line in dockerfile.upper().splitlines()):
        raise recipes.RecipeError(".sbx/Dockerfile: COPY and ADD are not supported; fetch files with RUN curl or in .sbx/build")
    if dockerfile:
        t = t.dockerfile(dockerfile)  # its FROM is ignored: templates start from the base
    workdir, branch = a.workdir, recipes.default_branch(a.repo)
    t = (
        t.set_user("user")
        .set_workdir(platform.HOME)
        # The image holds no credential: git reaches origin with what the
        # GitHub connection's runtime layer writes. updateInstead lets `sbx
        # push` update a checked-out branch.
        .run_cmd(
            f"{fetch} -o /tmp/repo.bundle && git init -q {workdir} && cd {workdir} "
            f"&& git fetch -q /tmp/repo.bundle HEAD && rm /tmp/repo.bundle && git checkout -q -B {shlex.quote(branch)} FETCH_HEAD "
            f"&& git remote add origin {recipes.remote(a.repo)} "
            "&& git config receive.denyCurrentBranch updateInstead "
            "&& git rev-parse HEAD > ~/.template-commit "
            f"&& git config --global user.name 'sandbox agent' && git config --global user.email '{settings.current().git_email}'"
        )
        .set_workdir(workdir)
    )
    if build:
        t = t.run_cmd(inline(build))
    return t


# The last lines of each template's latest build, for the web UI's repo page.
BUILD_LOGS: dict = {}


def build_log(key: str, kind: str | None = None) -> list:
    """The last lines of key's latest build on the runtime of kind (None: the default)."""
    return list(BUILD_LOGS.get((kind or runtime.default(), key)) or [])


def mask(text: str, hidden) -> str:
    """text without the build's one-time download tokens: the build log, a
    failed build's error and what the journal gets all pass through here."""
    for value in hidden:
        text = text.replace(value, "••••")
    return text


class BuildFailed(Exception):
    """A template build the runtime refused, its message masked."""


def build_logger(key: str, hidden, kind: str | None = None):
    lines = BUILD_LOGS[(kind or runtime.default(), key)] = collections.deque(maxlen=300)

    def log(entry):
        if getattr(entry, "level", "info") == "debug":
            return
        line = f"{time.strftime('%H:%M:%S', time.gmtime())}  {mask(str(getattr(entry, 'message', entry)), hidden)}"
        lines.append(line)
        print(line, flush=True)
    return log


def build(p: Plan, git_dir: pathlib.Path | None = None, *, kind: str | None = None, base: str | None = None):
    """Build the plan as p.name on the runtime of kind (None: the default);
    a repo's needs its mirror (git_dir). base: the name the base template was
    built under (its build row), which it starts from; default: the name planned now."""
    BUILD_LOGS.pop((kind or runtime.default(), p.key), None)
    served = []
    try:
        if p.base is None:
            t = base_template()
        else:
            t = runtime.TemplateSpec().from_template(base or runtime.template_name(p.base.name, kind))
            if p.recipe:
                token, bundle = serve_bundle(git_dir)
                served.append((token, bundle))
                t = recipe_layer(t, p.recipe, fetch(token))
            else:
                for command in DEFAULT_STEPS:
                    t = t.run_cmd(command)
            workdir = p.recipe.workdir if p.recipe else platform.HOME
            t = contribute(t, p.contributions, served)
            for c in p.contributions:
                if c.ready:
                    t = t.run_cmd(c.ready.command)
            t = t.set_workdir(workdir)
        t = t.set_start_cmd(platform.START, platform.NOVNC_PORT)
        hidden = [token for token, _ in served]
        try:
            return runtime.build_template(t, alias=runtime.template_name(p.name, kind), cpu=p.size["cpu"], memory_mb=p.size["memory_mb"],
                                          disk_mb=p.size["disk_mb"], on_log=build_logger(p.key, hidden, kind), kind=kind)
        except Exception as error:
            # The runtime's error repeats the command that failed.
            raise BuildFailed(mask(str(error), hidden)) from None
    finally:
        for _, path in served:
            path.unlink(missing_ok=True)


def fetch(token: str) -> str:
    """The curl that downloads what this control serves a build under token.
    The token travels in a header, so no access log records it. A build
    trusts only public CAs, so a self-signed certificate (--dns none) is
    pinned by its key."""
    command = f"curl -fsSL -H {shlex.quote(f'{TOKEN_HEADER}: {token}')} {shlex.quote(bundle_origin() + '/bundles')}"
    pin = pinned_key()
    return f"{command} {pin}" if pin else command


def pinned_key() -> str:
    """The curl options that trust this server's self-signed certificate by
    its key (--dns none); "" when its certificate is from a public CA."""
    name = settings.current().domain.name
    path = paths.tls() / "letsencrypt/live" / name / "fullchain.pem"
    if not name or not path.exists():
        return ""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    if cert.issuer != cert.subject:
        return ""
    key = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return f"--insecure --pinnedpubkey sha256//{base64.b64encode(hashlib.sha256(key).digest()).decode()}"


def bundle_origin() -> str:
    """An operator may expose this control's bundles on a separate private origin."""
    origin = os.environ.get("SBX_TEMPLATE_BUNDLE_ORIGIN") or settings.current().base_url
    try:
        parsed = urlsplit(origin)
        host = parsed.hostname or ""
        try:
            ipaddress.ip_address(host)
            valid_host = True
        except ValueError:
            valid_host = bool(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host))
        valid = (parsed.scheme in {"http", "https"} and valid_host and parsed.netloc
                 and parsed.username is None and parsed.password is None
                 and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
                 and not any(character.isspace() or ord(character) < 32 for character in origin)
                 and (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("template bundle origin must be an http(s) origin without credentials, path, query or fragment")
    return origin.rstrip("/")


def _bundle_path() -> tuple[str, pathlib.Path]:
    bundle_origin()  # refuse an invalid origin before anything is written
    folder = bundles()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    for old in folder.glob("*.bundle"):
        if time.time() - old.stat().st_mtime > BUNDLE_TTL:
            old.unlink(missing_ok=True)
    token = secrets.token_urlsafe(24)
    return token, folder / f"{token}.bundle"


def serve_bundle(git_dir: pathlib.Path) -> tuple[str, pathlib.Path]:
    """Bundle the default branch where sbx-control serves it (dist_api)
    under a one-time token; returns the token and the file."""
    token, path = _bundle_path()
    subprocess.run(["git", "-C", git_dir, "bundle", "create", path, "HEAD"], check=True, capture_output=True)
    return token, path


def serve_bytes(data: bytes) -> tuple[str, pathlib.Path]:
    """A build artifact served the same way as a bundle."""
    token, path = _bundle_path()
    path.write_bytes(data)
    path.chmod(0o600)
    return token, path
