"""Newer releases (updates.py): their order, what admins are offered and may
install through the API, and the host's upgrade effect, with GitHub faked at
httpx and a release archive whose install.sh records where it ran."""
import contextlib
import fcntl
import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from support import Installation, Server

REPO = "Batuhan1999/sbx"


DOWNLOADS = f"https://github.com/{REPO}/releases/download"


def release(tag, notes="", *, draft=False, prerelease=False, assets=True):
    names = [f"sbx-{tag}.tar.gz", f"sbx-{tag}.tar.gz.sha256"] if assets else []
    return {"tag_name": tag, "draft": draft, "prerelease": prerelease, "body": notes,
            "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
            "assets": [{"name": n, "browser_download_url": f"{DOWNLOADS}/{tag}/{n}"} for n in names]}


# Newest first by date, as GitHub lists them; a patch of an older line came last.
RELEASES = [release("v0.1.5", "* Backported fix"),
            release("v0.4.0-rc.1", "* Candidate", prerelease=True),
            release("v0.3.0", "* Small fix"),
            release("v0.2.0", "Move the setting first.\n\n### Breaking changes\n* Renamed a setting"),
            release("v0.9.0", draft=True), release("v0.8.0", assets=False), release("nightly"),
            release("v0.1.0", "* First")]


def archive(tag, install="echo installing\n"):
    """A release archive as release.yml builds it: sbx-<tag>/ with VERSION."""
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        for name, text in ((f"sbx-{tag}/VERSION", f"{tag}\n"), (f"sbx-{tag}/install.sh", install)):
            info = tarfile.TarInfo(name)
            info.size = len(text.encode())
            tar.addfile(info, io.BytesIO(text.encode()))
    return data.getvalue()


class FakeGitHub:
    """httpx.get and httpx.stream for the releases API and the downloads."""

    def __init__(self, releases=RELEASES):
        self.releases, self.archives, self.checksums, self.asked = releases, {}, {}, []

    def add(self, tag, install="echo installing\n", checksum=None):
        self.archives[tag] = archive(tag, install)
        self.checksums[tag] = checksum or hashlib.sha256(self.archives[tag]).hexdigest()

    def get(self, url, params=None, **_):
        self.asked.append(url)
        request = httpx.Request("GET", url)
        if url == f"https://api.github.com/repos/{REPO}/releases":
            size, page = (params or {}).get("per_page", 30), (params or {}).get("page", 1)
            return httpx.Response(200, json=self.releases[(page - 1) * size:page * size], request=request)
        if url.startswith(f"https://api.github.com/repos/{REPO}/releases/tags/"):
            found = [r for r in self.releases if r["tag_name"] == url.rsplit("/", 1)[1] and not r["draft"]]
            return httpx.Response(200 if found else 404, json=found[0] if found else {}, request=request)
        tag = url.split("/")[-2]
        if url.endswith(".tar.gz.sha256") and tag in self.checksums:
            return httpx.Response(200, text=f"{self.checksums[tag]}  sbx-{tag}.tar.gz\n", request=request)
        return httpx.Response(404, request=request)

    @contextlib.contextmanager
    def stream(self, method, url, **_):
        self.asked.append(url)
        tag = url.split("/")[-2]
        yield httpx.Response(200 if tag in self.archives else 404, content=self.archives.get(tag, b""),
                             request=httpx.Request(method, url))


def fake(test, version="v0.1.0", github=None):
    """GitHub faked, the installed release `version`, and no upgrade asked for or made yet."""
    import shutil
    from sbx_control import paths, updates
    (paths.run() / "apply/upgrade").unlink(missing_ok=True)
    for made in ("upgrade.json", "upgrade.lock"):
        (paths.state_dir() / made).unlink(missing_ok=True)
    shutil.rmtree(paths.state_dir() / "upgrades", ignore_errors=True)
    github = github or FakeGitHub()
    test.enterContext(patch.object(updates.httpx, "get", side_effect=github.get))
    test.enterContext(patch.object(updates.httpx, "stream", side_effect=github.stream))
    test.version = test.enterContext(patch.object(updates.setup, "version", return_value=version))
    test.enterContext(patch.dict(updates._cache, {"repository": None, "at": 0.0, "releases": [], "error": ""}))
    return github


