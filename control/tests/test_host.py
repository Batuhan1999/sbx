"""The host's units and commands, rendered from one list (host.py) into a
throwaway systemd directory, with systemctl recorded instead of run."""
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from support import Installation


def connector_settings(kind, **changes):
    from sbx_control import extensions
    connector = extensions.get(kind)
    connector.save_settings({**connector.settings().model_dump(), **changes}, by="test")


class HostTests(Installation):
    def setUp(self):
        super().setUp()
        from sbx_control import extensions, host
        self.addCleanup(setattr, extensions, "_connectors", None)
        connector_settings("claude", enabled=True)
        connector_settings("amp", enabled=False)
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="sbx-host-")))
        self.systemd, self.sbin, self.current = root / "systemd", root / "sbin", root / "opt/current"
        for folder in (self.systemd, self.sbin, self.current):
            folder.mkdir(parents=True)
        self.enterContext(patch.dict(os.environ, {"SBX_SYSTEMD_DIR": str(self.systemd)}))
        self.enterContext(patch.object(host, "SBIN", self.sbin))
        self.enterContext(patch.object(host, "CURRENT", self.current))
        self.enterContext(patch.object(host, "INFISICAL_COMPOSE", self.current / "infisical/compose.yaml"))
        self.ready = self.enterContext(patch.object(host, "wait_ready"))
        self.run = self.enterContext(patch.object(host.subprocess, "run"))
        self.host = host

    def systemctl(self):
        return [c.args[0][1:] for c in self.run.call_args_list if c.args[0][0] == "systemctl"]

    def unit(self, name):
        return (self.systemd / name).read_text()

    def test_install_runs_every_unit_and_command_from_the_active_release(self):
        self.host.install()
        # Switching releases is one symlink: whatever a unit or command runs of
        # sbx is reached through it (or through a command that is).
        from sbx_control.runtime.embed import host as embed_host
        outside = ("/usr/bin/", "/bin/", str(embed_host.EMBED))   # docker, sh, Embed's compose project
        for unit in self.systemd.glob("*.service"):
            for line in unit.read_text().splitlines():
                key, _, value = line.partition("=")
                for path in (w for w in value.split() if key in ("ExecStart", "WorkingDirectory") and w.startswith("/")):
                    with self.subTest(unit=unit.name, path=path):
                        self.assertTrue(path.startswith((str(self.current), str(self.sbin), *outside)))
        for command in self.sbin.iterdir():
            with self.subTest(command=command.name):
                self.assertIn(f"cd {self.current}\n", command.read_text())
        self.assertNotIn("sbx-infisical", self.unit("sbx-control.service"))   # no local Infisical yet
        self.assertTrue((self.systemd / "claude-orchestrator.service").exists())
        self.assertFalse((self.sbin / "sbx-firewall").exists())   # no network.sandbox_cidr
        calls = self.systemctl()
        self.assertIn(["disable", "--now", "-q", "sbx-amp.service"], calls)   # Amp is off
        self.assertIn(["disable", "--now", "-q", "sbx-firewall.service"], calls)
        # The guard before sbx-control; sbx-control answering before the connectors that call it.
        self.assertLess(calls.index(["restart", "sbx-host-guard.service"]), calls.index(["restart", "sbx-control.service"]))
        self.assertLess(calls.index(["restart", "sbx-control.service"]), calls.index(["restart", "claude-orchestrator.service"]))
        self.ready.assert_called_once()

    def test_a_local_infisical_adds_its_units_and_sbx_control_requires_it(self):
        from sbx_control import paths
        (paths.etc() / "infisical.env").write_text("ENCRYPTION_KEY=test-only\n")
        self.addCleanup((paths.etc() / "infisical.env").unlink)
        self.host.install(["sbx-infisical.service", "sbx-control.service"])
        self.assertIn("Requires=sbx-infisical.service", self.unit("sbx-control.service"))
        self.assertIn("After=sbx-infisical.service", self.unit("sbx-backup.service"))
        calls = self.systemctl()
        started = calls.index(["enable", "-q", "--now", "sbx-infisical.service"])
        self.assertLess(started, calls.index(["restart", "sbx-control.service"]))
        self.assertFalse([c for c in calls if c[0] == "disable"])   # only the named units are touched

    def test_one_backup_timer_runs_whatever_the_secret_store(self):
        from sbx_control import settings
        self.host.install()
        self.assertIn("sbx_control.backup scheduled", self.unit("sbx-backup.service"))
        self.assertIn("OnUnitActiveSec=24h", self.unit("sbx-backup.timer"))
        self.assertIn(["enable", "-q", "--now", "sbx-backup.timer"], self.systemctl())
        settings.update("backup", {"every_hours": 0}, by="test")
        self.host.install(["sbx-backup.timer"])
        self.assertIn(["disable", "--now", "-q", "sbx-backup.timer"], self.systemctl())

    def test_embeds_env_has_one_writer_for_the_keys_sbx_owns(self):
        from sbx_control import settings
        from sbx_control.runtime.embed import host as embed_host
        embed = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="sbx-embed-")))
        self.enterContext(patch.object(embed_host, "EMBED", embed))
        (embed / ".env").write_text("POSTGRES_PASSWORD=runtime-own\n#HUGEPAGES=0\nE2B_API_IMAGE=old\n")
        pins = embed / "pins.env"
        pins.write_text("E2B_API_IMAGE=new\nRUNTIME_COMMIT=abc\nPOSTGRES_PASSWORD=vendor-default\n")
        settings.update("domain", {"name": "sbx.example.com"}, by="test")
        settings.update("network", {"internal_cidrs": ["10.0.0.0/24"]}, by="test")
        embed_host.write_embed_env(hugepages=4096, pins=pins)
        env = dict(line.split("=", 1) for line in (embed / ".env").read_text().splitlines())
        self.assertEqual(env, {"POSTGRES_PASSWORD": "runtime-own", "HUGEPAGES": "4096", "E2B_API_IMAGE": "new",
                               "RUNTIME_COMMIT": "abc", "ALLOW_SANDBOX_INTERNAL_CIDRS": "10.0.0.0/24",
                               "E2B_PUBLIC_DOMAIN": "sbx.example.com",
                               "E2B_PUBLIC_SANDBOX_URL": "https://sandbox.sbx.example.com"})
        # An extension's runtime layer: its compose file joins Embed's, and goes again when cleared.
        connector_settings("amp", checkpoint_api_image="sbx-checkpoint-api:1", orchestrator_binary="/opt/amp/orchestrator")
        embed_host.write_embed_env()
        env = dict(line.split("=", 1) for line in (embed / ".env").read_text().splitlines())
        self.assertEqual(env["COMPOSE_FILE"], "compose.yaml:compose.override.yaml:amp/compose.yaml")
        self.assertEqual(env["SBX_ORCHESTRATOR_BINARY"], "/opt/amp/orchestrator")
        self.assertTrue((embed / "amp/orchestrator-launch.sh").exists())
        connector_settings("amp", checkpoint_api_image="", orchestrator_binary="")
        before, after = embed_host.write_embed_env()
        self.assertNotIn("COMPOSE_FILE", after)
        self.assertNotIn("SBX_CHECKPOINT_API_IMAGE", after)
        self.assertEqual(embed_host.write_embed_env(), (after, after))

    def test_an_upgrade_that_recreates_embed_under_sandboxes_is_refused_before_anything_changes(self):
        """upgrade-check (Embed's backend) lays a release over a copy of
        Embed's project; a fake compose hashes each service from the files it
        reads there."""
        import hashlib
        import subprocess
        from sbx_control.runtime.embed import __main__ as embed_cli, host as embed_host
        embed = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="sbx-embed-"))) / "e2b"
        release = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="sbx-release-")))
        vendored = release / "vendor/e2b-embed/compose"
        vendored.mkdir(parents=True)
        (release / "embed").mkdir()
        for project in (embed, vendored):
            project.mkdir(exist_ok=True)
            (project / "compose.yaml").write_text("services: {orchestrator: {}, api: {}}\n")
        (embed / "compose.override.yaml").write_text("api: one\n")
        (release / "embed/compose.override.yaml").write_text("api: one\n")
        (embed / ".env").write_text("E2B_ORCHESTRATOR_IMAGE=orch:1\nHUGEPAGES=4096\nPOSTGRES_PASSWORD=runtime-own\n")
        (vendored / ".env").write_text("E2B_ORCHESTRATOR_IMAGE=orch:1\nPOSTGRES_PASSWORD=vendor-default\n")
        self.enterContext(patch.object(embed_host, "EMBED", embed))
        embed_host.write_embed_env()     # Embed as install.sh left it

        def hashes(folder):
            # api mounts ./api.sh: its hash holds the absolute source path, as compose's does.
            env = (folder / ".env").read_text()
            orchestrator = "".join(line for line in env.splitlines() if "ORCHESTRATOR" in line or "HUGEPAGES" in line)
            return {"orchestrator": hashlib.sha256(orchestrator.encode()).hexdigest(),
                    "api": hashlib.sha256((folder / "compose.override.yaml").read_bytes()
                                          + str(folder / "api.sh").encode()).hexdigest()}

        before = (embed / ".env").read_text()
        labels = hashes(embed)                     # the containers as they were created

        def docker(args, cwd=None, **_):
            if args[:2] == ["docker", "ps"]:
                return subprocess.CompletedProcess(args, 0, "".join(f"{s} {h}\n" for s, h in labels.items()), "")
            self.assertEqual(args, ["docker", "compose", "-p", "e2b", "config", "--hash", "*"])
            return subprocess.CompletedProcess(args, 0, "".join(f"{s} {h}\n" for s, h in hashes(Path(cwd)).items()), "")
        self.run.side_effect = docker
        self.assertEqual(embed_host.recreates(release, 4096), [])                       # the copy's paths do not count
        self.assertEqual(embed_host.recreates(release, 8192), ["orchestrator"])         # another hugepages count
        (embed / ".env").write_text(before.replace("orch:1", "orch:0"))                 # a change up would apply already
        self.assertEqual(embed_host.recreates(release, 4096), ["orchestrator"])
        (embed / ".env").write_text(before)
        (vendored / ".env").write_text("E2B_ORCHESTRATOR_IMAGE=orch:2\n")              # a new runtime pin
        (release / "embed/compose.override.yaml").write_text("api: two\n")              # sbx's override
        self.assertEqual(embed_host.recreates(release, 4096), ["api", "orchestrator"])
        self.assertEqual((embed / ".env").read_text(), before)                          # Embed's own project untouched
        self.assertEqual((embed / "compose.override.yaml").read_text(), "api: one\n")
        check = ["upgrade-check", "--source", str(release), "--hugepages", "4096"]
        with patch("sbx_control.platform.connect"), patch("sbx_control.runtime.embed.own_sandboxes", return_value=[]):
            self.assertEqual(embed_cli.main(check), 0)
        with patch("sbx_control.platform.connect"), patch("sbx_control.runtime.embed.own_sandboxes", side_effect=OSError("down")), \
                patch("sys.stderr"):
            self.assertEqual(embed_cli.main(check), 1)              # Embed not answering is no answer
        with patch("sbx_control.platform.connect"), patch("sbx_control.runtime.embed.own_sandboxes", return_value=["paused"]), \
                patch("sys.stderr", new_callable=__import__("io").StringIO) as said:
            self.assertEqual(embed_cli.main(check), 1)
        self.assertIn("recreates Embed's api, orchestrator, which the 1 sandbox here", said.getvalue())
        # Docker not answering is no answer: refused, not taken for "nothing changes".
        self.run.side_effect = subprocess.TimeoutExpired(["docker", "ps"], 60)
        with patch("sys.stderr", new_callable=__import__("io").StringIO) as said:
            self.assertEqual(embed_cli.main(check), 1)
        self.assertIn("could not compare this release with Embed's running containers", said.getvalue())

    def test_a_domain_change_writes_only_the_domain_keys(self):
        # A pending runtime-network change recreates the runtime, which only
        # runtime.apply_network may do (it refuses under running sandboxes).
        from sbx_control import settings
        from sbx_control.runtime.embed import host as embed_host
        embed = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="sbx-embed-")))
        self.enterContext(patch.object(embed_host, "EMBED", embed))
        (embed / ".env").write_text("ALLOW_SANDBOX_INTERNAL_CIDRS=10.0.0.0/24\n")
        settings.update("network", {"internal_cidrs": ["10.9.0.0/24"]}, by="test")
        settings.update("domain", {"name": "sbx.example.com"}, by="test")
        connector_settings("amp", checkpoint_api_image="sbx-checkpoint-api:1", orchestrator_binary="/opt/amp/orchestrator")
        self.assertEqual(self.host.apply("runtime-env"), 0)
        env = dict(line.split("=", 1) for line in (embed / ".env").read_text().splitlines())
        self.assertEqual(env, {"ALLOW_SANDBOX_INTERNAL_CIDRS": "10.0.0.0/24", "E2B_PUBLIC_DOMAIN": "sbx.example.com",
                               "E2B_PUBLIC_SANDBOX_URL": "https://sandbox.sbx.example.com"})
        self.assertFalse((embed / "amp").exists())

    def test_an_upgrade_drops_effect_units_it_no_longer_has_and_keeps_their_requests(self):
        # v0.1 named the runtime's effects embed-env and embed-network.
        from sbx_control import paths
        self.effects()
        for name in ("sbx-apply-embed-env.path", "sbx-apply-embed-env.service"):
            (self.systemd / name).write_text("[Unit]\n")
        (self.systemd / "sbx-something-else.service").write_text("[Unit]\n")
        (paths.run() / "apply/embed-env").write_text("1\n")
        (paths.run() / "apply/embed-network").write_text("1\n")
        self.host.install()
        self.assertEqual(sorted(p.name for p in self.systemd.glob("sbx-apply-embed-*")), [])
        self.assertTrue((self.systemd / "sbx-something-else.service").exists())   # only the effects' units
        self.assertTrue((self.systemd / "sbx-apply-runtime-env.path").exists())
        self.assertFalse((self.systemd / "sbx-apply-runtime-network.path").exists())   # network apply's
        # The web app's Update runs install.sh inside sbx-apply-upgrade.service: never a stale unit.
        self.assertTrue((self.systemd / "sbx-apply-upgrade.service").exists())
        self.assertFalse(any("sbx-apply-upgrade" in " ".join(c) for c in self.systemctl() if c[:2] == ["disable", "--now"]))
        calls = self.systemctl()
        dropped = ["disable", "--now", "-q", "sbx-apply-embed-env.path", "sbx-apply-embed-env.service"]
        self.assertLess(calls.index(dropped), calls.index(["daemon-reload"]))
        self.assertLess(calls.index(dropped), calls.index(["enable", "-q", "--now", "sbx-apply-runtime-env.path"]))
        self.assertEqual(self.effects(), ["runtime-env", "runtime-network"])
        # A partial install touches only the units it names, and the watcher of a request it carried over.
        (self.systemd / "sbx-apply-gone.path").write_text("[Unit]\n")
        (paths.run() / "apply/embed-env").write_text("1\n")
        self.run.reset_mock()
        self.host.install(["sbx-control.service"])
        self.assertTrue((self.systemd / "sbx-apply-gone.path").exists())
        self.assertEqual(self.effects(), ["runtime-env"])
        self.assertIn(["enable", "-q", "--now", "sbx-apply-runtime-env.path"], self.systemctl())
        self.assertNotIn(["enable", "-q", "--now", "sbx-apply-tls.path"], self.systemctl())

    def test_host_tools_are_the_ones_catalog_packages_declare(self):
        from sbx_control import catalog
        self.assertEqual(sorted(catalog.host_tools()), ["psql", "temporal"])
        with patch.object(self.host.shutil, "which", side_effect=lambda name: "/usr/bin/psql" if name == "psql" else None):
            self.host.install_tools()
        installed = [c.args[0] for c in self.run.call_args_list if c.args[0][0] == "sh"]
        self.assertEqual(installed, [["sh", "-c", catalog.host_tools()["temporal"]]])

    def test_apply_removes_the_request_and_restarts_only_its_units(self):
        from sbx_control import paths
        (paths.run() / "apply").mkdir(parents=True, exist_ok=True)
        (paths.run() / "apply/orchestrator").write_text("1\n")
        self.assertEqual(self.host.apply("orchestrator"), 0)
        self.assertFalse((paths.run() / "apply/orchestrator").exists())
        self.assertIn(["restart", "claude-orchestrator.service"], self.systemctl())
        self.assertNotIn(["restart", "sbx-control.service"], self.systemctl())
        self.assertEqual(self.host.apply("no-such-effect"), 2)

    def test_prune_keeps_the_newest_releases_and_the_active_one(self):
        releases = self.current.parent / "releases"
        made = []
        for index in range(5):
            path = releases / f"release-{index}"
            path.mkdir(parents=True)
            os.utime(path, (1000 + index, 1000 + index))
            made.append(path)
        self.current.rmdir()
        self.current.symlink_to(made[0])
        with patch.object(self.host, "RELEASES", releases):
            self.host.prune_releases(keep=2)
        self.assertEqual(sorted(p.name for p in releases.iterdir()), ["release-0", "release-3", "release-4"])

    def test_an_upgrade_stops_the_writers_and_backs_up_before_it_migrates(self):
        import sqlite3
        import subprocess
        from types import SimpleNamespace
        from sbx_control import paths, state
        self.host.install()
        releases = self.current.parent / "releases"
        (releases / "old").mkdir(parents=True)
        self.current.rmdir()
        self.current.symlink_to(releases / "old")
        with state.db() as conn:
            conn.execute("insert into settings (key, value) values ('upgrade_marker', 'before')")
        made = []

        def run(args, **kwargs):
            if args[0] == str(self.sbin / "sbx-admin"):   # the active release's backup bundle
                made.append(args)
                Path(args[-1]).write_bytes(b"bundle")
            # Amp is off: its unit is installed, not running.
            return SimpleNamespace(returncode=3 if args[-1] == "sbx-amp.service" else 0, stdout="", stderr="")
        self.run.reset_mock()
        self.run.side_effect = run
        out = self.host.upgrade_backup()
        stops = [c for c in self.systemctl() if c[0] == "stop"]
        self.assertEqual(stops, [["stop", "sbx-control.service", "claude-orchestrator.service", "sbx-acme-dns.service"]])
        self.assertEqual(made, [[str(self.sbin / "sbx-admin"), "backup", "--out", str(out / "bundle.tar.gz")]])
        self.assertEqual(out.parent, paths.state_dir() / "upgrade-backups")
        self.assertEqual(out.stat().st_mode & 0o777, 0o700)
        self.assertEqual((out / "sbx.db").stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(out / "sbx.db") as copy:
            self.assertEqual(copy.execute("select value from settings where key = 'upgrade_marker'").fetchone(), ("before",))
        self.assertTrue((out / "systemd/sbx-control.service").is_file())
        self.assertTrue((out / "systemd/claude-orchestrator.service").is_file())
        rollback = (out / "rollback.sh").read_text()
        self.assertIn(f"ln -sfn {(releases / 'old').resolve()} {self.current}.new", rollback)
        self.assertIn(f"install -m 600 {out / 'sbx.db'} {paths.database()}", rollback)
        # runtime.env as the active release knew it: an upgrade may enable a runtime it can't load.
        self.assertEqual((out / "runtime.env").read_text(), paths.runtime_env().read_text())
        self.assertEqual((out / "runtime.env").stat().st_mode & 0o777, 0o600)
        restore = f"install -m 600 {out / 'runtime.env'} {paths.runtime_env()}"
        self.assertIn(restore, rollback)
        before = paths.runtime_env().read_text()
        paths.runtime_env().write_text(before + "SBX_RUNTIMES=embed,e2b-cloud\nE2B_CLOUD_API_KEY=test-only\n")
        subprocess.check_call(["sh", "-c", restore])
        self.assertEqual(paths.runtime_env().read_text(), before)
        subprocess.run(["sh", "-n", str(out / "rollback.sh")], check=True)
        # The release's effect paths come back on, whatever the upgrade disabled.
        self.assertTrue((out / "systemd/sbx-apply-tls.path").is_file())
        self.assertIn(f"for unit in {out / 'systemd'}/sbx-apply-*.path; do\n", rollback)
        self.assertNotIn("start", [c[0] for c in self.systemctl()])   # they stay stopped for the migration
        self.assertIn("systemctl start sbx-control.service claude-orchestrator.service sbx-acme-dns.service\n", rollback)
        # Back to a release from before the effects' rename, a pending request takes its old name again.
        start = rollback.index("if [ -e")
        end = rollback.index("\nfi\n", start) + 4
        requests = paths.run() / "apply"
        requests.mkdir(parents=True, exist_ok=True)
        (requests / "runtime-network").write_text("1\n")
        subprocess.check_call(["sh", "-c", rollback[start:end]])   # run is the systemctl recorder here
        self.assertTrue((requests / "runtime-network").exists())     # the restored release knows runtime-*
        (out / "systemd/sbx-apply-embed-env.path").write_text("[Unit]\n")
        subprocess.check_call(["sh", "-c", rollback[start:end]])
        self.assertTrue((requests / "embed-network").exists())
        self.assertFalse((requests / "runtime-network").exists())

    def test_an_upgrade_stops_platform_services_that_run_from_another_release_directory(self):
        from types import SimpleNamespace
        self.host.install()
        unit = self.systemd / "sbx-control.service"
        unit.write_text(unit.read_text().replace(f"WorkingDirectory={self.current}",
                                                 f"WorkingDirectory={self.current.parent}/some-release"))
        self.run.reset_mock()
        self.run.side_effect = lambda args, **kwargs: SimpleNamespace(
            returncode=3 if args[-1] == "sbx-amp.service" else 0, stdout="", stderr="")
        self.assertIn("sbx-control.service", self.host.writers())

    def test_a_failed_upgrade_backup_starts_the_active_release_again(self):
        from types import SimpleNamespace
        self.host.install()
        self.run.reset_mock()
        self.run.side_effect = lambda args, **kwargs: SimpleNamespace(
            returncode=0 if args[0] == "systemctl" else 1, stdout="", stderr="no room")
        with self.assertRaisesRegex(SystemExit, "no room"):
            self.host.upgrade_backup()
        self.assertEqual([c[0] for c in self.systemctl() if c[0] != "is-active"], ["stop", "start"])

    def test_a_first_install_has_nothing_to_back_up(self):
        from sbx_control import paths
        with patch.object(paths, "database", return_value=self.current / "missing.db"):
            self.run.reset_mock()
            self.assertIsNone(self.host.upgrade_backup())
        self.assertEqual(self.systemctl(), [])


class StateFileTests(Installation):
    def test_migrate_keeps_sbx_db_readable_by_root_only(self):
        from sbx_control import paths, state
        for suffix in ("", "-wal", "-shm"):
            path = paths.database().with_name(paths.database().name + suffix)
            if path.exists():
                path.chmod(0o644)
        state.migrate()
        for suffix in ("", "-wal", "-shm"):
            path = paths.database().with_name(paths.database().name + suffix)
            if path.exists():
                with self.subTest(path=path.name):
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(paths.database().stat().st_mode & 0o777, 0o600)
