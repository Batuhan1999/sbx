"""Beam as a sandbox runtime, against a fake of Beam's Gateway HTTP API
(fake_beam) behind the backend's real requests: sandboxes from a template's
image, their metadata kept in the sandbox, commands and files, the timeout,
the pause Beam does not have, previews, templates as images, Beam's errors
as sbx's, and its settings. Nothing here reaches Beam itself."""
import json
import os
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch

from fake_beam import TOKEN, FakeBeam, configured

from sbx_control import runtime
from sbx_control.runtime import CommandFailed, Failed, NotRunning, TemplateGone, TemplateSpec, Timeout


class Case(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(time, "sleep"))
        self.index = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory())) / "templates.json"
        self.backend, self.fake = configured(index=self.index)
        self.fake.images["img-tpl"] = {"/home/user/app/README": b"hi"}
        self.backend.index().put("tpl", {"image": "img-tpl", "cpu": 2, "memory_mb": 4096, "start": False})

    def create(self, **metadata):
        return self.backend.create("tpl", timeout=900, envs={"SBX_NAME": "box"},
                                   metadata={"sbx": "box", "sbx_install": "inst-a", **metadata})

    def sent(self, method, path):
        return [body for m, p, _, body in self.fake.calls if m == method and p == path]


class Sandboxes(Case):
    def test_create_makes_a_stub_of_the_templates_image_and_size(self):
        handle = self.create()
        stub = self.sent("POST", "/stubs")[0]
        self.assertEqual((stub["imageId"], stub["stubType"], stub["cpu"], stub["memory"], stub["keepWarmSeconds"]),
                         ("img-tpl", "sandbox", 2000, 4096, 900))   # millicores, MiB, the TTL
        self.assertEqual((stub["env"], stub["entrypoint"], stub["forceCreate"]),
                         (["SBX_NAME=box"], ["tail", "-f", "/dev/null"], True))
        self.assertEqual(self.sent("POST", "/pods"), [{"stubId": "stub-1"}])
        info = self.backend.info(handle.sandbox_id)
        self.assertEqual((info.state, info.template_id, info.cpu_count, info.memory_mb),
                         (runtime.RUNNING, "tpl", 2, 4096))
        self.assertEqual(info.metadata, {"sbx": "box", "sbx_install": "inst-a", "sbx_template": "tpl"})

    def test_every_request_carries_this_backends_token_and_none_from_the_environment(self):
        seen = []
        inner = self.backend.transport
        self.backend.transport = lambda method, url, headers, data, timeout: (
            seen.append(headers.get("Authorization")), inner(method, url, headers, data, timeout))[1]
        with patch.dict(os.environ, {"BEAM_TOKEN": "from-the-environment", "BETA9_TOKEN": "also-not"}):
            self.create()
        api_calls = [value for value in seen if value is not None]
        self.assertTrue(api_calls)
        self.assertEqual(set(api_calls), {f"Bearer {TOKEN}"})

    def test_a_template_without_an_image_is_gone(self):
        with self.assertRaises(TemplateGone) as raised:
            self.backend.create("deleted", timeout=600)
        self.assertEqual(raised.exception.name, "deleted")

    def test_a_template_whose_image_beam_lost_is_gone_and_forgotten(self):
        del self.fake.images["img-tpl"]
        with self.assertRaises(TemplateGone):
            self.create()
        self.assertEqual(self.backend.templates(), [])   # the scheduler builds it again

    def test_metadata_lives_in_the_sandbox_and_is_read_back_after_a_restart(self):
        mine = self.create().sandbox_id
        self.backend.create("tpl", timeout=900, metadata={"sbx": "other", "sbx_install": "inst-b"})
        self.fake.boxes["endpoint-1"] = {"stub": "stub-x", "status": "RUNNING", "foreign": True}   # not a sandbox
        again, _ = configured(self.fake, index=self.index)   # a restart: nothing kept in memory
        listed = again.sandboxes({"sbx_install": "inst-a"})
        self.assertEqual([i.sandbox_id for i in listed], [mine])
        self.assertEqual(listed[0].metadata["sbx"], "box")
        self.assertEqual(len(again.sandboxes()), 2)        # both installations', never the foreign container
        before = len(self.fake.calls)
        again.sandboxes()
        self.assertEqual(len(self.fake.calls) - before, 1)   # one listing: metadata and the foreign one are kept

    def test_a_listing_beam_does_not_answer_is_an_error_not_an_empty_list(self):
        self.create()
        again, _ = configured(self.fake, index=self.index)
        self.fake.fail("GET", "/containers", 502, {"message": "bad gateway"}, times=3)
        with self.assertRaises(Failed):
            again.sandboxes()

    def test_beam_has_no_paused_sandboxes_and_refuses_a_pause(self):
        handle = self.create()
        self.assertFalse(self.backend.capabilities.pause)
        self.assertEqual(self.backend.sandboxes(states=[runtime.PAUSED]), [])
        with self.assertRaises(Failed) as raised:
            self.backend.pause(handle.sandbox_id)
        self.assertIn("can't pause", str(raised.exception))
        self.assertNotIsInstance(raised.exception, runtime.Busy)   # not "try again": it never will
        self.assertEqual(self.backend.info(handle.sandbox_id).state, runtime.RUNNING)
        with self.assertRaises(NotRunning):
            self.backend.pause("sandbox-gone")

    def test_the_timeout_is_beams_ttl(self):
        handle = self.create()
        self.backend.set_timeout(handle.sandbox_id, 1800)
        self.assertEqual(self.fake.boxes[handle.sandbox_id]["ttl"], 1800)
        self.backend.connect(handle.sandbox_id, timeout=600)
        self.assertEqual(self.fake.boxes[handle.sandbox_id]["ttl"], 600)

    def test_a_sandbox_beam_no_longer_lists_is_not_running(self):
        handle = self.create()
        del self.fake.boxes[handle.sandbox_id]   # its TTL passed
        for call in (lambda: self.backend.set_timeout(handle.sandbox_id, 600),
                     lambda: self.backend.connect(handle.sandbox_id),
                     lambda: self.backend.info(handle.sandbox_id),
                     lambda: handle.commands.run("echo hi")):
            with self.assertRaises(NotRunning):
                call()

    def test_kill_stops_the_container(self):
        handle = self.create()
        self.assertTrue(self.backend.kill(handle.sandbox_id))
        self.assertFalse(self.backend.kill(handle.sandbox_id))
        self.assertEqual(self.backend.sandboxes(), [])

    def test_a_stop_beam_refuses_for_a_sandbox_it_still_lists_is_an_error_not_a_delete(self):
        handle = self.create()
        self.fake.fail("POST", f"/containers/{handle.sandbox_id}/stop", 200, {"ok": False, "errorMsg": "busy"})
        with self.assertRaises(Failed):
            self.backend.kill(handle.sandbox_id)
        self.assertIn(handle.sandbox_id, self.fake.boxes)

    def test_what_it_kept_of_a_sandbox_goes_once_beam_no_longer_lists_it(self):
        handle = self.create()
        self.backend.upstream_host(3000, handle.sandbox_id)
        del self.fake.boxes[handle.sandbox_id]   # its TTL passed
        self.assertEqual(self.backend.sandboxes(), [])
        self.assertEqual((self.backend._meta, self.backend._stubs, self.backend._urls.keys()), ({}, {}, []))

    def test_a_create_that_fails_half_way_leaves_no_sandbox_running(self):
        self.fake.respond = lambda box, command: ("", 1) if command.startswith("mkdir -p /etc/sbx && mv") else None
        with self.assertRaises(CommandFailed):
            self.create()
        self.assertEqual(self.fake.boxes, {})