class VersionTests(unittest.TestCase):
    def test_releases_sort_as_semver_and_a_checkout_past_a_tag_as_that_tag(self):
        from sbx_control.updates import key
        tags = ["v1.0.0", "v0.2.0", "v0.2.0-rc.10", "v0.10.0", "v0.2.0-rc.2", "v0.2.0-beta", "v0.1.9"]
        self.assertEqual(sorted(tags, key=key),
                         ["v0.1.9", "v0.2.0-beta", "v0.2.0-rc.2", "v0.2.0-rc.10", "v0.2.0", "v0.10.0", "v1.0.0"])
        self.assertEqual(key("v0.2.0-3-g1a2b3c4"), key("v0.2.0"))
        self.assertEqual(key("v0.2.0-rc.1-3-g1a2b3c4-dirty"), key("v0.2.0-rc.1"))
        for other in ("dev", "local", "1a2b3c4", "0.2.0", "v0.2", ""):
            self.assertIsNone(key(other), other)

    def test_only_newer_releases_and_candidates_only_when_asked(self):
        from sbx_control.updates import Release, newer
        found = [Release(t, "", "", "-" in t) for t in ("v0.3.0-rc.1", "v0.2.0", "v0.1.1", "v0.1.0", "v0.0.9")]
        self.assertEqual([r.tag for r in newer(found, "v0.1.0", False)], ["v0.2.0", "v0.1.1"])
        self.assertEqual([r.tag for r in newer(found, "v0.1.0-4-gabcdef0", True)], ["v0.3.0-rc.1", "v0.2.0", "v0.1.1"])
        self.assertEqual([r.tag for r in newer(found, "v0.2.0-rc.1", False)], ["v0.2.0"])
        self.assertEqual(newer(found, "v0.3.0", True), [])
        self.assertEqual(newer(found, "1a2b3c4", True), [])     # no release: nothing is known to be newer


class FetchTests(unittest.TestCase):
    def test_published_releases_with_the_archive_newest_by_version(self):
        from sbx_control import updates
        github = FakeGitHub()
        with patch.object(updates.httpx, "get", side_effect=github.get), \
                patch.object(updates.settings, "current", return_value=updates.settings.Settings()):
            found = updates.fetch()
        self.assertEqual([r.tag for r in found], ["v0.4.0-rc.1", "v0.3.0", "v0.2.0", "v0.1.5", "v0.1.0"])
        self.assertEqual([r.breaking for r in found], [False, False, True, False, False])
        self.assertTrue(found[0].prerelease)

    def test_every_page_of_releases(self):
        from sbx_control import updates
        github = FakeGitHub([release(f"v0.0.{n}") for n in range(150)] + [release("v1.0.0", "### Breaking changes\n* x")])
        with patch.object(updates.httpx, "get", side_effect=github.get), \
                patch.object(updates.settings, "current", return_value=updates.settings.Settings()):
            found = updates.fetch()
        self.assertEqual((len(found), found[0].tag, found[0].breaking), (151, "v1.0.0", True))
        self.assertEqual(len(github.asked), 2)


