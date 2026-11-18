"""One directory of a container image, read straight from its registry: the
OCI distribution API over HTTPS, anonymously (a public repository may still
ask for an anonymous bearer token), by a pinned digest. Every manifest and
layer is checked against its sha256 digest before anything of it is kept,
and only the entries under the directory are, as a gzipped tarball named
from its parent (the shape `tar -C /opt -czf - amp-wayland` gives).

No runtime is involved, so a host whose only runtime builds no templates
from images (Boat) gets the files too."""
from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import posixpath
import re
import tarfile
import urllib.error
import urllib.parse
import urllib.request

INDEXES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")
MANIFESTS = ("application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json")
GZIP_LAYERS = ("application/vnd.oci.image.layer.v1.tar+gzip", "application/vnd.docker.image.rootfs.diff.tar.gzip")
PLAIN_LAYERS = ("application/vnd.oci.image.layer.v1.tar",)
PLATFORM = {"os": "linux", "architecture": "amd64"}   # what templates run (READY reads x86_64 libraries)
TIMEOUT = 60                  # seconds without a byte before a request fails
MANIFEST_LIMIT = 4 << 20      # bytes of a manifest or index
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
WHITEOUT, OPAQUE = ".wh.", ".wh..wh..opq"


class Refused(RuntimeError):
    """The registry or the image is not what a verified extraction needs."""


def parse(image: str) -> tuple[str, str, str]:
    """host/repository@sha256:<hex> as (host, repository, digest)."""
    name, _, digest = image.partition("@")
    host, _, repository = name.partition("/")
    if not DIGEST.fullmatch(digest) or "." not in host or not repository:
        raise Refused(f"{image} is not host/repository@sha256:<digest>")
    return host, repository.split(":")[0], digest


class Registry:
    """The repository's /v2 API, with an anonymous token once it asks for one."""

    def __init__(self, host: str, repository: str, opener=None):
        self.base = f"https://{host}/v2/{repository}"
        self.opener = opener or urllib.request.build_opener()
        self.token: str | None = None

    def open(self, path: str, accept: tuple[str, ...] = ()):
        for attempt in (0, 1):
            request = urllib.request.Request(self.base + path, headers={"Accept": ", ".join(accept)} if accept else {})
            if self.token:   # never sent on to where a blob redirects (its storage)
                request.add_unredirected_header("Authorization", f"Bearer {self.token}")
            try:
                return self.opener.open(request, timeout=TIMEOUT)
            except urllib.error.HTTPError as error:
                challenge = error.headers.get("WWW-Authenticate", "") if error.headers else ""
                error.close()
                if error.code != 401 or attempt or not challenge.lower().startswith("bearer "):
                    raise Refused(f"{path.split('/')[1]}: the registry answered HTTP {error.code}") from None
                self.token = self._anonymous(challenge)
        raise AssertionError("unreachable")

    def _anonymous(self, challenge: str) -> str:
        fields = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = fields.pop("realm", "")
        if urllib.parse.urlsplit(realm).scheme != "https":
            raise Refused("the registry asks for a token from a realm that is not HTTPS")
        query = urllib.parse.urlencode({k: v for k, v in fields.items() if k in ("service", "scope")})
        try:
            with self.opener.open(realm + ("&" if "?" in realm else "?") + query, timeout=TIMEOUT) as response:
                answer = json.loads(response.read(MANIFEST_LIMIT))
        except urllib.error.HTTPError as error:
            error.close()
            raise Refused(f"the registry gives no anonymous token (HTTP {error.code}); the image needs a login") from None
        token = answer.get("token") or answer.get("access_token") if isinstance(answer, dict) else None
        if not token:
            raise Refused("the registry's token answer holds no token")
        return token

    def manifest(self, digest: str, accept: tuple[str, ...]) -> dict:
        with self.open(f"/manifests/{digest}", accept) as response:
            data = response.read(MANIFEST_LIMIT + 1)
        if len(data) > MANIFEST_LIMIT:
            raise Refused(f"manifest {digest} is larger than {MANIFEST_LIMIT} bytes")
        if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
            raise Refused(f"manifest {digest} does not match its digest")
        return json.loads(data)


class _Verified(io.RawIOBase):
    """A blob's bytes as they arrive, hashed on the way through."""

    def __init__(self, response):
        self.response, self.hash = response, hashlib.sha256()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        data = self.response.read(len(buffer))
        self.hash.update(data)
        buffer[:len(data)] = data
        return len(data)

    def finish(self, digest: str):
        """Read to the blob's end; Refused unless all of it has the digest."""
        while self.readinto(bytearray(1 << 20)):
            pass
        if "sha256:" + self.hash.hexdigest() != digest:
            raise Refused(f"layer {digest} does not match its digest")