class CommandsAndFiles(Case):
    def test_a_command_runs_as_the_default_user_with_its_env_out_of_its_text(self):
        handle = self.create()
        result = handle.commands.run("echo hello", envs={"API_TOKEN": "s3cret"})
        self.assertEqual((result.stdout, result.exit_code), ("hello\n", 0))
        box, user, cwd, command, env = self.fake.ran[-1]
        self.assertEqual((user, command, env), ("default", "echo hello", {"API_TOKEN": "s3cret"}))
        sent = self.sent("POST", f"/pods/{handle.sandbox_id}/exec")[-1]
        self.assertNotIn("s3cret", sent["command"])
        self.assertFalse(sent["wait"])

    def test_a_command_as_another_user_and_in_a_folder(self):
        handle = self.create()
        handle.commands.run("echo hi", user="root", cwd="/srv/app")
        self.assertEqual(self.fake.ran[-1][1:3], ("root", "/srv/app"))
        handle.commands.run("echo hi", user="deploy")
        self.assertEqual(self.fake.ran[-1][1], "deploy")

    def test_a_failed_command_raises_with_its_code_and_output(self):
        handle = self.create()
        with self.assertRaises(CommandFailed) as raised:
            handle.commands.run("exit 3")
        self.assertEqual((raised.exception.exit_code, raised.exception.stderr), (3, "failed\n"))

    def test_a_command_out_of_time_is_killed(self):
        handle = self.create()
        self.fake.hang = "sleep 999"
        clock = iter(range(0, 10_000, 5))
        with patch.object(time, "monotonic", lambda: next(clock)), self.assertRaises(Timeout):
            handle.commands.run("sleep 999", timeout=20)
        self.assertTrue(self.sent("POST", f"/pods/{handle.sandbox_id}/kill"))

    def test_a_background_command_returns_once_started(self):
        handle = self.create()
        self.fake.hang = "sleep 999"
        handle.commands.run("sleep 999", background=True, timeout=0).disconnect()
        self.assertFalse([c for c in self.fake.calls if c[1].endswith("/status")
                          and c[2].get("pid") == str(self.fake._n)])

    def test_files_are_uploaded_in_chunks_moved_into_place_and_read_back(self):
        from sbx_control.runtime.beam import sandbox
        handle = self.create()
        data = bytes(range(256)) * 5
        with patch.object(sandbox, "CHUNK", 500):
            handle.files.write("/home/user/app/blob.bin", data)
        uploads = self.sent("POST", f"/pods/{handle.sandbox_id}/files/upload")[1:]   # after the metadata's
        self.assertEqual([u["offset"] for u in uploads], [0, 500, 1000])
        self.assertTrue(all(u["containerPath"].startswith("/tmp/.sbx-upload-") for u in uploads))
        self.assertEqual(handle.files.read("/home/user/app/blob.bin", format="bytes"), data)
        self.assertEqual(handle.files.read("/home/user/app/README"), "hi")
        with self.assertRaises(CommandFailed):
            handle.files.read("/home/user/missing")


