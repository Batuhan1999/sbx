"""Lizard as a sandbox runtime, against a fake of Lizard's HTTP API
(fake_lizard): sandboxes from snapshots and their metadata, the timeout that
pauses (Lizard's own deletes), pause and the cold-boot resume, commands and
files, templates as snapshots at the installation's size, the token-gated
previews, Lizard's refusals as sbx's, and its settings."""
import time
import unittest
from unittest.mock import patch

from fake_lizard import FakeLizard, configured

from sbx_control import runtime
from sbx_control.runtime.lizard import build, errors, lifecycle, plans, sandbox
from support import RUNTIME, Installation

LIZARD_ENV = RUNTIME + "SBX_RUNTIMES=lizard\nLIZARD_API_KEY=liz_unit-test\n"


class Backend(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(time, "sleep"))
        self.enterContext(patch.object(build, "_installation", return_value="inst-a"))
        self.backend, self.fake = configured()
        self.snapshot = self.fake.snapshot("tpl")

    def create(self, **options):
        return self.backend.create("tpl", **{"timeout": 900, "envs": {"SBX_NAME": "box"},
                                             "metadata": {"sbx": "box", "sbx_install": "inst-a"}, **options})

    def bodies(self, method, path):
        return [body for m, p, _, body in self.fake.calls if m == method and p == path]

    def scripts(self):
        return [cmd[2] if isinstance(cmd, list) else cmd for _, cmd, _, _ in self.fake.ran]

    def test_create_starts_the_templates_snapshot_in_the_project_with_no_lifetime(self):
        handle = self.create()
        (body,) = self.bodies("POST", "/api/sandboxes")
        self.assertEqual(body, {"snapshotId": self.snapshot, "projectId": "proj_1", "region": "us-east-1",
                                "timeoutMs": 0, "envs": {"SBX_NAME": "box"},
                                "metadata": {"sbx": "box", "sbx_install": "inst-a", "sbx_template": "tpl"}})
        self.assertTrue(all(h["X-API-Key"] == "liz_unit-test" for h in self.fake.headers))
        # What sbx needs of the machine as root, then the template's start command as the user.
        self.assertEqual([(cmd[2], user) for _, cmd, user, _ in self.fake.ran],
                         [(lifecycle.MACHINE, "root"), (lifecycle.BOOT, "user")])
        info = self.backend.info(handle.sandbox_id)
        self.assertEqual((info.state, info.template_id, info.cpu_count, info.memory_mb),
                         (runtime.RUNNING, "tpl", 2, 4096))
        self.assertAlmostEqual(info.end_at.timestamp(), time.time() + 900, delta=5)

    def test_a_missing_template_is_gone_and_a_sandbox_that_cannot_be_prepared_is_deleted(self):
        with self.assertRaises(runtime.TemplateGone):
            self.backend.create("none", timeout=900)
        self.fake.respond = lambda sandbox_id, script: ("", 1) if script == lifecycle.MACHINE else ""
        with self.assertRaisesRegex(runtime.Failed, "could not be prepared"):
            self.create()
        self.assertEqual(self.fake.sandboxes, {})

    def test_the_newest_ready_snapshot_of_a_name_is_the_template(self):
        self.fake.snapshot("tpl", status="failed")
        newest = self.fake.snapshot("tpl")
        self.fake.snapshot("other")
        self.create()
        self.assertEqual(self.bodies("POST", "/api/sandboxes")[0]["snapshotId"], newest)
        self.assertEqual(dict(self.backend.templates()), {"tpl": "ready", "other": "ready"})

    def test_sbx_s_timeout_pauses_where_lizard_s_would_delete(self):
        handle = self.create()
        self.backend.set_timeout(handle.sandbox_id, 600)
        self.assertEqual(self.bodies("POST", f"/api/sandboxes/{handle.sandbox_id}/timeout"), [])   # never Lizard's
        self.assertEqual([i.state for i in self.backend.sandboxes()], [runtime.RUNNING])
        self.backend._deadlines[handle.sandbox_id] = time.time() - 1
        (listed,) = self.backend.sandboxes({"sbx_install": "inst-a"})
        self.assertEqual((listed.state, self.fake.sandboxes[handle.sandbox_id]["status"]), (runtime.PAUSED, "paused"))
        with self.assertRaises(runtime.NotRunning):
            self.backend.set_timeout(handle.sandbox_id, 600)

    def test_a_deadline_not_known_after_a_restart_starts_again_and_never_pauses_at_once(self):
        handle = self.create()
        again, _ = configured(self.fake)   # sbx-control restarted: nothing kept
        (listed,) = again.sandboxes()
        self.assertEqual(listed.state, runtime.RUNNING)
        self.assertAlmostEqual(again._deadlines[handle.sandbox_id], time.time() + lifecycle.RESTART_SECONDS, delta=5)

    def test_a_sandbox_made_to_be_killed_gets_lizard_s_lifetime(self):
        handle = self.create(on_timeout="kill")
        (body,) = self.bodies("POST", "/api/sandboxes")
        self.assertEqual((body["timeoutMs"], body["metadata"]["sbx_on_timeout"]), (900_000, "kill"))
        self.backend.set_timeout(handle.sandbox_id, 300)
        self.assertEqual(self.bodies("POST", f"/api/sandboxes/{handle.sandbox_id}/timeout"), [{"timeoutMs": 300_000}])
        self.backend._deadlines[handle.sandbox_id] = time.time() - 1
        self.assertEqual([i.state for i in self.backend.sandboxes()], [runtime.RUNNING])   # Lizard ends it, not sbx

    def test_listing_filters_by_metadata_and_state_and_leaves_out_what_is_gone(self):
        mine = self.create()
        other = self.backend.create("tpl", timeout=900, metadata={"sbx_install": "inst-b"})
        self.fake.sandboxes[other.sandbox_id]["status"] = "paused"
        gone = self.backend.create("tpl", timeout=900, metadata={"sbx_install": "inst-a"})
        self.fake.sandboxes[gone.sandbox_id]["status"] = "deleted"
        self.assertEqual([i.sandbox_id for i in self.backend.sandboxes({"sbx_install": "inst-a"})], [mine.sandbox_id])
        self.assertEqual([i.sandbox_id for i in self.backend.sandboxes(None, [runtime.PAUSED])], [other.sandbox_id])
        with self.assertRaises(runtime.NotRunning):
            self.backend.info(gone.sandbox_id)
        with self.assertRaises(runtime.NotRunning):
            self.backend.info("sb-none")

    def test_pause_keeps_the_files_and_a_resume_is_a_fresh_boot(self):
        handle = self.create()
        self.assertEqual(self.backend.upstream_token(handle.sandbox_id, 3000), f"tok-{handle.sandbox_id}")
        self.backend.pause(handle.sandbox_id)
        self.backend.pause(handle.sandbox_id)   # one already paused stays paused
        self.assertEqual(len(self.bodies("POST", f"/api/sandboxes/{handle.sandbox_id}/pause")), 1)
        self.assertEqual(self.backend.info(handle.sandbox_id).state, runtime.PAUSED)
        self.assertEqual(self.backend.upstream_token(handle.sandbox_id, 3000, fetch=False), "")
        with self.assertRaises(runtime.NotRunning):   # a look never resumes
            self.backend.handle(handle.sandbox_id)
        self.fake.ran.clear()
        self.backend.connect(handle.sandbox_id, timeout=600)
        self.assertEqual(self.fake.boots[handle.sandbox_id], 2)
        self.assertEqual(self.scripts(), [lifecycle.MACHINE, lifecycle.BOOT])
        self.assertTrue(self.backend.cold_resume)
        self.fake.ran.clear()
        self.backend.connect(handle.sandbox_id)   # running: nothing is made again
        self.assertEqual(self.fake.ran, [])
        self.assertTrue(self.backend.kill(handle.sandbox_id))
        self.assertFalse(self.backend.kill(handle.sandbox_id))

    def test_a_pause_lizard_refuses_is_busy(self):
        handle = self.create()
        self.fake.fail("POST", r"/api/sandboxes/[^/]+/pause", 409, {"error": "Sandbox is busy"})
        with self.assertRaises(runtime.Busy):
            self.backend.pause(handle.sandbox_id)

    def test_commands_carry_the_user_and_env_outside_their_text(self):
        handle = self.create()
        self.fake.ran.clear()
        self.fake.respond = lambda sandbox_id, script: "hello\n"
        result = handle.commands.run("echo hello", envs={"TOKEN": "s3cret"}, user="root", cwd="/srv", timeout=30)
        self.assertEqual((result.stdout, result.exit_code), ("hello\n", 0))
        (_, cmd, user, envs), = self.fake.ran
        self.assertEqual((cmd[:2], user, envs), (["bash", "-c"], "root", {"TOKEN": "s3cret"}))
        self.assertEqual(cmd[2], f"[ -r {sandbox.TEMPLATE_FILE} ] && . {sandbox.TEMPLATE_FILE}; cd /srv || exit 1; "
                                 "echo hello")
        self.assertNotIn("s3cret", str(cmd))
        self.assertEqual(self.bodies("POST", f"/api/sandboxes/{handle.sandbox_id}/exec")[-1]["timeoutMs"], 30_000)
        handle.commands.run("true")
        self.assertEqual(self.fake.ran[-1][2], "user")   # the template's unprivileged account by default
        self.fake.respond = lambda sandbox_id, script: ("", 3)
        with self.assertRaises(runtime.CommandFailed) as raised:
            handle.commands.run("false")
        self.assertEqual(raised.exception.exit_code, 3)

    def test_a_command_past_lizard_s_ten_minutes_runs_detached_and_is_polled(self):
        handle = self.create()
        self.fake.ran.clear()
        self.fake.respond = lambda sandbox_id, script: "built\n"
        seen = []
        result = handle.commands.run("make", timeout=0, envs={"A": "1"}, on_output=seen.append)
        self.assertEqual((result.stdout, seen), ("built\n", ["built\n"]))
        launch = self.fake.ran[0]
        self.assertEqual((launch[1][2], launch[2], launch[3]), (sandbox.DETACH, "user", {"A": "1"}))
        self.assertTrue(launch[1][4].startswith(sandbox.RUN_DIR + "/"))
        self.assertTrue(all(body["timeoutMs"] <= 600_000
                            for body in self.bodies("POST", f"/api/sandboxes/{handle.sandbox_id}/exec")))
        self.fake.ran.clear()
        started = handle.commands.run("sleep 1000", background=True, timeout=0)
        started.disconnect()
        self.assertEqual(len(self.fake.ran), 1)   # started, not waited for
        self.fake.respond = lambda sandbox_id, script: ("", 2)
        with self.assertRaises(runtime.CommandFailed):
            handle.commands.run("make", timeout=3600)

    def test_a_command_on_a_sandbox_that_paused_is_not_running(self):
        handle = self.create()
        self.fake.sandboxes[handle.sandbox_id]["status"] = "paused"
        with self.assertRaises(runtime.NotRunning):
            handle.commands.run("true")
        with self.assertRaises(runtime.NotRunning):
            handle.files.read("/home/user/x")
        self.backend.kill(handle.sandbox_id)
        with self.assertRaises(runtime.NotRunning):
            handle.commands.run("true")

    def test_files_go_base64_as_the_user_and_large_ones_in_parts(self):
        handle = self.create()
        handle.files.write("/run/sbx/env", "A=1\n")
        handle.files.write("/etc/x", b"\x00\xff", user="root")
        self.assertEqual(self.fake.files[handle.sandbox_id]["/run/sbx/env"], (b"A=1\n", "user"))
        self.assertEqual(self.fake.files[handle.sandbox_id]["/etc/x"], (b"\x00\xff", "root"))
        self.assertEqual(handle.files.read("/run/sbx/env"), "A=1\n")
        self.assertEqual(handle.files.read("/etc/x", format="bytes"), b"\x00\xff")
        with self.assertRaises(errors.LizardError) as raised:
            handle.files.read("/none")
        self.assertTrue(raised.exception.gone())
        big = bytes(range(256)) * (sandbox.FILE_CHUNK // 256 * 2 + 1)
        handle.files.write("/home/user/big", big)
        self.assertEqual(handle.files.read("/home/user/big", format="bytes"), big)
        self.assertEqual([p for p in self.fake.files[handle.sandbox_id] if "upload" in p], [])

    def test_a_port_is_published_once_and_answers_only_with_the_sandbox_s_token(self):
        handle = self.create()
        self.assertEqual((self.backend.upstream_token_at.place, self.backend.upstream_token_at.name),
                         ("header", "X-Lizard-Access-Token"))
        self.assertEqual(self.backend.ingress, "https://sbx-ingress.sandbox.us-east-1.onlizard.com")
        self.assertEqual(self.backend.upstream_token(handle.sandbox_id, 3000, fetch=False), "")
        self.assertEqual(self.fake.exposed, {})   # a look never publishes
        for _ in range(2):
            self.assertEqual(self.backend.upstream_token(handle.sandbox_id, 3000), f"tok-{handle.sandbox_id}")
        self.assertEqual(len(self.bodies("POST", f"/api/sandboxes/{handle.sandbox_id}/expose/3000")), 1)
        self.assertEqual(self.backend.upstream_host(3000, handle.sandbox_id),
                         f"{handle.sandbox_id}-3000.sandbox.us-east-1.onlizard.com")
        self.assertEqual(self.backend.upstream_host(8080, "sb9"), "sb9-8080.sandbox.us-east-1.onlizard.com")
        self.assertIn(49983, self.backend.ports)
        self.backend.listed(set())
        self.assertEqual(self.backend.upstream_token(handle.sandbox_id, 3000, fetch=False), "")

    def test_a_port_without_a_token_or_outside_the_region_is_never_served(self):
        handle = self.create()
        self.fake.token = ""
        with self.assertRaisesRegex(runtime.Failed, "without a token"):
            self.backend.upstream_token(handle.sandbox_id, 3000)
        self.fake.token, self.fake.region_domain = "tok-", "sandbox.eu-west-lim-a.onlizard.com"
        with self.assertRaisesRegex(runtime.Failed, "LIZARD_REGION"):
            self.backend.upstream_token(handle.sandbox_id, 3001)
        elsewhere, fake = configured(LIZARD_REGION="eu-west-lim-a")
        self.assertEqual(elsewhere.ingress, "https://sbx-ingress.sandbox.eu-west-lim-a.onlizard.com")

    def test_the_base_template_is_replayed_on_lizard_s_base_at_the_installation_s_size_and_saved(self):
        older = self.fake.snapshot("base-1")
        spec = (runtime.TemplateSpec().from_image("ubuntu:24.04").set_user("root").run_cmd("apt-get install -y git")
                .set_envs({"LANG": "C.UTF-8"}).set_user("user").set_workdir("/home/user/app").run_cmd("make")
                .set_start_cmd("start-desktop", "true"))
        logs = []
        self.fake.respond = lambda sandbox_id, script: "Debian GNU/Linux 12\n" if "PRETTY_NAME" in script else ""
        self.assertEqual(self.backend.build_template(spec, alias="base-1", cpu=2, memory_mb=4096, disk_mb=8192,
                                                     on_log=logs.append), "base-1")
        (body,) = self.bodies("POST", "/api/sandboxes")
        self.assertEqual(body, {"projectId": "proj_1", "timeoutMs": build.BUILD_SECONDS * 1000, "template": "base",
                                "size": "small", "region": "us-east-1",
                                "metadata": {"sbx_install": "inst-a", "sbx_build": "base-1",
                                             "sbx_on_timeout": "kill"}})
        self.assertIn("Lizard: the base system is Debian GNU/Linux 12", logs)
        runs = [(self.fake.detached[cmd[4]]["script"], user) for _, cmd, user, _ in self.fake.ran
                if isinstance(cmd, list) and cmd[2] == sandbox.DETACH]
        self.assertEqual(runs[0], (sandbox.wrap("apt-get install -y git", "/home/user"), "root"))
        self.assertEqual(runs[1], (sandbox.wrap("make", "/home/user/app"), "user"))
        self.assertEqual(runs[2], (sandbox.wrap(f"bash {lifecycle.START_FILE}", "/"), "user"))
        self.assertTrue(any(build.SUDOERS in script for script in self.scripts()))
        saved = [s for s in self.fake.snapshots.values() if s["name"] == "base-1"]
        self.assertEqual(len(saved), 1)            # the older one of the name went
        self.assertNotEqual(saved[0]["id"], older)
        self.assertEqual(self.fake.sandboxes, {})  # the builder is deleted

    def test_a_template_runs_at_its_parent_s_size_and_one_that_asks_for_more_is_refused(self):
        spec = runtime.TemplateSpec().from_template("tpl").run_cmd("make").set_start_cmd("start", "true")
        self.backend.build_template(spec, alias="app", cpu=2, memory_mb=4096, disk_mb=8192, on_log=lambda _: None)
        body = self.bodies("POST", "/api/sandboxes")[0]
        self.assertEqual((body["snapshotId"], "size" in body, "template" in body), (self.snapshot, False, False))
        with self.assertRaisesRegex(runtime.LimitRefused, "LIZARD_MAX_CPU"):   # beyond the installation's size
            self.backend.build_template(spec, alias="app", cpu=4, memory_mb=8192, disk_mb=8192)
        bigger, fake = configured(LIZARD_MAX_CPU="4", LIZARD_MAX_MEMORY_MB="8192", LIZARD_MAX_DISK_MB="40960")
        fake.snapshot("tpl")   # taken from a Small sandbox, before the size was raised
        with patch.object(build, "_installation", return_value="inst-a"), \
                self.assertRaisesRegex(runtime.LimitRefused, "Rebuild the base template"):
            bigger.build_template(spec, alias="app", cpu=4, memory_mb=8192, disk_mb=8192, on_log=lambda _: None)
        self.assertEqual(fake.sandboxes, {})
        with self.assertRaisesRegex(runtime.Failed, "imports no image"):
            self.backend.build_template(runtime.TemplateSpec().from_image("ghcr.io/acme/app:1"), alias="x", cpu=2,
                                        memory_mb=4096, disk_mb=8192)
        with self.assertRaises(runtime.TemplateGone):
            self.backend.build_template(runtime.TemplateSpec().from_template("none"), alias="x", cpu=2,
                                        memory_mb=4096, disk_mb=8192)

    def test_a_snapshot_is_named_and_deleted_by_name(self):
        handle = self.create()
        self.assertEqual(self.backend.snapshot(handle.sandbox_id, "cache-1").snapshot_id, "cache-1")
        (body,) = self.bodies("POST", f"/api/sandboxes/{handle.sandbox_id}/snapshot")
        self.assertEqual(body["name"], "cache-1")
        self.assertIn("cache-1", dict(self.backend.templates()))
        self.assertTrue(self.backend.delete_template("cache-1"))
        self.assertFalse(self.backend.delete_template("cache-1"))

    def test_lizard_s_refusals_name_what_to_change(self):
        cases = (
            (429, {"error": "SANDBOX_LIMIT_REACHED", "message": "limit", "unit": "vcpu", "limit": 40, "running": 40},
             runtime.LimitRefused, "sandboxes", "40 of 40 vCPUs"),
            (429, {"error": "too fast", "code": "SANDBOX_RATE_LIMITED", "retryAfterS": 30}, runtime.LimitRefused,
             "starts", "30 s"),
            (403, {"error": "TRIAL_SANDBOX_LIFETIME", "message": "1 hour"}, runtime.LimitRefused, "timeout_seconds",
             "1 hour"),
            (403, {"error": "SANDBOX_SIZE_NOT_ALLOWED", "message": "no"}, runtime.LimitRefused, "cpu", "size"),
            (402, {"error": "PAYMENT_REQUIRED", "message": "Start Pro", "status": "trial_available",
                   "subscribeUrl": "https://lizard.build/billing"}, runtime.LimitRefused, "plan",
             "https://lizard.build/billing"),
        )
        for status, body, kind, field, said in cases:
            self.fake.fail("POST", "/api/sandboxes", status, body)
            with self.assertRaises(kind) as raised:
                self.create()
            self.assertEqual(raised.exception.field, field, body)
            self.assertIn(said, str(raised.exception))
        self.fake.fail("POST", "/api/sandboxes", 503, {"error": "paused", "code": "region_unavailable"})
        with self.assertRaises(runtime.NoRoom):
            self.create()
        self.assertEqual(self.fake.sandboxes, {})

    def test_a_read_is_sent_again_on_a_gateway_s_answer_and_a_create_never_is(self):
        handle = self.create()
        self.fake.fail("GET", r"/api/sandboxes/[^/]+", 502, b"<html>Bad Gateway</html>", times=2)
        self.assertEqual(self.backend.info(handle.sandbox_id).state, runtime.RUNNING)
        self.fake.fail("GET", r"/api/sandboxes/[^/]+", 502, b"<html>Bad Gateway</html>", times=3)
        with self.assertRaisesRegex(errors.LizardError, "502") as raised:
            self.backend.info(handle.sandbox_id)
        self.assertEqual((raised.exception.status, raised.exception.code), (502, ""))
        before = len(self.bodies("POST", "/api/sandboxes"))
        self.fake.fail("POST", "/api/sandboxes", 502, b"<html>Bad Gateway</html>")
        with self.assertRaises(errors.LizardError):
            self.create()
        self.assertEqual(len(self.bodies("POST", "/api/sandboxes")), before + 1)
        self.fake.failures.clear()
        self.backend.transport = lambda *request: (_ for _ in ()).throw(TimeoutError("timed out"))
        with self.assertRaises(runtime.Timeout):
            self.backend.kill(handle.sandbox_id)


class Settings(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(time, "sleep"))

    def test_presets_are_lizard_s_plans_and_the_defaults_the_small_size_on_pro(self):
        from sbx_control.runtime.lizard import Lizard
        self.assertEqual([p.name for p in Lizard.presets], ["trial", "pro", "pro-5x", "pro-20x"])
        trial, pro = Lizard.presets[0], Lizard.presets[1]
        self.assertEqual(trial.values, {"timeout_seconds": 3600, "cpu": 2, "memory_mb": 4096, "disk_mb": 20480,
                                        "sandboxes": 1})
        self.assertIn("deletes every sandbox an hour after it starts", trial.note)
        self.assertEqual(pro.values["sandboxes"], 20)   # 40 vCPU over Small's 2
        limits = Lizard().limits()
        self.assertEqual({f: getattr(limits, f) for f in pro.values}, dict(pro.values))
        self.assertEqual(set(Lizard.credential_keys), {s.key for s in Lizard.settings})
        self.assertEqual([s.key for s in Lizard.settings if s.secret], [Lizard.secret_key])
        self.assertEqual(Lizard.capabilities, runtime.Capabilities(snapshots=True))   # no egress rules, no image import
        self.assertEqual(plans.size(1, 1024, 8192), "mini")
        self.assertEqual(plans.size(2, 4096, 8192), "small")
        self.assertEqual(plans.size(8, 8192, 8192), "large")
        with self.assertRaises(runtime.LimitRefused):
            plans.size(32, 4096, 8192)

    def test_configure_needs_a_key_an_https_url_and_a_region_that_is_one(self):
        from sbx_control.runtime.lizard import Lizard
        for secrets, said in (({}, "LIZARD_API_KEY"),
                              ({"LIZARD_API_KEY": "k", "LIZARD_API_URL": "http://lizard.build"}, "https URL"),
                              ({"LIZARD_API_KEY": "k", "LIZARD_REGION": "us east/1"}, "LIZARD_REGION"),
                              ({"LIZARD_API_KEY": "k", "LIZARD_MAX_CPU": "64"}, "no size")):
            with self.assertRaisesRegex(SystemExit, said):
                Lizard().configure(secrets)
        backend = Lizard()
        with self.assertRaises(SystemExit):   # the region and limits apply without the key: nginx is written from them
            backend.configure({"LIZARD_REGION": "eu-west-lim-a", "LIZARD_MAX_TIMEOUT": "3600"})
        self.assertEqual((backend.ingress, backend.limits().timeout_seconds),
                         ("https://sbx-ingress.sandbox.eu-west-lim-a.onlizard.com", 3600))
        with self.assertRaisesRegex(runtime.Failed, "not configured"):
            backend.sandboxes()

    def test_the_project_is_the_one_named_or_the_only_one_the_key_sees(self):
        backend, fake = configured()
        self.assertEqual(backend.project(), "proj_1")
        backend.project()
        self.assertEqual(len([c for c in fake.calls if c[1] == "/api/projects"]), 1)   # asked once
        fake = FakeLizard()
        fake.projects.append({"id": "proj_2", "slug": "other", "name": "Other"})
        with self.assertRaisesRegex(runtime.Failed, "several projects.*main, other.*LIZARD_PROJECT"):
            configured(fake)[0].project()
        self.assertEqual(configured(fake, LIZARD_PROJECT="Other")[0].project(), "proj_2")
        with self.assertRaisesRegex(runtime.Failed, "no project 'gone'"):
            configured(fake, LIZARD_PROJECT="gone")[0].project()

    def test_status_and_the_check_say_the_trial_and_a_refused_key(self):
        from sbx_control import howto
        backend, fake = configured()
        fake.snapshot("tpl")
        (check,) = backend.test()
        self.assertEqual((check.ok, check.help_url), (True, ""))
        self.assertIn("project proj_1; plan pro; 1 snapshots", check.detail)
        fake.tier = "trial"
        checks = {c.name: c for c in backend.test()}
        self.assertEqual((checks["Lizard plan"].ok, checks["Lizard plan"].help_url), (False, howto.LIZARD_PLAN))
        with patch("sbx_control.runtime.sandboxes", return_value=[]):
            found = backend.status()
        self.assertEqual(found.state, "attention")
        self.assertIn("Trial: Lizard deletes every sandbox an hour after it starts", found.summary)
        refused = configured(LIZARD_API_KEY="liz_wrong")[0]
        (check,) = refused.test()
        self.assertEqual((check.ok, check.help_url), (False, howto.LIZARD_KEY))
        self.assertIn("401", check.detail)
        self.assertNotIn("liz_wrong", check.detail)


class Installed(Installation):
    """Lizard as an installation's runtime: the registry, isolation, what it refuses."""
    RUNTIME_ENV = LIZARD_ENV

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(time, "sleep"))
        self.backend, self.fake = configured()
        self.enterContext(patch.dict(runtime._backends, {"lizard": self.backend}))
        self.enterContext(patch.dict(runtime._index, clear=True))
        self.template = runtime.template_name("tpl", "lizard")
        self.fake.snapshot(self.template)

    def test_it_is_a_built_in_runtime_with_its_own_keys(self):
        self.assertIn("lizard", runtime.kinds())
        self.assertEqual(runtime.BUILTIN["lizard"], "sbx_control.runtime.lizard:Lizard")
        from sbx_control.runtime.boat import Boat
        from sbx_control.runtime.e2b_cloud import Cloud
        taken = {*Boat.credential_keys, *Cloud.credential_keys}
        self.assertFalse(taken & set(type(self.backend).credential_keys))   # two backends never share a key
        self.assertTrue(all(key.startswith("LIZARD_") for key in type(self.backend).credential_keys))

    def test_another_installations_sandboxes_and_templates_stay_its_own(self):
        self.assertRegex(self.template, r"^[0-9a-f]{8}--tpl$")
        handle = runtime.create(self.template, timeout=600, metadata={"sbx": "mine"})
        self.backend.create(self.template, timeout=600, metadata={"sbx": "theirs", "sbx_install": "someone-else"})
        self.assertEqual([i.sandbox_id for i in runtime.sandboxes()], [handle.sandbox_id])
        self.assertEqual(runtime.sandboxes()[0].runtime, "lizard")
        self.fake.snapshot("deadbeef--tpl")
        self.assertEqual([n for n, _ in runtime.templates("lizard")], [self.template])

    def test_untrusted_public_repos_are_refused_on_lizard(self):
        from sbx_control import reach, settings
        self.assertFalse(runtime.capabilities("lizard").egress_rules)
        self.assertFalse(reach.offered()["public_repos"])
        with self.assertRaisesRegex(Exception, "egress_rules"):
            settings.update("access", {**settings.current().access.model_dump(), "public_repos": True}, by="test")

    def test_a_resume_is_a_cold_boot_that_reships_the_runtime_and_restarts_the_app(self):
        from types import SimpleNamespace
        from sbx_control import core, sandbox_runtime
        handle = runtime.create(self.template, timeout=600, metadata={"sbx": "cold", "repo": "owner/app"})
        self.backend.pause(handle.sandbox_id)
        info = runtime.info(handle.sandbox_id)
        sbx = self.backend.connect(handle.sandbox_id)
        with patch.object(sandbox_runtime, "ensure_started") as started, \
                patch.object(sandbox_runtime, "initialize_ssh_identity"), \
                patch.object(sandbox_runtime, "runtime_values", return_value=({}, set(), SimpleNamespace())), \
                patch.object(sandbox_runtime, "write_runtime") as written, patch.object(core.extensions, "on_resume"):
            self.assertTrue(core.restore_runtime(sbx, info))   # True: the app restarts
        started.assert_called_once_with(sbx, "lizard")
        written.assert_called_once()
        self.assertEqual(runtime.desktop("lizard"), "sbx")   # sbx's own noVNC, published like any port
