"""Boat as a sandbox runtime, against a fake Boat API behind the real SDK
(fake_boat): sandboxes and their labels, states, pause and the cold-boot
resume, commands and files, templates as saved snapshots, the per-port
preview token, Boat's desktop, its refusals as sbx's, and its settings."""
import base64
import shlex
import time
import unittest
from unittest.mock import patch

from fake_boat import configured

from sbx_control.runtime import dockerfile
from support import RUNTIME, Installation

BOAT_ENV = RUNTIME + "SBX_RUNTIMES=boat\nBOAT_API_KEY=boat_unit-test\n"


class Backend(unittest.TestCase):
    def setUp(self):
        from sbx_control.runtime import boat
        self.boat_module = boat
        self.enterContext(patch.object(time, "sleep"))
        self.enterContext(patch.object(boat.build, "_installation", return_value="inst-a"))
        self.backend, self.fake = configured()
        self.fake.saved["tpl"] = {"name": "tpl", "status": "ready", "sourceSandboxId": "x", "type": "default",
                                  "createdAt": "2026-11-17T12:00:00Z", "files": {"/home/user/app/README": b"hi"}}

    def create(self, **metadata):
        return self.backend.create("tpl", timeout=900, envs={"SBX_NAME": "box"},
                                   metadata={"sbx": "box", "sbx_install": "inst-a", **metadata})

    def body(self, method, path):
        return next(body for m, p, _, body in self.fake.calls if m == method and p == path)

    def test_create_labels_the_sandbox_and_gives_it_no_account_secrets(self):
        handle = self.create(owner="dev@example.com")
        sent = self.body("POST", "/sandboxes")
        self.assertEqual(sent["from"], "tpl")
        self.assertEqual(sent["labels"], {"sbx": "box", "sbx_install": "inst-a", "owner": "dev@example.com",
                                          "sbx_template": "tpl"})   # Boat lists no template: a label says it
        self.assertEqual((sent["noEnv"], sent["ttlSeconds"], sent["env"]), (True, 900, {"SBX_NAME": "box"}))
        self.assertNotIn("type", sent)        # the template's own size
        self.assertNotIn("environment", sent)  # none of the account's environments
        self.assertEqual(self.backend.info(handle.sandbox_id).template_id, "tpl")

    def test_a_handle_is_a_look_that_asks_boat_nothing(self):
        handle = self.create()
        before = len(self.fake.calls)
        self.assertEqual(self.backend.handle(handle.sandbox_id).sandbox_id, handle.sandbox_id)
        self.assertEqual(len(self.fake.calls), before)   # no resume, no TTL, no mount: the gate's probe per port

    def test_a_missing_template_is_gone(self):
        from sbx_control.runtime import TemplateGone
        with self.assertRaises(TemplateGone) as raised:
            self.backend.create("deleted", timeout=600)
        self.assertEqual(raised.exception.name, "deleted")

    def test_listing_filters_by_label_and_reads_every_page(self):
        for n in range(5):
            self.create(sbx=f"box{n}")
        self.backend.create("tpl", timeout=600, metadata={"sbx": "other", "sbx_install": "inst-b"})
        self.fake.page_size = 2
        found = self.backend.sandboxes({"sbx_install": "inst-a"})
        self.assertEqual(sorted(i.metadata["sbx"] for i in found), [f"box{n}" for n in range(5)])
        listed = [q for m, p, q, _ in self.fake.calls if m == "GET" and p == "/sandboxes"]
        self.assertEqual(listed[-1]["label"], ["sbx_install=inst-a"])   # Boat filters
        self.assertEqual(len(listed), 3)                                 # 2 + 2 + 1

    def test_states_map_to_running_paused_and_failed(self):
        from sbx_control.runtime import FAILED, PAUSED, RUNNING
        handle = self.create()
        box = self.fake.sandboxes[handle.sandbox_id]
        for state, expected in (("provisioning", RUNNING), ("ready", RUNNING), ("idle", RUNNING),
                                ("running", RUNNING), ("archiving", PAUSED), ("archived", PAUSED), ("error", FAILED)):
            box["state"] = state
            self.assertEqual(self.backend.info(handle.sandbox_id).state, expected, state)
        box["state"] = "cancelled"   # never got a machine: gone
        from sbx_control.runtime import NotRunning
        with self.assertRaises(NotRunning):
            self.backend.info(handle.sandbox_id)
        self.assertEqual(self.backend.sandboxes(), [])
        self.assertEqual(self.backend.sandboxes(None, [RUNNING]), [])
        listed = [q for m, p, q, _ in self.fake.calls if m == "GET" and p == "/sandboxes"]
        self.assertIn("ready", listed[-1]["state"][0])

    def test_pause_is_a_stop_and_resume_a_cold_boot(self):
        from sbx_control.runtime import PAUSED, RUNNING
        handle = self.create()
        self.backend.pause(handle.sandbox_id)
        self.assertEqual(self.backend.info(handle.sandbox_id).state, PAUSED)
        self.backend.pause(handle.sandbox_id)   # paused stays paused
        self.backend.connect(handle.sandbox_id, timeout=1200)
        self.assertEqual(self.backend.info(handle.sandbox_id).state, RUNNING)
        self.assertEqual(self.fake.boots[handle.sandbox_id], 2)
        # Boat's /run is noexec: every machine gets /run/sbx as an executable tmpfs (the recipe's scripts run there).
        mounts = [c for s, c, _ in self.fake.ran if s == handle.sandbox_id and "mount -t tmpfs" in c]
        self.assertEqual(len(mounts), 2)
        self.assertNotIn("noexec", mounts[0])
        self.assertEqual(self.body("POST", f"/sandboxes/{handle.sandbox_id}/resume")["ttlSeconds"], 1200)
        self.assertTrue(self.backend.cold_resume)

    def test_set_timeout_patches_the_ttl_and_refuses_a_stopped_sandbox(self):
        from sbx_control.runtime import NotRunning
        handle = self.create()
        self.backend.set_timeout(handle.sandbox_id, 3600)
        self.assertEqual(self.fake.sandboxes[handle.sandbox_id]["ttlSeconds"], 3600)
        self.fake.sandboxes[handle.sandbox_id]["state"] = "archived"
        with self.assertRaises(NotRunning):
            self.backend.set_timeout(handle.sandbox_id, 3600)

    def test_kill_deletes_with_the_confirmation_and_says_when_nothing_was_there(self):
        handle = self.create()
        self.assertTrue(self.backend.kill(handle.sandbox_id))
        self.assertNotIn(handle.sandbox_id, self.fake.sandboxes)
        self.assertFalse(self.backend.kill(handle.sandbox_id))

    def test_commands_carry_env_user_and_workdir_and_long_ones_run_detached(self):
        from sbx_control.runtime import CommandFailed
        handle = self.create()
        self.fake.respond = lambda _, command: ("oops", 3) if "fail" in command else "out\n"
        secret = "orb_test-only-secret-value"
        self.assertEqual(handle.commands.run("echo x", envs={"A": "b c", "ORB_TOKEN": secret}, user="root",
                                             cwd="/srv").stdout, "out\n")
        sent = self.fake.ran[-1][1]
        self.assertNotIn(secret, sent)   # the env never in the command text Boat keeps
        self.assertIn("exec sudo -H -u root bash", sent)
        written = [v.decode() for p, v in self.fake.files[handle.sandbox_id].items() if p.startswith("/tmp/.sbx-env/")]
        self.assertEqual(written, [f"export A='b c'\nexport ORB_TOKEN={secret}\n"])
        inner = shlex.split(sent)[-1]
        self.assertIn("rm -f /tmp/.sbx-env-", inner)            # read once, removed
        self.assertIn("cd /srv || exit 1; echo x", inner)        # a failed cd stops it
        with self.assertRaises(CommandFailed) as raised:
            handle.commands.run("fail now")
        self.assertEqual(raised.exception.exit_code, 3)
        self.assertEqual(handle.commands.run("make all", timeout=0).stdout, "out\n")   # no limit: detached, polled
        self.assertTrue(self.fake.ran[-1][2])
        started = handle.commands.run("server", background=True, timeout=0)
        started.disconnect()
        self.assertTrue(self.fake.ran[-1][2])

    def test_files_outside_boats_folders_are_staged_and_moved_and_big_ones_use_signed_urls(self):
        handle = self.create()
        handle.files.write("/run/sbx/env", "export A=1\n")
        self.assertEqual(self.fake.files[handle.sandbox_id]["/run/sbx/env"], b"export A=1\n")
        self.assertEqual(handle.files.read("/run/sbx/env"), "export A=1\n")
        big = b"x" * (5 * 1024 * 1024 + 1)
        handle.files.write("/home/user/big.bin", big)
        self.assertTrue(any(p.endswith("/files/upload-url") for _, p, _, _ in self.fake.calls))
        self.assertEqual(handle.files.read("/home/user/big.bin", format="bytes"), big)
        self.assertTrue(any(p.endswith("/files/download-url") for _, p, _, _ in self.fake.calls))

    def test_a_staged_file_waits_where_only_its_user_reads_and_goes_when_the_move_fails(self):
        from sbx_control.runtime import CommandFailed
        handle = self.create()
        self.fake.respond = lambda _, command: ("mv: cannot move", 1) if "mv -f" in command else None
        with self.assertRaises(CommandFailed):
            handle.files.write("/run/sbx/env", "export TOKEN=x\n")
        staged = next(p for p in self.fake.files[handle.sandbox_id] if "upload-" in p)
        self.assertTrue(staged.startswith("/tmp/.sbx-env/"), staged)   # 0700, not /tmp itself
        self.assertIn(f"|| {{ rm -f {staged}; exit 1; }}", self.fake.ran[-1][1])

    def test_a_ports_token_is_fetched_once_exposed_private_and_forgotten_on_stop(self):
        handle = self.create()
        sandbox_id = handle.sandbox_id
        self.assertEqual(self.backend.upstream_token(sandbox_id, 3000, fetch=False), "")   # nothing asked yet
        token = self.backend.upstream_token(sandbox_id, 3000)
        self.assertEqual(token, f"tok-{sandbox_id}-3000")
        self.assertEqual(self.backend.upstream_token(sandbox_id, 3000), token)
        hosts = [b for m, p, _, b in self.fake.calls if p.endswith("/host")]
        self.assertEqual(len(hosts), 1)
        self.assertEqual(hosts[0]["public"], False)   # never public
        self.assertEqual(self.backend.upstream_host(3000, sandbox_id), f"word-{sandbox_id[3:]}-3000.on.boat.dev")
        self.assertNotEqual(self.backend.upstream_token(sandbox_id, 5173), token)   # one per port
        self.backend.pause(sandbox_id)
        self.assertEqual(self.backend.upstream_token(sandbox_id, 3000, fetch=False), "")

    def test_a_port_is_looked_at_again_until_its_app_listens_and_the_relay_survives_a_resume(self):
        sandbox_id = self.create().sandbox_id
        listening = {"5173": ""}
        self.fake.respond = lambda _, command: (listening["5173"] if "sport = :5173" in command else None)
        clock = [100.0]
        self.enterContext(patch.object(time, "monotonic", side_effect=lambda: clock[0]))
        self.backend.upstream_token(sandbox_id, 5173)            # nothing listens yet: exposed as reachable
        listening["5173"] = "LISTEN 0 511 127.0.0.1:5173 0.0.0.0:*\n"
        self.backend.upstream_token(sandbox_id, 5173)            # within a few seconds: not asked again
        clock[0] += 5
        self.backend.upstream_token(sandbox_id, 5173)            # looked at again: loopback only, relayed
        clock[0] += 60
        self.backend.upstream_token(sandbox_id, 5173)            # it answered: settled, nothing asked
        hosts = [b["localhost"] for _, p, _, b in self.fake.calls if p.endswith("/host")]
        self.assertEqual(hosts, [False, True])
        self.backend.pause(sandbox_id)
        self.backend.connect(sandbox_id)
        listening["5173"] = ""                                   # after the resume, before the app is up again
        self.backend.upstream_token(sandbox_id, 5173)
        hosts = [b["localhost"] for _, p, _, b in self.fake.calls if p.endswith("/host")]
        self.assertEqual(hosts, [False, True, True])             # the relay kept, not turned off

    def test_an_exposure_under_way_when_the_sandbox_stops_is_not_kept(self):
        import threading
        sandbox_id = self.create().sandbox_id
        self.fake.respond = lambda _, command: ("LISTEN 0 511 0.0.0.0:3000 0.0.0.0:*\n"
                                                if "sport = :3000" in command else None)
        expose, stops = self.backend._expose, []

        def stopped_meanwhile(*args):
            stop = threading.Thread(target=self.backend._forget, args=(sandbox_id,))
            stop.start()
            stop.join(0.3)   # a stop's reset, while Boat answers the exposure
            stops.append(stop)
            return expose(*args)
        with patch.object(self.backend, "_expose", side_effect=stopped_meanwhile):
            self.backend.upstream_token(sandbox_id, 3000)
        stops[0].join()
        self.assertEqual(self.backend.upstream_token(sandbox_id, 3000, fetch=False), "")   # asked again on the next machine

    def test_a_loopback_only_app_is_exposed_through_boats_relay(self):
        handle = self.create()
        self.fake.respond = lambda _, command: ("LISTEN 0 511 127.0.0.1:5173 0.0.0.0:*\n"
                                                if "sport = :5173" in command else
                                                "LISTEN 0 511 0.0.0.0:3000 0.0.0.0:*\n" if "sport = :3000" in command
                                                else None)
        self.backend.upstream_token(handle.sandbox_id, 5173)
        self.backend.upstream_token(handle.sandbox_id, 3000)
        relayed = {b["port"]: b["localhost"] for _, p, _, b in self.fake.calls if p.endswith("/host")}
        self.assertEqual(relayed, {5173: True, 3000: False})

    def test_a_token_less_url_is_never_served(self):
        from sbx_control.runtime import Failed
        handle = self.create()
        original = self.fake._sandbox_call

        def public(method, box, rest, body, headers, query):
            status, payload = original(method, box, rest, body, headers, query)
            if rest[:1] == ["host"]:
                payload["url"] = payload["url"].split("?")[0]
            return status, payload
        self.fake._sandbox_call = public
        with self.assertRaisesRegex(Failed, "without a token"):
            self.backend.upstream_token(handle.sandbox_id, 3000)

    def test_boats_desktop_replaces_sbxs(self):
        from sbx_control import platform
        handle = self.create()
        self.assertEqual(self.backend.desktop, "provider")
        self.assertTrue(self.backend.desktop_url(handle.sandbox_id).startswith("https://desk.example.test/"))
        self.assertIn(platform.NOVNC_PORT, self.backend.ports)   # Boat's own 6080: never a preview
        self.assertNotIn("websockify", platform.start(own_desktop=False))
        self.assertIn(str(platform.SSH_BRIDGE_PORT), platform.started(own_desktop=False))

    def test_a_template_is_the_spec_replayed_in_a_builder_and_saved(self):
        from sbx_control.runtime import TemplateSpec
        spec = (TemplateSpec().from_template("tpl").set_user("root").run_cmd("apt-get install -y jq")
                .set_envs({"TOOL_HOME": "/opt/tool"}).set_user("user").set_workdir("/home/user/app")
                .dockerfile("FROM x\nARG V=1\nENV PATH_EXTRA=/x \\\n    MORE=2\nRUN echo $V \\\n  && true\n"
                            "WORKDIR /srv\nEXPOSE 80\n")
                .set_start_cmd("bash -c 'sleep infinity'", 8081))
        self.fake.respond = lambda _, command: ("LISTEN 0 511 0.0.0.0:8081 0.0.0.0:*\n"
                                                if "sport = :8081" in command else None)
        logs = []
        self.assertEqual(self.backend.build_template(spec, alias="abcd1234--repo-1", cpu=2, memory_mb=4096,
                                                     disk_mb=10240, on_log=logs.append), "abcd1234--repo-1")
        builder = self.body("POST", "/sandboxes")
        self.assertEqual((builder["from"], builder["type"], builder["noEnv"]), ("tpl", "small", True))
        self.assertEqual((builder["labels"], builder["ttlSeconds"]),
                         ({"sbx_install": "inst-a", "sbx_build": "abcd1234--repo-1"}, 4 * 3600))
        commands = [c for _, c, _ in self.fake.ran]
        apt = next(c for c in commands if "apt-get install" in c)
        self.assertIn("exec sudo -H -u root", apt)
        self.assertTrue(any("echo $V" in c for c in commands))
        envs = [v.decode() for p, v in self.fake.files[next(iter(self.fake.files))].items()
                if p.startswith("/tmp/.sbx-env/")]
        self.assertTrue(any("export V=1\n" in e for e in envs))
        unit = next(c for c in commands if "sbx-start.service" in c)
        self.assertIn("base64 -d | sudo tee", unit)
        self.assertTrue(any("systemctl enable sbx-start && sudo systemctl restart sbx-start" in c for c in commands))
        persisted = [c for c in commands if "sudo tee /etc/sbx/template.sh" in c]
        text = base64.b64decode(persisted[-1].split("echo ")[1].split(" ")[0]).decode()
        self.assertIn("export TOOL_HOME=/opt/tool", text)
        self.assertIn("export SBX_TEMPLATE_WORKDIR=/srv", text)
        self.assertEqual(self.fake.saved["abcd1234--repo-1"]["status"], "ready")
        self.assertEqual(len(self.fake.sandboxes), 0)   # the builder is deleted
        self.assertIn(("abcd1234--repo-1", "ready"), self.backend.templates())
        self.assertTrue(self.backend.delete_template("abcd1234--repo-1"))
        self.assertFalse(self.backend.delete_template("abcd1234--repo-1"))

    def written(self, path):
        """What the build last wrote to path through base64 | sudo tee."""
        command = [c for _, c, _ in self.fake.ran if f"sudo tee {path} " in c][-1]
        return base64.b64decode(command.split("echo ")[1].split(" ")[0]).decode()

    def test_the_start_command_runs_whole_through_a_shell(self):
        import subprocess
        from sbx_control.runtime import TemplateSpec
        self.backend.build_template(TemplateSpec().from_template("tpl").set_start_cmd("cd / && echo started", "true"),
                                    alias="s", cpu=2, memory_mb=4096, disk_mb=1024)
        shown = subprocess.run(["bash", "-c", self.written("/etc/sbx/start.sh")], capture_output=True, text=True)
        self.assertEqual(shown.stdout, "started\n")   # exec would run cd alone, or nothing after its &&

    def test_a_child_template_keeps_its_parents_env_and_workdir(self):
        from sbx_control.runtime import TemplateSpec
        self.fake.respond = lambda _, command: ("export BASE=ok\nexport SBX_TEMPLATE_WORKDIR=/srv/parent\n"
                                                if "cat /etc/sbx/template.sh" in command else None)
        self.backend.build_template(TemplateSpec().from_template("tpl").set_envs({"CHILD": "1"}).run_cmd("make"),
                                    alias="c", cpu=2, memory_mb=4096, disk_mb=1024)
        self.assertEqual(self.written("/etc/sbx/template.sh"),
                         "export BASE=ok\nexport CHILD=1\nexport SBX_TEMPLATE_WORKDIR=/srv/parent\n")
        self.assertIn("cd /srv/parent || exit 1; make", next(c for _, c, _ in self.fake.ran if "make" in c))

    def test_a_dockerfile_workdir_expands_its_variables(self):
        import subprocess
        from sbx_control.runtime import TemplateSpec
        steps = dockerfile.dockerfile_steps("FROM x\nENV APP=/srv/app\nWORKDIR $APP/web\n")
        exports = "".join(dockerfile.export_line(k, v) for what, d in steps if what == "env" for k, v in d.items())
        shown = subprocess.run(["bash", "-c", exports + f"printf %s {steps[-1][1]}"], capture_output=True, text=True)
        self.assertEqual(shown.stdout, "/srv/app/web")
        self.fake.respond = lambda _, command: "/srv/app/web" if 'printf %s "$APP/web"' in command else None
        self.backend.build_template(TemplateSpec().from_template("tpl").dockerfile("ENV APP=/srv/app\nWORKDIR $APP/web\n"
                                                                                    "RUN make"),
                                    alias="w", cpu=2, memory_mb=4096, disk_mb=1024)
        self.assertIn("export SBX_TEMPLATE_WORKDIR=/srv/app/web\n", self.written("/etc/sbx/template.sh"))
        self.assertIn("cd /srv/app/web || exit 1; make", next(c for _, c, _ in self.fake.ran if "make" in c))

    def test_a_dockerfile_reads_as_docker_does(self):
        import subprocess
        steps = dockerfile.dockerfile_steps(
            "FROM x\nARG V=1\nRUN apt-get update \\\n  # a comment inside the continuation\n  && echo done\n"
            "ENV X=${V}-$V PATH=/opt/node/bin:$PATH A=\"x y\" C=$(id) D=`id` E=a;b\nENV MSG hello $V\n")
        self.assertEqual(steps[1], ("run", "apt-get update  && echo done"))
        exports = "".join(dockerfile.export_line(k, v) for what, d in steps if what == "env" for k, v in d.items())
        shown = subprocess.run(["bash", "-c", "PATH=/usr/bin:/bin\n" + exports + 'printf "%s|" "$X" "$PATH" "$A" "$C" '
                                '"$D" "$E" "$MSG"'], capture_output=True, text=True, check=True).stdout
        self.assertEqual(shown, "1-1|/opt/node/bin:/usr/bin:/bin|x y|$(id)|`id`|a;b|hello 1|")   # no command ran

    def test_builds_and_snapshots_are_refused_on_the_trial_before_a_builder_starts(self):
        from sbx_control.runtime import LimitRefused, TemplateSpec
        self.fake.plan = "trial"
        with self.assertRaisesRegex(LimitRefused, "first payment"):
            self.backend.build_template(TemplateSpec().from_image("ubuntu:24.04"), alias="a", cpu=2,
                                        memory_mb=4096, disk_mb=1024)
        self.assertEqual(self.fake.sandboxes, {})                # no builder for nothing
        handle = self.create()
        with self.assertRaisesRegex(LimitRefused, "first payment"):
            self.backend.snapshot(handle.sandbox_id, "cache")

    def test_builders_left_behind_are_reclaimed_and_retries_reuse_one_key(self):
        from sbx_control.runtime import TemplateSpec
        old = self.backend.create("tpl", timeout=600, metadata={"sbx_install": "inst-a", "sbx_build": "x"})
        self.fake.sandboxes[old.sandbox_id]["createdAt"] = "2026-01-01T00:00:00Z"
        self.fake.fail("POST", "/sandboxes", 409, "idempotency_in_progress")
        self.backend.build_template(TemplateSpec().from_image("ubuntu:24.04"), alias="b", cpu=2, memory_mb=4096,
                                    disk_mb=1024)
        self.assertNotIn(old.sandbox_id, self.fake.sandboxes)
        keys = [h for m, p, _, _, h in self.fake.requests if m == "POST" and p == "/sandboxes"][-2:]
        self.assertEqual(keys[0]["Idempotency-Key"], keys[1]["Idempotency-Key"])

    def test_a_sandbox_is_handed_over_once_its_files_have_all_come_in(self):
        original = self.fake._create

        def restoring(body):
            status, payload = original(body)
            self.fake.sandboxes[payload["sandbox"]["id"]]["hydrated"] = False
            return status, payload
        self.fake._create = restoring
        handle = self.create()
        self.assertTrue(self.fake.sandboxes[handle.sandbox_id]["hydrated"])   # waited for it

    def test_a_sandbox_that_fails_to_start_is_deleted(self):
        from sbx_control.runtime import Failed
        original = self.fake._create

        def failing(body):
            status, payload = original(body)
            self.fake.sandboxes[payload["sandbox"]["id"]]["state"] = "error"
            return status, payload
        self.fake._create = failing
        with self.assertRaisesRegex(Failed, "failed to start"):
            self.create()
        self.assertEqual(self.fake.sandboxes, {})   # not left running until its timeout

    def test_a_resume_without_a_timeout_keeps_the_auto_stop(self):
        handle = self.create()
        self.backend.pause(handle.sandbox_id)
        self.backend.connect(handle.sandbox_id)
        self.assertNotIn("ttlSeconds", self.body("POST", f"/sandboxes/{handle.sandbox_id}/resume"))   # null: off for good

    def test_an_image_other_than_boats_ubuntu_and_a_copy_are_refused(self):
        from sbx_control.runtime import Failed, TemplateSpec
        with self.assertRaisesRegex(Failed, "Ubuntu 24.04"):
            self.backend.build_template(TemplateSpec().from_image("debian:12"), alias="a", cpu=2, memory_mb=4096,
                                        disk_mb=1024)
        with self.assertRaisesRegex(Failed, "COPY.*RUN step"):
            self.backend.build_template(TemplateSpec().from_image("ubuntu:24.04").dockerfile("COPY . /app"),
                                        alias="a", cpu=2, memory_mb=4096, disk_mb=1024)
        self.assertEqual(self.fake.sandboxes, {})

    def test_sizes_map_to_the_smallest_machine_that_fits(self):
        from sbx_control.runtime import LimitRefused
        machine = self.boat_module.plans.machine
        self.assertEqual(machine(2, 4096, 10240), "small")
        self.assertEqual(machine(4, 4096, 51 * 1024), "default")
        self.assertEqual(machine(2, 12000, 0), "large")
        self.assertEqual(machine(16, 32768, 0), "xlarge")
        with self.assertRaises(LimitRefused):
            machine(32, 1024, 0)

    def test_limit_refusals_name_what_to_change(self):
        from sbx_control.runtime import LimitRefused, NoRoom
        for status, code, field in ((429, "limit_reached", "sandboxes"), (429, "rate_limited", "starts"),
                                    (403, "machine_class_plan_required", "cpu"),
                                    (403, "trial_machine_class_not_allowed", "cpu"),
                                    (402, "billing_required", "plan"), (400, "trial_auto_stop_required", "timeout_seconds")):
            self.fake.fail("POST", "/sandboxes", status, code)
            with self.assertRaises(LimitRefused) as raised:
                self.create()
            self.assertEqual(raised.exception.field, field, code)
        self.fake.fail("POST", "/sandboxes", 503, "out_of_capacity")
        with self.assertRaises(NoRoom):
            self.create()

    def test_a_restoring_sandbox_is_retried_and_a_stopped_one_is_not_running(self):
        from sbx_control.runtime import NotRunning
        handle = self.create()
        self.fake.fail("POST", f"/sandboxes/{handle.sandbox_id}/commands", 502, "sandbox_direct_failed",
                       details={"retryable": True, "message": "sandbox_restoring"})
        self.assertEqual(handle.commands.run("true").exit_code, 0)
        self.fake.sandboxes[handle.sandbox_id]["state"] = "archived"
        with self.assertRaises(NotRunning):
            handle.commands.run("true")

    def test_a_command_that_may_have_run_is_not_sent_again(self):
        from sbx_control.runtime.boat.errors import BoatError
        handle = self.create()
        path = f"/sandboxes/{handle.sandbox_id}/commands"
        before = len(self.fake.calls)
        self.fake.fail("POST", path, 502, "sandbox_direct_failed", details={"retryable": True, "message": "reset"})
        with self.assertRaises(BoatError):
            handle.commands.run("echo once >> log")
        self.assertEqual(sum(m == "POST" and p == path for m, p, _, _ in self.fake.calls[before:]), 1)
        self.fake.fail("GET", f"/sandboxes/{handle.sandbox_id}/files", 502, "sandbox_direct_failed",
                       details={"retryable": True, "message": "reset"})
        handle.files.write("/home/user/a", "x")
        self.assertEqual(handle.files.read("/home/user/a"), "x")   # a file read gives the same again

    def test_saved_names_follow_boats_rules(self):
        name = self.boat_module.build.saved_name
        self.assertEqual(name("abcd1234--default-0f0f0f0f"), "abcd1234--default-0f0f0f0f")
        made = name("Amp_Cache:main/feature")
        self.assertRegex(made, r"^[a-z0-9][a-z0-9-]{0,62}$")
        self.assertEqual(made, name("Amp_Cache:main/feature"))