class UpdatesApiTests(Server):
    def setUp(self):
        super().setUp()
        self.repo_admin()
        self.github = fake(self)
        self.effects()     # the setup's own

    def offered(self):
        from sbx_control import updates
        updates.refresh()
        return self.client.get("/api/updates").json()

    def test_admins_only(self):
        from sbx_control import auth
        member = {"Authorization": f"Bearer {auth.mint_token('member@example.com', 'test')}"}
        self.assertEqual(self.client.get("/api/updates", headers=member).status_code, 403)
        self.assertEqual(self.client.post("/api/updates", json={"tag": "v0.3.0"}, headers=member).status_code, 403)
        self.assertEqual(self.effects(), [])

    def test_the_newest_release_with_the_notes_of_every_release_it_skips(self):
        found = self.offered()
        self.assertEqual(found["installed"], "v0.1.0")
        available = found["available"]
        self.assertEqual((available["latest"], available["url"]), ("v0.3.0", f"https://github.com/{REPO}/releases/tag/v0.3.0"))
        self.assertEqual([r["tag"] for r in available["releases"]], ["v0.3.0", "v0.2.0", "v0.1.5"])
        self.assertTrue(available["breaking"])     # v0.2.0's, which the upgrade passes
        self.assertIsNone(found["upgrade"])

    def test_candidates_when_asked_and_nothing_when_the_check_is_off(self):
        from sbx_control import settings
        settings.update("updates", {"prereleases": True}, by="test")
        self.assertEqual(self.offered()["available"]["latest"], "v0.4.0-rc.1")
        settings.update("updates", {"check": False}, by="test")
        self.assertIsNone(self.offered()["available"])

    def test_nothing_newer_than_the_newest_and_nothing_for_a_checkout_without_a_release(self):
        for version in ("v0.3.0", "v0.3.0-2-gabcdef0", "1a2b3c4"):
            self.version.return_value = version
            self.assertIsNone(self.offered()["available"], version)

    def test_installs_only_the_newest_release(self):
        for tag in ("v0.2.0", "v0.4.0-rc.1", "v9.9.9", "v0.1.0", "../../etc", ""):
            response = self.client.post("/api/updates", json={"tag": tag})
            self.assertEqual(response.status_code, 400, tag)
        self.assertIn("v0.3.0 is", response.json()["detail"])
        self.assertEqual(self.effects(), [])
        self.version.return_value = "1a2b3c4"
        self.assertIn("not a release", self.client.post("/api/updates", json={"tag": "v0.3.0"}).json()["detail"])

    def test_the_newest_asks_the_host_for_the_upgrade_once(self):
        from sbx_control import paths
        self.github.asked.clear()
        response = self.client.post("/api/updates", json={"tag": "v0.3.0"})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(self.github.asked)     # GitHub asked again, not the cache
        self.assertEqual((paths.run() / "apply/upgrade").read_text(), "v0.3.0\n")
        self.assertEqual(sorted(p.name for p in (paths.run() / "apply").iterdir()), ["upgrade"])   # written whole, renamed in
        self.assertEqual(response.json()["upgrade"]["state"], "running")
        self.assertEqual(response.json()["upgrade"]["tag"], "v0.3.0")
        again = self.client.post("/api/updates", json={"tag": "v0.3.0"})
        self.assertEqual(again.status_code, 409, again.text)

    def test_check_now_asks_github_and_says_when(self):
        self.github.asked.clear()
        found = self.client.post("/api/updates/check").json()
        self.assertEqual(self.github.asked, [f"https://api.github.com/repos/{REPO}/releases"])
        self.assertEqual(found["available"]["latest"], "v0.3.0")
        self.assertTrue(found["checked_at"])
        self.assertEqual(found["check_error"], "")

    def test_github_not_answering_refuses_the_upgrade(self):
        from sbx_control import updates
        with patch.object(updates.httpx, "get", side_effect=httpx.ConnectError("no route")):
            response = self.client.post("/api/updates", json={"tag": "v0.3.0"})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.effects(), [])


