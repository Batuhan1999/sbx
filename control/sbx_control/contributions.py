"""What an extension puts into a sandbox: one record shared by every kind of
extension (agent connectors, the GitHub connection, catalog packages).

A contribution has two layers. Its template layer is built into the
sandbox's template once (templates.py), so it costs nothing when a sandbox
starts; its runtime layer is written into the sandbox's tmpfs every time the
platform writes the sandbox's runtime (create, prepare, env changes, a cold
boot), so it holds what is per sandbox or secret. Nothing here knows which
extension contributes what."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable, Mapping

from . import platform


@dataclass(frozen=True)
class File:
    content: str
    mode: int = 0o600


@dataclass(frozen=True)
class Check:
    """A command that exits 0 when the contribution is in place. Run at the
    end of a template build (failing it) and when its extension attaches to
    a sandbox (core.attach, verifying only: nothing is installed then)."""
    command: str
    timeout: int = 60


@dataclass(frozen=True)
class Artifact:
    """A file the host produces at build time (a .tar.gz), served to the
    build once and unpacked into a directory of the template."""
    label: str                        # what identifies its content, for the fingerprint
    produce: Callable[[], bytes]
    into: str


@dataclass(frozen=True)
class TemplateLayer:
    apt: tuple[str, ...] = ()
    files: Mapping[str, File] = field(default_factory=dict)     # absolute path -> file, owned by root
    root: tuple[str, ...] = ()        # commands as root, after the packages and files; credential-free
    user: tuple[str, ...] = ()        # commands as the sandbox's user
    artifacts: tuple[Artifact, ...] = ()

    def digest(self) -> dict:
        """What the layer installs, as data: a change marks templates stale."""
        return {"apt": list(self.apt), "root": list(self.root), "user": list(self.user),
                "files": {path: [f.content, f.mode] for path, f in sorted(self.files.items())},
                "artifacts": [[a.label, a.into] for a in self.artifacts]}


@dataclass(frozen=True)
class RuntimeLayer:
    env: Mapping[str, str] = field(default_factory=dict)
    files: Mapping[str, File] = field(default_factory=dict)     # relative to folder(name)
    sources: tuple[str, ...] = ()     # files under platform.RUN that shells source when present
    dirs: tuple[str, ...] = ()        # directories made (and kept) for others to write into
    git_config: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Contribution:
    name: str
    template: TemplateLayer | None = None
    runtime: RuntimeLayer | None = None
    scrub_paths: tuple[str, ...] = ()  # removed before a reusable snapshot of a sandbox is published
    ready: Check | None = None


def folder(name: str) -> str:
    """Where a contribution's runtime files are, in the sandbox's tmpfs. The
    platform replaces the whole of folder("") (EXT) on every runtime write, so
    what an extension no longer contributes is gone too."""
    return f"{EXT}/{name}" if name else EXT


EXT = f"{platform.RUN}/ext"


def fingerprint(contributions) -> str:
    digest = hashlib.sha256()
    for c in contributions:
        if c.template or c.ready:
            digest.update(json.dumps([c.name, c.template.digest() if c.template else None,
                                      c.ready.command if c.ready else None], sort_keys=True).encode())
    return digest.hexdigest()


def merged_runtime(contributions) -> RuntimeLayer:
    """The runtime layers as one: env and git config in order (a later one
    wins), files under each contribution's folder."""
    env, files, sources, dirs, git = {}, {}, [], [], {}
    for c in contributions:
        if not c.runtime:
            continue
        env.update(c.runtime.env)
        files.update({f"{c.name}/{path}": f for path, f in c.runtime.files.items()})
        sources += [s for s in c.runtime.sources if s not in sources]
        dirs += [d for d in c.runtime.dirs if d not in dirs]
        git.update(c.runtime.git_config)
    return RuntimeLayer(env, files, tuple(sources), tuple(dirs), git)