class Previews(Case):
    def test_a_port_is_exposed_on_its_first_request_and_its_host_kept(self):
        handle = self.create()
        host = self.backend.upstream_host(3000, handle.sandbox_id)
        self.assertEqual(host, "stub-1-3000.app.beam.cloud")
        self.assertEqual(self.backend.upstream_host(3000, handle.sandbox_id), host)
        exposed = self.sent("POST", f"/pods/{handle.sandbox_id}/ports/expose")
        self.assertEqual(exposed, [{"stubId": "stub-1", "port": 3000}])
        self.assertEqual(self.backend.ingress, "https://sbx-ingress.app.beam.cloud")
        self.assertIsNone(self.backend.upstream_token_at)   # Beam's URLs take no token: they are public
        self.assertEqual(self.backend.upstream_token(handle.sandbox_id, 3000), "")

    def test_a_url_outside_beams_preview_domain_is_refused(self):
        handle = self.create()
        self.fake.preview_domain = "elsewhere.example"
        with self.assertRaises(Failed) as raised:
            self.backend.upstream_host(3000, handle.sandbox_id)
        self.assertIn("app.beam.cloud", str(raised.exception))

    def test_a_deleted_sandbox_keeps_no_host(self):
        handle = self.create()
        self.backend.upstream_host(3000, handle.sandbox_id)
        self.backend.kill(handle.sandbox_id)
        with self.assertRaises(NotRunning):
            self.backend.upstream_host(3000, handle.sandbox_id)