class UpgradeEffectTests(Installation):
    """sbx-apply upgrade, as the path unit runs it: the request names the tag."""

    def setUp(self):
        super().setUp()
        from sbx_control import paths, updates
        self.github = fake(self)
        folder = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="sbx-upgrade-")))
        self.log = folder / "log/sbx-upgrade.log"
        self.enterContext(patch.object(updates, "LOG", self.log))
        self.ran = folder / "ran"
        self.github.add("v0.3.0", f"echo '==> Preflight'\npwd > {self.ran}\n"
                        "echo 'rollback: sh /var/lib/sbx/upgrade-backups/1/rollback.sh'\necho '==> Done: https://sbx.test'\n")
        self.paths, self.updates = paths, updates

    def apply(self, tag):
        from sbx_control import host
        (self.paths.run() / "apply").mkdir(parents=True, exist_ok=True)
        (self.paths.run() / "apply/upgrade").write_text(f"{tag}\n")
        return host.apply("upgrade")

    def record(self):
        return json.loads((self.paths.state_dir() / "upgrade.json").read_text())

    def test_the_host_has_a_path_unit_for_the_upgrade(self):
        from sbx_control import host
        self.assertIn("sbx-apply-upgrade.path", [u.name for u in host.units()])

    def test_downloads_checks_unpacks_and_runs_the_releases_install_sh(self):
        self.assertEqual(self.apply("v0.3.0"), 0)
        source = self.paths.state_dir() / "upgrades/sbx-v0.3.0"
        self.assertEqual(Path(self.ran.read_text().strip()).resolve(), source.resolve())
        self.assertFalse((self.paths.run() / "apply/upgrade").exists())
        self.assertFalse((self.paths.state_dir() / "upgrades/sbx-v0.3.0.tar.gz").exists())
        self.assertEqual(self.github.asked, [f"https://api.github.com/repos/{REPO}/releases/tags/v0.3.0",
                                             f"{DOWNLOADS}/v0.3.0/sbx-v0.3.0.tar.gz",
                                             f"{DOWNLOADS}/v0.3.0/sbx-v0.3.0.tar.gz.sha256"])
        record = self.record()
        self.assertEqual((record["tag"], record["from"], record["state"]), ("v0.3.0", "v0.1.0", "done"))
        self.assertEqual(record["rollback"], "sh /var/lib/sbx/upgrade-backups/1/rollback.sh")
        self.assertIn(f"sbx-v0.3.0.tar.gz: sha256 {self.github.checksums['v0.3.0']}", self.log.read_text())
        self.assertFalse(self.updates.running())
        self.version.return_value = "v0.3.0"     # sbx-control restarted on it
        upgrade = self.updates.view()["upgrade"]
        self.assertEqual((upgrade["state"], upgrade["progress"]), ("done", 1.0))
        size = len(self.github.archives["v0.3.0"]) / 1e6
        self.assertEqual(upgrade["steps"], [
            {"title": "Download sbx-v0.3.0.tar.gz and check its .sha256", "state": "done",
             "detail": f"{size:.1f} of {size:.1f} MB"},
            {"title": "Preflight", "state": "done", "detail": ""},
            {"title": "Done: https://sbx.test", "state": "done", "detail": ""}])
        self.assertIn("==> Preflight", upgrade["log_tail"])

    def test_progress_while_it_runs_and_where_it_stopped(self):
        from sbx_control import updates
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.log.write_text("an older upgrade\n==> Old step\n")
        record = {"tag": "v0.3.0", "from": "v0.1.0", "state": "running", "log": str(self.log),
                  "log_offset": self.log.stat().st_size, "phase": "download", "received": 500_000, "total": 2_000_000}
        found = updates.progress(record)
        self.assertEqual((found["progress"], found["steps"][0]["state"], found["steps"][0]["detail"]),
                         (0.025, "running", "0.5 of 2.0 MB"))
        self.assertEqual(len(found["steps"]), 1)      # nothing of the older run
        with open(self.log, "a") as log:
            log.write("==> Preflight\n==> Packages and Docker\n==> sbx-control release\n")
        found = updates.progress({**record, "phase": "install"})
        self.assertEqual([s["state"] for s in found["steps"]], ["done", "done", "done", "running"])
        self.assertEqual(found["progress"], 0.28)
        failed = updates.progress({**record, "phase": "install", "state": "failed"})
        self.assertEqual(failed["steps"][-1], {"title": "sbx-control release", "state": "failed", "detail": ""})

    def test_an_archive_that_does_not_match_its_checksum_is_not_installed(self):
        self.github.add("v0.3.0", f"pwd > {self.ran}\n", checksum="0" * 64)
        self.assertEqual(self.apply("v0.3.0"), 1)
        self.assertFalse(self.ran.exists())
        self.assertFalse((self.paths.state_dir() / "upgrades/sbx-v0.3.0").exists())
        record = self.record()
        self.assertEqual(record["state"], "failed")
        self.assertIn("does not match its .sha256", record["error"])
        self.assertIn("does not match", self.log.read_text())

    def test_a_failed_install_sh_is_reported_with_its_log_and_the_rollback_while_it_runs(self):
        self.github.add("v0.3.0", "echo 'rollback: sh /var/lib/sbx/upgrade-backups/2/rollback.sh'\nexit 3\n")
        self.assertEqual(self.apply("v0.3.0"), 1)
        self.assertEqual((self.record()["state"], self.record()["error"]), ("failed", "install.sh exited with 3"))
        self.github.add("v0.3.0", "printf '\\nFAILED: %s\\n' 'Embed would be recreated' >&2\nexit 1\n")
        self.assertEqual(self.apply("v0.3.0"), 1)
        self.assertEqual(self.record()["error"], "Embed would be recreated")     # install.sh's own reason
        self.github.add("v0.3.0", "echo 'rollback: sh /var/lib/sbx/upgrade-backups/2/rollback.sh'\nexit 3\n")
        self.assertEqual(self.apply("v0.3.0"), 1)
        before = self.updates.view()["upgrade"]           # install.sh went back to v0.1.0 itself
        self.assertEqual((before["state"], before["log"], before["rollback"]), ("failed", str(self.log), ""))
        self.version.return_value = "v0.3.0"               # it failed after v0.3.0 started
        self.assertEqual(self.updates.view()["upgrade"]["rollback"], "sh /var/lib/sbx/upgrade-backups/2/rollback.sh")
        self.version.return_value = "v0.4.0"               # a later release: old news
        self.assertIsNone(self.updates.view()["upgrade"])

    def test_only_a_published_release_with_its_files(self):
        self.github.add("v0.9.0")      # a draft, though its files are there
        self.github.add("v0.8.0")      # listed without them
        for tag in ("v0.9.0", "v0.8.0", "v0.7.0"):
            self.assertEqual(self.apply(tag), 1, tag)
            self.assertEqual((self.record()["tag"], self.record()["state"]), (tag, "failed"))
        self.assertFalse(any("/releases/download/" in url for url in self.github.asked))

    def test_never_an_older_or_the_same_release(self):
        for tag in ("v0.1.0", "v0.0.9", "../x", ""):
            self.assertEqual(self.apply(tag), 1, tag)
        self.assertEqual(self.github.asked, [])
        self.assertFalse((self.paths.state_dir() / "upgrade.json").exists())

    def test_running_while_the_installer_holds_the_lock_and_failed_when_it_died(self):
        self.updates._record({"tag": "v0.3.0", "from": "v0.1.0", "state": "running", "log": str(self.log)})
        with open(self.paths.state_dir() / "upgrade.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertTrue(self.updates.running())
            self.assertEqual(self.updates.last()["state"], "running")
        self.assertFalse(self.updates.running())
        self.assertEqual(self.updates.last()["state"], "failed")

    def test_sbx_admin_upgrade_lists_the_newer_releases_and_how_to_install(self):
        from sbx_control import admin
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(admin.main(["upgrade"]), 0)
        self.assertIn("== v0.2.0 (breaking changes)", out.getvalue())
        self.assertIn("sbx-admin upgrade v0.3.0", out.getvalue())
        self.assertEqual(self.effects(), [])