class Settings(unittest.TestCase):
    def test_presets_are_the_trial_and_the_paid_plans_and_the_defaults_the_20_plan(self):
        from sbx_control.runtime import boat
        self.assertEqual([p.name for p in boat.PRESETS], ["trial", "20", "100", "500", "2000"])
        trial, twenty = boat.PRESETS[0].by_key(), boat.PRESETS[1].by_key()
        self.assertEqual((trial["BOAT_MAX_CPU"], trial["BOAT_MAX_SANDBOXES"], trial["BOAT_MAX_TIMEOUT"]),
                         ("4", "2", str(2 * 3600)))   # the trial: the default size, 2 at once, 2 hours
        self.assertIn("templates need the first payment", boat.PRESETS[0].note)
        self.assertEqual((twenty["BOAT_MAX_CPU"], twenty["BOAT_MAX_SANDBOXES"], twenty["BOAT_MAX_TIMEOUT"]),
                         ("8", "100", str(30 * 86400)))
        self.assertEqual(boat.PRESETS[2].by_key()["BOAT_MAX_CPU"], "16")   # xlarge from $100
        backend, _ = configured()
        self.assertEqual(backend.limits().known(), {"timeout_seconds": 30 * 86400, "cpu": 8, "memory_mb": 16384,
                                                    "disk_mb": 111 * 1024, "sandboxes": 100})
        keys = [s.key for s in boat.SETTINGS]
        self.assertEqual(keys[:3], ["BOAT_API_KEY", "BOAT_API_URL", "BOAT_ORG"])
        self.assertTrue(boat.SETTINGS[0].secret and boat.SETTINGS[1].rekey)

    def test_configure_needs_a_key_and_an_https_url(self):
        from sbx_control.runtime.boat import Boat
        with self.assertRaisesRegex(SystemExit, "BOAT_API_KEY"):
            Boat().configure({})
        with self.assertRaisesRegex(SystemExit, "https"):
            Boat().configure({"BOAT_API_KEY": "boat_x", "BOAT_API_URL": "http://boat.dev/api/v1"})

    def test_the_org_goes_with_creates_and_listings(self):
        backend, fake = configured(BOAT_ORG="team_abc")
        backend.sandboxes()
        self.assertEqual(fake.calls[-1][2]["org"], ["team_abc"])

    def test_status_warns_of_the_trial_and_a_key_that_expires_soon(self):
        import datetime
        backend, fake = configured()
        fake.plan = "trial"
        soon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3)
        fake.keys = [{"keyLastFour": "test", "expiresAt": soon.isoformat()}]
        with patch("sbx_control.runtime.sandboxes", return_value=[]):
            found = backend.status()
        self.assertEqual(found.state, "attention")
        self.assertIn("Trial: templates need the first payment", found.summary)
        self.assertIn("expires in", found.summary)
        checks = {c.name: c for c in backend.test()}
        self.assertFalse(checks["Boat plan"].ok)   # confirmed live: no saved snapshots on the trial
        self.assertIn("first payment", checks["Boat plan"].fix)
        self.assertFalse(checks["Boat API key"].ok)