class Templates(Case):
    def build(self, spec, alias="base", **size):
        self.log = []
        return self.backend.build_template(spec, alias=alias, **{"cpu": 2, "memory_mb": 4096, "disk_mb": 8192, **size},
                                           on_log=self.log.append)

    def test_a_template_from_an_image_is_replayed_in_a_builder_and_saved_as_an_image(self):
        spec = (TemplateSpec().from_image("ubuntu:24.04").set_user("root").run_cmd("apt-get install -y git")
                .set_envs({"LANG": "C.UTF-8"}).set_workdir("/home/user").set_user("user").run_cmd("echo built"))
        self.assertEqual(self.build(spec), "base")
        verify = self.sent("POST", "/images/verify-build")[0]
        self.assertEqual(verify, {"existingImageUri": "docker.io/library/ubuntu:24.04", "ignorePython": True})
        self.assertFalse(self.sent("POST", "/images/build"))   # Beam has that image already
        runs = [(user, cwd, command) for _, user, cwd, command, _ in self.fake.ran
                if command in ("apt-get install -y git", "echo built")]
        self.assertEqual(runs, [("root", "/", "apt-get install -y git"), ("user", "/home/user", "echo built")])
        entry = self.backend.index().get("base")
        self.assertEqual((entry["cpu"], entry["memory_mb"], entry["start"]), (2, 4096, False))
        template = self.fake.images[entry["image"]]["/etc/sbx/template.sh"].decode()
        self.assertIn("LANG=", template)
        self.assertIn("export SBX_TEMPLATE_WORKDIR=/home/user", template)
        self.assertEqual(self.fake.boxes, {})                  # the builder is deleted
        self.assertEqual(self.backend.templates(), [("base", "ready"), ("tpl", "ready")])
        self.assertEqual(json.loads(self.index.read_text())["base"]["image"], entry["image"])
        self.assertEqual(self.index.stat().st_mode & 0o777, 0o600)

    def test_an_image_beam_does_not_have_is_imported_first(self):
        self.build(TemplateSpec().from_image("ghcr.io/acme/base:1").run_cmd("true"))
        self.assertEqual(self.sent("POST", "/images/build")[0]["existingImageUri"], "ghcr.io/acme/base:1")
        self.assertIn("pulling", self.log)
        self.assertEqual(self.sent("POST", "/stubs")[0]["imageId"], "img-new")

    def test_a_template_on_another_inherits_its_env_and_its_start_command(self):
        self.fake.respond = lambda box, command: self.fake.boxes[box]["listening"].add(6080) \
            if command.startswith("exec bash /etc/sbx/start.sh") else None
        self.build(TemplateSpec().from_image("ubuntu:24.04").set_envs({"A": "1"}).set_start_cmd("start-desktop", 6080))
        self.assertTrue(self.backend.index().get("base")["start"])
        self.build(TemplateSpec().from_template("base").set_envs({"B": "2"}).run_cmd("echo child"), alias="child")
        child = self.backend.index().get("child")
        self.assertTrue(child["start"])
        template = self.fake.images[child["image"]]["/etc/sbx/template.sh"].decode()
        self.assertLess(template.index("A="), template.index("B="))
        handle = self.backend.create("child", timeout=600, metadata={"sbx_install": "inst-a"})
        self.assertTrue(self.fake.boxes[handle.sandbox_id]["started"])   # a container has no init: create starts it

    def test_a_failed_step_fails_the_build_and_deletes_the_builder(self):
        with self.assertRaises(CommandFailed):
            self.build(TemplateSpec().from_image("ubuntu:24.04").run_cmd("exit 2"))
        self.assertEqual(self.fake.boxes, {})
        self.assertIsNone(self.backend.index().get("base"))

    def test_a_missing_parent_and_a_size_beyond_the_limits_are_refused_before_a_builder_starts(self):
        with self.assertRaises(TemplateGone):
            self.build(TemplateSpec().from_template("nope").run_cmd("true"))
        with self.assertRaises(runtime.LimitRefused) as raised:
            self.build(TemplateSpec().from_image("ubuntu:24.04"), cpu=16)
        self.assertEqual((raised.exception.field, raised.exception.allowed), ("cpu", 8))
        self.assertEqual(self.sent("POST", "/pods"), [])

    def test_a_snapshot_is_an_image_new_sandboxes_start_from(self):
        handle = self.create()
        handle.files.write("/home/user/app/state", "kept")
        found = self.backend.snapshot(handle.sandbox_id, "snap-1")
        self.assertEqual(found.snapshot_id, "snap-1")
        again = self.backend.create("snap-1", timeout=600, metadata={"sbx_install": "inst-a"})
        self.assertEqual(again.files.read("/home/user/app/state"), "kept")
        self.assertEqual(self.sent("POST", "/stubs")[-1]["cpu"], 2000)   # the size of the template it came from
        self.backend.delete_snapshot("snap-1")
        self.assertFalse(self.backend.delete_template("snap-1"))
        self.assertTrue(self.backend.delete_template("tpl"))


