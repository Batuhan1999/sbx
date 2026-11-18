"""Amp's desktop tools read straight from an OCI registry: a fake registry
serves an index, a manifest and layers; every digest is checked and only
the asked directory is kept."""
import hashlib
import io
import json
import tarfile
import unittest
import urllib.error
import urllib.request

from sbx_control.connectors import amp
from sbx_control.connectors.amp import registry

HOST, REPO = "registry.example.test", "team/orb"


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def layer(entries, gzip_it: bool = True) -> bytes:
    """A layer tarball: (name, content) for files, (name, None) for dirs,
    (name, ("link", target)) for hard links."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz" if gzip_it else "w") as tar:
        for name, content in entries:
            info = tarfile.TarInfo(name)
            if content is None:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            elif isinstance(content, tuple):
                info.type, info.linkname = tarfile.LNKTYPE, content[1]
                tar.addfile(info)
            else:
                info.size, info.mode = len(content), 0o755
                tar.addfile(info, io.BytesIO(content))
    return out.getvalue()


TOOLS = [("opt/amp-wayland", None), ("opt/amp-wayland/bin/labwc", b"\x7fELF labwc"),
         ("opt/amp-wayland/bin/waymote-gateway", b"\x7fELF gateway"),
         ("opt/amp-wayland/libexec/waymote-streamd", b"\x7fELF streamd")]


class FakeRegistry:
    """An opener for registry.Registry: blobs and manifests by path, and
    optionally a bearer challenge before any of them."""

    def __init__(self, layers, *, kind="application/vnd.oci.image.layer.v1.tar+gzip", token=None, platform=None,
                 tamper=None):
        self.blobs, self.requests, self.token = {}, [], token
        descriptors = []
        for data in layers:
            digest = sha(data)
            self.blobs[f"/blobs/{digest}"] = tamper(data) if tamper else data
            descriptors.append({"mediaType": kind, "digest": digest, "size": len(data)})
        manifest = json.dumps({"schemaVersion": 2, "mediaType": registry.MANIFESTS[0],
                               "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                                          "digest": sha(b"{}"), "size": 2},
                               "layers": descriptors}).encode()
        index = json.dumps({"schemaVersion": 2, "mediaType": registry.INDEXES[0], "manifests": [
            {"mediaType": registry.MANIFESTS[0], "digest": sha(b"attestation"), "size": 11,
             "platform": {"os": "unknown", "architecture": "unknown"}},
            {"mediaType": registry.MANIFESTS[0], "digest": sha(manifest), "size": len(manifest),
             "platform": platform or {"os": "linux", "architecture": "amd64"}}]}).encode()
        self.blobs[f"/manifests/{sha(manifest)}"] = manifest
        self.blobs[f"/manifests/{sha(index)}"] = index
        self.image = f"{HOST}/{REPO}@{sha(index)}"

    def open(self, request, timeout=None):
        if isinstance(request, str):   # the token realm
            self.requests.append(("token", request, None))
            return io.BytesIO(json.dumps({"token": "anonymous-fixture-token"}).encode())
        url = request.full_url
        auth = request.unredirected_hdrs.get("Authorization")
        self.requests.append(("get", url, auth))
        if self.token and auth != f"Bearer {self.token}":
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {
                "WWW-Authenticate": f'Bearer realm="https://{HOST}/v2/token",service="{HOST}",'
                                    f'scope="repository:{REPO}:pull"'}, io.BytesIO())
        path = url.removeprefix(f"https://{HOST}/v2/{REPO}")
        if path not in self.blobs:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO())
        return io.BytesIO(self.blobs[path])


def names(data: bytes) -> dict:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {m.name: (tar.extractfile(m).read() if m.isfile() else m.type) for m in tar}


class RegistryTests(unittest.TestCase):
    def extract(self, fake, **kwargs):
        return registry.extract(fake.image, "opt/amp-wayland", limit=kwargs.pop("limit", 1 << 20), opener=fake)

    def test_only_the_directory_is_kept_named_from_its_parent(self):
        fake = FakeRegistry([layer([("etc/passwd", b"root:x:0:0"), ("opt/other/tool", b"other")]),
                             layer(TOOLS), layer([("home/user/.amp/bin/amp", b"cli")])])
        data = self.extract(fake)
        found = names(data)
        self.assertEqual(sorted(found), ["amp-wayland", "amp-wayland/bin/labwc", "amp-wayland/bin/waymote-gateway",
                                         "amp-wayland/libexec/waymote-streamd"])
        self.assertEqual(found["amp-wayland/bin/labwc"], b"\x7fELF labwc")
        self.assertTrue(amp.tools_valid(data))
        self.assertTrue(all(auth is None for _, _, auth in fake.requests))   # a public registry: no token asked

    def test_later_layers_replace_and_delete_what_lower_ones_had(self):
        fake = FakeRegistry([layer(TOOLS + [("opt/amp-wayland/share/old", b"old"), ("opt/amp-wayland/etc/a", b"a")]),
                             layer([("opt/amp-wayland/share/.wh.old", b""),
                                    ("opt/amp-wayland/etc/.wh..wh..opq", b""), ("opt/amp-wayland/etc/b", b"b"),
                                    ("opt/amp-wayland/bin/labwc", b"\x7fELF newer")])])
        found = names(self.extract(fake))
        self.assertNotIn("amp-wayland/share/old", found)
        self.assertNotIn("amp-wayland/etc/a", found)                     # hidden by the opaque marker
        self.assertEqual(found["amp-wayland/etc/b"], b"b")                # the same layer's entry stays
        self.assertEqual(found["amp-wayland/bin/labwc"], b"\x7fELF newer")

    def test_a_whiteout_of_a_parent_removes_the_directory(self):
        fake = FakeRegistry([layer(TOOLS), layer([("opt/.wh.amp-wayland", b"")])])
        with self.assertRaisesRegex(registry.Refused, "has no opt/amp-wayland"):
            self.extract(fake)

    def test_a_layer_that_does_not_match_its_digest_is_refused(self):
        fake = FakeRegistry([layer(TOOLS)], tamper=lambda data: layer(TOOLS + [("opt/amp-wayland/evil", b"x")]))
        with self.assertRaisesRegex(registry.Refused, "does not match its digest"):
            self.extract(fake)

    def test_a_manifest_that_does_not_match_its_digest_is_refused(self):
        fake = FakeRegistry([layer(TOOLS)])
        path = next(p for p in fake.blobs if p.startswith("/manifests/") and b"layers" in fake.blobs[p])
        fake.blobs[path] = fake.blobs[path].replace(b'"schemaVersion": 2', b'"schemaVersion":  2')
        with self.assertRaisesRegex(registry.Refused, "manifest .* does not match its digest"):
            self.extract(fake)

    def test_an_anonymous_token_is_fetched_once_asked_and_never_redirected(self):
        fake = FakeRegistry([layer(TOOLS)], token="anonymous-fixture-token")
        self.assertTrue(amp.tools_valid(self.extract(fake)))
        token = [r for r in fake.requests if r[0] == "token"]
        self.assertEqual(len(token), 1)
        self.assertIn("scope=repository%3Ateam%2Forb%3Apull", token[0][1])
        self.assertIn(f"service={HOST}", token[0][1])

    def test_a_registry_that_gives_no_anonymous_token_is_refused(self):
        class Private(FakeRegistry):
            def open(self, request, timeout=None):
                if isinstance(request, str):
                    raise urllib.error.HTTPError(request, 403, "Forbidden", {}, io.BytesIO())
                return super().open(request, timeout)

        fake = Private([layer(TOOLS)], token="anonymous-fixture-token")
        with self.assertRaisesRegex(registry.Refused, "no anonymous token .* needs a login"):
            self.extract(fake)

    def test_an_image_without_linux_amd64_or_with_an_unreadable_layer_is_refused(self):
        with self.assertRaisesRegex(registry.Refused, "no linux/amd64 image"):
            self.extract(FakeRegistry([layer(TOOLS)], platform={"os": "linux", "architecture": "arm64"}))
        with self.assertRaisesRegex(registry.Refused, "zstd"):
            self.extract(FakeRegistry([layer(TOOLS)], kind="application/vnd.oci.image.layer.v1.tar+zstd"))

    def test_uncompressed_layers_and_hard_links_inside_the_directory(self):
        fake = FakeRegistry([layer(TOOLS + [("opt/amp-wayland/bin/alias", ("link", "opt/amp-wayland/bin/labwc"))],
                                   gzip_it=False)], kind="application/vnd.oci.image.layer.v1.tar")
        found = names(self.extract(fake))
        self.assertEqual(found["amp-wayland/bin/alias"], b"\x7fELF labwc")   # a copy: it may sort first

    def test_a_hard_link_out_of_the_directory_is_refused(self):
        fake = FakeRegistry([layer(TOOLS + [("opt/amp-wayland/bin/sh", ("link", "bin/sh"))])])
        with self.assertRaisesRegex(registry.Refused, "outside opt/amp-wayland"):
            self.extract(fake)

    def test_more_than_the_limit_is_refused(self):
        with self.assertRaisesRegex(registry.Refused, "holds more than 16 bytes"):
            self.extract(FakeRegistry([layer(TOOLS)]), limit=16)

    def test_an_image_reference_without_a_digest_is_refused(self):
        for image in (f"{HOST}/{REPO}:latest", f"{HOST}/{REPO}@sha256:short", "orb@sha256:" + "0" * 64):
            with self.assertRaises(registry.Refused):
                registry.parse(image)
        self.assertEqual(registry.parse(amp.IMAGE)[:2], ("us-central1-docker.pkg.dev", "amp-prod-0/amp-orb/amp-orb"))

    def test_the_default_opener_does_not_forward_the_token_on_a_redirect(self):
        request = urllib.request.Request(f"https://{HOST}/v2/{REPO}/blobs/x")
        request.add_unredirected_header("Authorization", "Bearer t")
        moved = urllib.request.HTTPRedirectHandler().redirect_request(
            request, io.BytesIO(), 307, "Temporary Redirect", {}, "https://storage.example.test/blob")
        self.assertNotIn("Authorization", moved.headers)
        self.assertNotIn("Authorization", moved.unredirected_hdrs)


if __name__ == "__main__":
    unittest.main()