def _inside(path: str, root: str) -> bool:
    """path is root or below it; the image's root ("") holds everything."""
    return root == "" or path == root or path.startswith(root + "/")


def _normal(name: str) -> str:
    path = posixpath.normpath("/" + name).lstrip("/")
    return "" if path == "." else path


def _layer(member_stream, prefix: str) -> tuple[list, dict]:
    """What one layer changes under prefix: (removed paths, kept entries)."""
    removed, kept = [], {}
    for member in member_stream:
        path = _normal(member.name)
        folder, base = posixpath.split(path)
        if base == OPAQUE:
            # Everything below folder from lower layers goes; the folder stays.
            if _inside(folder, prefix) or _inside(prefix, folder):
                removed.append((folder, True))
            continue
        if base.startswith(WHITEOUT):
            gone = posixpath.join(folder, base[len(WHITEOUT):])
            if _inside(gone, prefix) or _inside(prefix, gone):
                removed.append((gone, False))
            continue
        if not _inside(path, prefix):
            continue
        if member.islnk():
            target = _normal(member.linkname)
            if not _inside(target, prefix):
                raise Refused(f"{path} is a hard link to {target}, outside {prefix}")
            member.linkname = target
        data = member_stream.extractfile(member).read() if member.isfile() else b""
        member.name = path
        kept[path] = (member, data)
    return removed, kept


def _payload(tree: dict, member, data: bytes) -> int:
    """The bytes an entry writes: a hard link becomes a copy of its target."""
    if member.islnk() and member.linkname in tree:
        return len(tree[member.linkname][1])
    return len(data)


def extract(image: str, directory: str, *, limit: int, opener=None) -> bytes:
    """The gzipped tarball of directory (a path in the image, such as
    opt/amp-wayland) in image, entries named from its parent. Refused on a
    digest that does not match, a layer type it can't read, or more than
    limit bytes of files."""
    host, repository, digest = parse(image)
    prefix = _normal(directory)
    registry = Registry(host, repository, opener)
    top = registry.manifest(digest, INDEXES + MANIFESTS)
    if top.get("mediaType") in INDEXES or "manifests" in top:
        chosen = next((m for m in top.get("manifests", ())
                       if all((m.get("platform") or {}).get(k) == v for k, v in PLATFORM.items())), None)
        if chosen is None or not DIGEST.fullmatch(chosen.get("digest", "")):
            raise Refused(f"{image} has no linux/amd64 image")
        top = registry.manifest(chosen["digest"], MANIFESTS)
    tree: dict = {}
    for layer in top.get("layers", ()):
        kind, blob = layer.get("mediaType", ""), layer.get("digest", "")
        if not DIGEST.fullmatch(blob) or kind not in GZIP_LAYERS + PLAIN_LAYERS:
            raise Refused(f"layer {blob or '?'} is {kind or 'of no type'}, not a tar or gzipped tar")
        with registry.open(f"/blobs/{blob}") as response:
            raw = _Verified(response)
            stream = gzip.GzipFile(fileobj=raw) if kind in GZIP_LAYERS else raw
            try:
                with tarfile.open(fileobj=stream, mode="r|") as tar:
                    removed, kept = _layer(tar, prefix)
                while stream.read(1 << 20):   # past tar's end-of-archive marker, through gzip's trailer
                    pass
            except (OSError, EOFError, tarfile.TarError) as error:
                raise Refused(f"layer {blob} is not a readable tar: {error}") from None
            raw.finish(blob)
        for path, opaque in removed:   # a layer hides lower layers' entries, never its own
            for old in [p for p in tree if _inside(p, path) and not (opaque and p == path)]:
                del tree[old]
        for path, (member, _) in kept.items():   # a file or link in place of a lower layer's folder
            if not member.isdir():
                for old in [p for p in tree if p.startswith(path + "/")]:
                    del tree[old]
        tree.update(kept)
        if sum(_payload(tree, member, data) for member, data in tree.values()) > limit:
            raise Refused(f"{directory} in {image} holds more than {limit} bytes")
    if not tree:
        raise Refused(f"{image} has no {directory}")
    parent = posixpath.dirname(prefix)
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for path in sorted(tree):
            member, data = tree[path]
            if member.islnk():   # a copy of its target: sorted, a link could come before what it names
                if member.linkname not in tree or not tree[member.linkname][0].isfile():
                    raise Refused(f"{path} is a hard link to {member.linkname}, which the image does not have")
                data = tree[member.linkname][1]
                member = copy.copy(member)
                member.type, member.linkname, member.size = tarfile.REGTYPE, "", len(data)
            member.name = posixpath.relpath(path, parent) if parent else path
            # A pax path or linkpath would win over the new name.
            member.pax_headers = {k: v for k, v in member.pax_headers.items() if k not in ("path", "linkpath")}
            tar.addfile(member, io.BytesIO(data) if member.isfile() else None)
    return out.getvalue()