class Installed(Installation):
    """Boat as an installation's runtime: isolation, the cold resume, what it refuses."""
    RUNTIME_ENV = BOAT_ENV

    def setUp(self):
        super().setUp()
        from sbx_control import runtime
        self.runtime = runtime
        self.enterContext(patch.object(time, "sleep"))
        self.backend, self.fake = configured()
        self.enterContext(patch.dict(runtime._backends, {"boat": self.backend}))
        self.enterContext(patch.dict(runtime._index, clear=True))
        self.fake.saved[runtime.template_name("tpl", "boat")] = {
            "name": runtime.template_name("tpl", "boat"), "status": "ready", "sourceSandboxId": "x",
            "type": "default", "createdAt": "2026-11-17T12:00:00Z", "files": {}}

    def test_another_installations_sandboxes_and_templates_stay_its_own(self):
        template = self.runtime.template_name("tpl", "boat")
        self.assertRegex(template, r"^[0-9a-f]{8}--tpl$")
        handle = self.runtime.create(template, timeout=600, metadata={"sbx": "mine"})
        self.backend.create(template, timeout=600, metadata={"sbx": "theirs", "sbx_install": "someone-else"})
        self.assertEqual([i.sandbox_id for i in self.runtime.sandboxes()], [handle.sandbox_id])
        self.fake.saved["deadbeef--tpl"] = {"name": "deadbeef--tpl", "status": "ready", "sourceSandboxId": "x",
                                           "type": "default", "createdAt": "2026-11-17T12:00:00Z", "files": {}}
        self.assertEqual([n for n, _ in self.runtime.templates("boat")], [template])

    def test_untrusted_public_repos_are_refused_on_boat(self):
        from sbx_control import reach, settings
        self.assertFalse(self.runtime.capabilities("boat").egress_rules)
        self.assertFalse(reach.offered()["public_repos"])   # the settings page doesn't offer it
        with self.assertRaisesRegex(Exception, "egress_rules"):
            settings.update("access", {**settings.current().access.model_dump(), "public_repos": True}, by="test")

    def test_a_resume_is_a_cold_boot_that_reships_the_runtime_and_restarts_the_app(self):
        from types import SimpleNamespace
        from sbx_control import core
        handle = self.runtime.create(self.runtime.template_name("tpl", "boat"), timeout=600,
                                     metadata={"sbx": "cold", "repo": "owner/app"})
        self.backend.pause(handle.sandbox_id)
        info = self.runtime.info(handle.sandbox_id)
        sbx = self.backend.connect(handle.sandbox_id)
        from sbx_control import sandbox_runtime
        with patch.object(sandbox_runtime, "ensure_started") as started, \
                patch.object(sandbox_runtime, "initialize_ssh_identity"), \
                patch.object(sandbox_runtime, "runtime_values", return_value=({}, set(), SimpleNamespace())), \
                patch.object(sandbox_runtime, "write_runtime") as written, patch.object(core.extensions, "on_resume"):
            self.assertTrue(core.restore_runtime(sbx, info))   # True: the app restarts
        started.assert_called_once_with(sbx, "boat")
        written.assert_called_once()
        self.assertEqual(self.runtime.desktop("boat"), "provider")