class ErrorsAndSettings(Case):
    def test_a_refused_token_fails_the_check_with_the_way_to_fix_it(self):
        backend, _ = configured(self.fake, index=self.index, BEAM_API_KEY="wrong")
        check = backend.test()[0]
        self.assertFalse(check.ok)
        self.assertIn("Invalid token", check.detail)
        self.assertIn("/howto/beam.md#api-token", check.help_url)
        with self.assertRaises(Failed) as raised:
            backend.sandboxes()
        self.assertTrue(raised.exception.unauthorized())
        self.assertTrue(backend.failure(raised.exception))

    def test_the_check_names_the_workspace(self):
        check = self.backend.test()[0]
        self.assertTrue(check.ok)
        self.assertIn("ws-1", check.detail)

    def test_a_read_is_sent_again_after_a_gateway_error_and_a_write_is_not(self):
        self.fake.fail("GET", "/containers", 503, {"message": "unavailable"}, times=2)
        self.assertEqual(self.backend.sandboxes(), [])
        self.fake.fail("POST", "/stubs", 503, {"message": "unavailable"})
        with self.assertRaises(Failed):
            self.create()
        self.assertEqual(len(self.sent("POST", "/stubs")), 1)

    def test_a_sandbox_still_starting_is_waited_for(self):
        handle = self.create()
        self.fake.fail("POST", f"/pods/{handle.sandbox_id}/exec", 200,
                       {"ok": False, "errorMsg": "sandbox process manager is not ready"}, times=3)
        self.assertEqual(handle.commands.run("echo up").stdout, "up\n")
        self.fake.fail("POST", f"/pods/{handle.sandbox_id}/exec", 200,
                       {"ok": False, "errorMsg": "sandbox process manager is not ready"})
        with self.assertRaises(Failed):
            self.backend.handle(handle.sandbox_id).commands.run("echo look")   # a look never waits

    def test_its_settings_are_its_own_keys_and_the_generic_form_has_them(self):
        from sbx_control.runtime.beam import Beam
        self.assertEqual(runtime.BUILTIN["beam"], "sbx_control.runtime.beam:Beam")
        self.assertIn("beam", runtime.kinds())
        keys = [s.key for s in Beam.settings]
        self.assertEqual(keys[:2], ["BEAM_API_KEY", "BEAM_API_URL"])
        self.assertTrue(set(keys) <= set(Beam.credential_keys))
        self.assertTrue(all(key.startswith("BEAM_") for key in Beam.credential_keys))
        self.assertTrue(Beam.settings[0].secret and Beam.settings[1].rekey and Beam.settings[1].hidden)
        limits = self.backend.limits()
        self.assertEqual((limits.cpu, limits.memory_mb, limits.sandboxes, limits.disk_mb), (8, 16384, 10, None))
        backend, _ = configured(self.fake, index=self.index, BEAM_MAX_SANDBOXES="3", BEAM_MAX_TIMEOUT="3600")
        self.assertEqual((backend.limits().sandboxes, backend.limits().timeout_seconds), (3, 3600))

    def test_configure_refuses_no_token_and_an_api_url_that_is_not_https(self):
        from sbx_control.runtime.beam import Beam
        with self.assertRaises(SystemExit) as raised:
            Beam().configure({})
        self.assertIn("BEAM_API_KEY", str(raised.exception))
        with self.assertRaises(SystemExit):
            Beam().configure({"BEAM_API_KEY": TOKEN, "BEAM_API_URL": "http://beam.internal/api"})

    def test_a_stream_that_reports_an_error_fails_the_import(self):
        self.fake.registry.clear()
        real = FakeBeam._route

        def route(fake, method, path, query, body):
            if path == "/images/build":
                return json.dumps({"error": {"code": 13, "message": "registry unreachable"}}).encode()
            return real(fake, method, path, query, body)
        with patch.object(FakeBeam, "_route", route), self.assertRaises(Failed) as raised:
            self.backend.build_template(TemplateSpec().from_image("ubuntu:24.04"), alias="base", cpu=1,
                                        memory_mb=1024, disk_mb=1024, on_log=lambda line: None)
        self.assertIn("registry unreachable", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
