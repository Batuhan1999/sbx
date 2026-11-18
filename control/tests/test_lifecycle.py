"""Run separately: PYTHONPATH=control .venv/bin/python -m unittest control.tests.test_lifecycle."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import re
import shlex
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from support import Installation


def done(stdout=""):
    return SimpleNamespace(stdout=stdout, stderr="", exit_code=0)


class FakeVM:
    """An E2B sandbox as sbx-control sees it: the guest's git runs on a local
    checkout, every health URL answers 200, the rest succeeds quietly. Its SSH
    host key is the template's until the guest helper gives it its own."""
    TEMPLATE_KEY = "ssh-ed25519 template-key"

    def __init__(self, sandbox_id="fixture-vm", identity_fails=False):
        self.sandbox_id, self.identity_fails = sandbox_id, identity_fails
        self.host_key = self.TEMPLATE_KEY
        self.commands, self.files = self, SimpleNamespace(write=lambda *args, **kwargs: None)

    def run(self, command, **_):
        from sbx_control import platform
        if command == platform.STARTED:
            return done("ready")
        identity = re.search(r"sbx_guest\.py identity (\S+)", command)
        if identity:
            if self.identity_fails:
                raise RuntimeError("the guest helper failed")
            self.host_key = f"ssh-ed25519 own-{identity[1]}"
            return done()
        if "ssh_host_ed25519_key.pub" in command:
            return done(self.host_key)
        if command.startswith("bash -c ") and "git " in command:
            return subprocess.run(shlex.split(command), check=True, capture_output=True, text=True)
        return done(" ".join(["200"] * command.count("curl ")))


def git(*args):
    return subprocess.run(["git", *map(str, args)], check=True, capture_output=True, text=True)


def checkout_of(root: Path) -> Path:
    """A clone of a repo whose main and feature branches differ in example.txt."""
    source, checkout = root / "source", root / "checkout"
    git("init", "-b", "main", source)
    git("-C", source, "config", "user.email", "fixture@example.test")
    git("-C", source, "config", "user.name", "fixture")
    (source / "example.txt").write_text("main\n")
    git("-C", source, "add", ".")
    git("-C", source, "commit", "-m", "main")
    git("-C", source, "checkout", "-q", "-b", "feature")
    (source / "example.txt").write_text("feature\n")
    git("-C", source, "commit", "-am", "feature")
    git("-C", source, "checkout", "-q", "main")
    git("clone", "-q", "-b", "main", source, checkout)
    for key, value in (("user.email", "fixture@example.test"), ("user.name", "fixture")):
        git("-C", checkout, "config", key, value)
    return checkout


class LifecycleTests(Installation):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from sbx_control import app, auth, core, lifecycle, recipes, state
        cls.app, cls.auth, cls.core, cls.lifecycle, cls.recipes, cls.state = app, auth, core, lifecycle, recipes, state
        from sbx_control import template_builds
        cls.template_for = staticmethod(template_builds.template_for)   # setUp stands in for it

    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        with self.state.db() as conn:
            conn.execute("insert into sandboxes (name, owner, branch, slug) "
                         "values ('fixture', 'owner@example.test', 'main', 'fixture')")
        self.client = TestClient(self.app.app, base_url="https://203.0.113.10")
        self.addCleanup(self.client.close)
        self.app.app.dependency_overrides[self.auth.api_user] = lambda: "owner@example.test"
        self.addCleanup(self.app.app.dependency_overrides.clear)
        self.enterContext(patch("sbx_control.setup.complete", return_value=True))
        # Sandboxes start from the template the default built last.
        from sbx_control import template_builds, templates
        self.enterContext(patch.object(template_builds, "template_for", return_value=f"{templates.DEFAULT}-fixture"))
        self.info = SimpleNamespace(sandbox_id="fixture-vm", state="running",
                                    metadata={"sbx": "fixture", "owner": "owner@example.test", "repo": "owner/repo"})

    def workspace(self, services=False):
        op = self.lifecycle.begin("fixture", "create")
        if services:
            op.advance("provisioning_services")
            op.advance("services_ready")
        op.advance("allocating")
        op.advance("preparing_workspace", sandbox_id="fixture-vm", runtime="running")
        op.advance("workspace_ready", finish=True)
        return op

    def healthy(self):
        self.workspace(services=True)
        op = self.lifecycle.begin("fixture", "restart")
        op.advance("starting_app")
        op.advance("app_ready", finish=True)
        return op

    def pause_hook(self, script):
        """A ready service of the fixture whose package has a pause hook."""
        import json
        from sbx_control import secret_store
        files = {"package.yaml": "kind: provision\nruns: {create: pause.sh}\nhooks: {pause: pause.sh}\n", "pause.sh": script}
        with self.state.db() as conn:
            conn.execute("delete from sandbox_services where sandbox = 'fixture'")
            conn.execute("insert into sandbox_services (sandbox, key, uses, repo, definition, inputs, connections, "
                         "resource_id, position, status) values ('fixture', 'db', 'hooked', 'owner/repo', ?, ?, '{}', "
                         "'fixture-db', 0, 'ready')", (json.dumps({"name": "hooked", "files": files, "source": "shipped"}),
                                                       secret_store.seal({}, key="service/fixture-db/inputs")))

        def cleanup():
            with self.state.db() as conn:
                conn.execute("delete from sandbox_services where sandbox = 'fixture'")
        self.addCleanup(cleanup)

    @contextlib.contextmanager
    def creating(self, recipe, vm):
        """A new sandbox of recipe (a repo this installation serves publicly) on vm; yields runtime.create."""
        with patch.object(self.core, "find", return_value=None), \
                patch.object(self.recipes, "load", return_value=recipe), \
                patch.object(self.core.settings, "trust", return_value="public"), \
                patch.object(self.core.runtime, "connect", return_value=vm), \
                patch.object(self.core.runtime, "create", return_value=vm) as create:
            yield create

    def test_pause_runs_the_services_pause_hooks_after_the_vm_is_paused(self):
        marker = Path(self.enterContext(tempfile.TemporaryDirectory())) / "hooked"
        self.pause_hook(f"#!/bin/sh\necho \"$SBX_ID\" > {marker}\n")
        self.healthy()
        seen = []
        with patch.object(self.core.runtime, 'pause', side_effect=lambda _: seen.append(marker.exists())):
            self.core.pause('fixture', self.info)
        self.assertEqual(seen, [False])
        self.assertEqual(marker.read_text(), "fixture-db\n")
        self.assertEqual(self.lifecycle.get('fixture')['runtime'], 'paused')

    def test_pause_hook_failure_is_deferred_without_losing_the_pause(self):
        self.pause_hook("#!/bin/sh\necho unavailable >&2\nexit 1\n")
        self.healthy()
        with patch.object(self.core.runtime, 'pause'):
            self.core.pause('fixture', self.info)
        self.assertEqual(self.lifecycle.get('fixture')['runtime'], 'paused')
        self.assertEqual(self.lifecycle.get('fixture')['phase'], 'app_ready')
        self.assertIn(('service pause hook deferred', True), [(kind, internal) for _, kind, _, internal in self.state.events('fixture')])

    def busy(self):
        return self.core.runtime.Busy("503: Sandbox 'fixture-vm' cannot be paused right now because its node is busy, please retry")

    def test_a_pause_the_runtime_refuses_for_a_moment_is_retried(self):
        self.healthy()
        with patch.object(self.core, "PAUSE_RETRY_SECONDS", (0, 0)), \
                patch.object(self.core.runtime, "pause", side_effect=[self.busy(), None]) as pause:
            self.core.pause("fixture", self.info)
        self.assertEqual(pause.call_count, 2)
        self.assertEqual(self.lifecycle.get("fixture")["runtime"], "paused")

    def test_a_refused_pause_leaves_a_running_healthy_sandbox_ready_and_says_why(self):
        # The launch lab: five refused pauses left n8n sandboxes 'failed' while the app ran fine.
        self.healthy()
        with patch.object(self.core, "PAUSE_RETRY_SECONDS", (0, 0)), \
                patch.object(self.core.runtime, "pause", side_effect=self.busy()) as pause, \
                patch.object(self.core, "find", return_value=self.info), \
                self.assertRaisesRegex(self.core.PauseRefused, "refused the pause .* keeps running") as refused:
            self.core.pause("fixture", self.info)
        self.assertEqual(pause.call_count, 3)
        record = self.lifecycle.get("fixture")
        self.assertEqual((record["phase"], record["runtime"], record["active"]), ("app_ready", "running", False))
        self.assertIn("still saving the template or snapshot", str(refused.exception))
        self.assertIn("pause refused", [kind for _, kind, _, _ in self.state.events("fixture")])

    def test_a_refusal_for_disk_says_how_much_a_pause_needs(self):
        self.healthy()
        self.info.memory_mb = 4096
        with patch.object(self.core, "PAUSE_RETRY_SECONDS", ()), \
                patch.object(self.core.runtime, "pause", side_effect=self.busy()), \
                patch.object(self.core, "find", return_value=self.info), \
                patch("sbx_control.runtime.embed.storage.free_mb", return_value=3 * 1024), \
                self.assertRaisesRegex(self.core.PauseRefused, r"disk has 3\.0 GB free .* memory \(4\.0 GB\) plus 1 GB"):
            self.core.pause("fixture", self.info)

    def test_a_refused_pause_that_lost_the_vm_fails(self):
        self.healthy()
        gone = SimpleNamespace(**{**vars(self.info), "state": "killed"})
        with patch.object(self.core, "PAUSE_RETRY_SECONDS", ()), \
                patch.object(self.core.runtime, "pause", side_effect=self.busy()), \
                patch.object(self.core, "find", return_value=gone), self.assertRaises(Exception):
            self.core.pause("fixture", self.info)
        self.assertEqual(self.lifecycle.get("fixture")["phase"], "failed")

    def test_the_api_answers_a_refused_pause_with_503_and_retry_after(self):
        self.healthy()
        self.client.cookies.clear()
        with patch.object(self.auth, "require_owned", return_value=self.info), \
                patch.object(self.core, "PAUSE_RETRY_SECONDS", ()), \
                patch.object(self.core.runtime, "pause", side_effect=self.busy()), \
                patch.object(self.core, "find", return_value=self.info):
            response = self.client.post("/api/sandboxes/fixture/pause")
        self.assertEqual((response.status_code, response.headers.get("retry-after")), (503, "60"), response.text)
        self.assertIn("keeps running", response.json()["detail"])

    def full_run_vm(self, probe="exited"):
        """A VM whose /run tmpfs is full of Codex's files, its supervisor gone (probe)."""
        def run(command, **_):
            if "processes probe" in command:
                return done(probe)
            if command.startswith("df -Pk /run"):
                return done("tmpfs 806032 803600 2432 100% /run\n196\t/run/e2b\n802816\t/run/sbx/codex\n803400\t/run/sbx\n")
            return done()
        vm = Mock()
        vm.commands.run.side_effect = run
        return vm

    def test_the_keeper_notices_an_app_whose_supervisor_a_full_run_stopped(self):
        from sbx_control import keeper
        self.healthy()
        self.info.metadata["repo"] = "owner/repo"
        recipe = self.recipes.App("owner/repo", Path("/unused"), {"processes": {"web": "serve"}})
        with patch.object(self.core, "app_of", return_value=recipe), \
                patch.object(self.core.runtime, "connect", return_value=self.full_run_vm()):
            keeper.notice_stopped_app(self.info)
        record = self.lifecycle.get("fixture")
        self.assertEqual((self.lifecycle.status(record), record["readiness"]), ("failed", "services_ready"))
        self.assertIn("/run is full (100% of 787 MB", record["error"])
        self.assertIn("/run/sbx/codex 784 MB", record["error"])
        self.assertTrue(self.lifecycle.actions(record, "failed", "running")["restart"])
        self.assertIn("app stopped", [kind for _, kind, _, _ in self.state.events("fixture")])

    def test_the_keeper_leaves_a_running_app_and_a_busy_sandbox_alone(self):
        from sbx_control import keeper
        self.healthy()
        recipe = self.recipes.App("owner/repo", Path("/unused"), {"processes": {"web": "serve"}})
        with patch.object(self.core, "app_of", return_value=recipe), \
                patch.object(self.core.runtime, "connect", return_value=self.full_run_vm(probe="alive")):
            keeper.notice_stopped_app(self.info)
        self.assertEqual(self.lifecycle.get("fixture")["readiness"], "app_ready")
        self.lifecycle.begin("fixture", "restart")
        with patch.object(self.core, "check_app") as check:
            keeper.notice_stopped_app(self.info)
        check.assert_not_called()

    def test_a_write_that_fails_on_a_full_run_says_what_fills_it(self):
        self.healthy()
        failure = RuntimeError("attempted to write 323 bytes: not enough disk space available")
        with patch.object(self.core, "_restart", side_effect=failure), patch.object(self.core, "find", return_value=self.info), \
                patch.object(self.core.runtime, "connect", return_value=self.full_run_vm()), \
                self.assertRaisesRegex(self.core.SandboxError, r"not enough disk space available: the sandbox's /run is full"):
            self.core.restart("fixture")
        self.assertEqual(self.lifecycle.get("fixture")["phase"], "failed")

    def amp_vm(self, state="running"):
        """The fixture as one of Amp's orbs, its bootstrap stops recorded in calls."""
        from sbx_control.connectors.amp import orbs
        self.info.metadata.update(connector="amp", orbId="orb-1")
        self.info.state = state
        calls = []
        self.enterContext(patch.object(orbs, "stop_bootstrap", side_effect=lambda *_: calls.append("bootstrap stopped")))
        self.enterContext(patch.object(self.core.runtime, "connect", return_value=Mock()))
        return calls

    def test_amp_pause_stops_the_bootstrap_before_the_checkpoint_and_the_vm_pause(self):
        self.healthy()
        calls = self.amp_vm()
        with patch.object(self.core.runtime, "pause", side_effect=lambda _: calls.append("paused")):
            self.core.pause("fixture", self.info, before_pause=lambda: calls.append("checkpoint"))
        self.assertEqual(calls, ["bootstrap stopped", "checkpoint", "paused"])

    def test_amp_pause_is_cancelled_when_the_bootstrap_does_not_stop(self):
        from sbx_control.connectors.amp import orbs
        self.healthy()
        self.amp_vm()
        with patch.object(orbs, "stop_bootstrap", side_effect=RuntimeError("transport unavailable")), \
                patch.object(self.core.runtime, "pause") as pause:
            with self.assertRaisesRegex(RuntimeError, "transport unavailable"):
                self.core.pause("fixture", self.info)
        pause.assert_not_called()

    def test_amp_pause_of_a_paused_vm_does_not_wake_it(self):
        self.healthy()
        calls = self.amp_vm(state="paused")
        with patch.object(self.core.runtime, "pause"):
            self.core.pause("fixture", self.info)
        self.assertEqual(calls, [])

    def test_amp_resume_stops_an_older_snapshot_s_bootstrap_before_restoring_the_runtime(self):
        self.healthy()
        calls = self.amp_vm(state="paused")
        sandbox = Mock()
        sandbox.commands.run.side_effect = lambda *a, **k: (calls.append("runtime probe") or Mock(stdout="ok"))
        with patch.object(self.core, "app_of", return_value=Mock(resume_mode="preserve")):
            self.assertFalse(self.core.restore_runtime(sandbox, self.info))
            self.info.state = "running"   # an already running one is left alone
            self.assertFalse(self.core.restore_runtime(sandbox, self.info))
        self.assertEqual(calls, ["bootstrap stopped", "runtime probe", "runtime probe"])

    def test_other_sandboxes_pause_without_a_connector_hook(self):
        from sbx_control.connectors.amp import orbs
        self.healthy()
        with patch.object(orbs, "stop_bootstrap") as stop, patch.object(self.core.runtime, "pause"):
            self.core.pause("fixture", self.info)
        stop.assert_not_called()

    def test_no_workspace_readiness_before_source_checkpoint(self):
        op = self.lifecycle.begin("fixture", "create")
        op.advance("allocating")
        op.advance("preparing_workspace", runtime="running")
        self.assertEqual(self.lifecycle.get("fixture")["readiness"], "not_ready")
        op.advance("workspace_ready", finish=True)
        self.assertEqual(self.lifecycle.get("fixture")["readiness"], "workspace_ready")

    def test_capacity_counts_the_memory_the_template_was_built_with(self):
        recipe = self.recipes.App("owner/repo", Path("/unused"), {"resources": {"memory_mb": 8192}})
        asked = []

        def room(memory_mb=None, kind=None):  # the host's hugepages
            asked.append(memory_mb)
            return {"known": False}
        with patch.object(self.recipes, "load", return_value=recipe), \
                patch.object(self.core.settings, "trust", return_value="trusted"), \
                patch.object(self.core, "capacity", side_effect=room), \
                patch.object(self.core, "find", return_value=self.info):  # stops creation after the reservation
            for template, memory_mb in (("old-template", 4096), ("a-snapshot", None)):
                with self.assertRaisesRegex(self.core.SandboxError, "already exists"):
                    self.core.create("fixture", "fixture", "owner@example.test", "main", "",
                                     "owner/repo", template=template, memory_mb=memory_mb)
        self.assertEqual(asked, [4096, 8192])

    def test_exhausted_preview_creation_releases_partial_port_assignments(self):
        from sbx_control import ip_previews
        recipe = self.recipes.App("owner/repo", Path("/unused"), {"previews": {"": 3000, "api": 4000}})
        with self.creating(recipe, FakeVM()) as create, \
                patch.object(ip_previews, "address", return_value="203.0.113.10"), \
                patch.object(ip_previews, "port_range", return_value=(22000, 22000)):
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, "All IP preview ports"):
                    self.core.create("fixture", "capacity-fixture", "owner@example.test",
                                     "main", "", "owner/repo", template="fixture-template")
                self.assertIsNone(ip_previews.route(22000))
        create.assert_not_called()

    def test_webhook_reports_creation_failure_without_a_runtime(self):
        from fastapi import HTTPException
        from sbx_control import github_api as previews, github_app
        payload = {"action": "labeled", "number": 42, "repository": {"full_name": "owner/repo"},
            "pull_request": {"state": "open", "labels": [{"name": "preview"}],
                "head": {"ref": "feature", "repo": {"full_name": "owner/repo"}}}}
        with patch.object(github_app, "verify", return_value=True), \
                patch.object(self.core.settings, "trust", return_value="trusted"), \
                patch.object(previews, "upsert", side_effect=HTTPException(502, "secret detail")), \
                patch.object(github_app, "comment_preview") as comment:
            response = self.client.post("/github/webhook", json=payload, headers={"x-github-event": "pull_request"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("Preview failed", comment.call_args.args[2])
        self.assertNotIn("secret detail", comment.call_args.args[2])

    def test_failed_port_probe_never_reclaims_routes(self):
        from sbx_control import ip_previews, routing
        with patch.object(self.core.runtime, "connect", side_effect=RuntimeError("unavailable")), \
                patch.object(ip_previews, "observe") as observe:
            routing.detect_ports("fixture-vm", self.recipes.App("owner/repo", Path("/unused"), {}))
        observe.assert_not_called()

    def test_exhausted_optional_urls_do_not_break_sandbox_views(self):
        from sbx_control import addressing, ip_previews, routing
        recipe = self.recipes.App("owner/repo", Path("/unused"), {"previews": {"": 3000, "api": 4000}})
        def origin(_slug, port):
            if port == 3000:
                return "https://203.0.113.10:21000"
            raise ip_previews.PortPoolExhausted("full")
        with \
                patch.object(ip_previews, "origin", side_effect=origin):
            self.assertEqual(addressing.urls("fixture", recipe), {"web": "https://203.0.113.10:21000"})
            with self.assertRaises(ip_previews.PortPoolExhausted):
                self.core.sbx_env("fixture", "fixture", recipe)
            with patch.object(self.core, "app_of", return_value=recipe), \
                    patch.object(self.core, "slug_of", return_value="fixture"), \
                    patch.object(routing, "known_ports", return_value={9999: False}):
                links, ports = routing.detected_urls(self.info)
            self.assertEqual(links, {})
            self.assertIn("preview_error", ports[0])

    def test_preview_failure_comment_does_not_require_a_runtime(self):
        from sbx_control import github_api as previews, github_app
        self.lifecycle.begin("pr-42", "create").fail(RuntimeError("startup failed"))
        with patch.object(previews, "preview_name", return_value="pr-42"), \
                patch.object(previews.time, "sleep"), \
                patch.object(self.core, "find", return_value=None), \
                patch.object(github_app, "comment_preview") as comment:
            previews.comment_when_up("owner/repo", 42)
        self.assertIn("Preview failed", comment.call_args.args[2])

    def test_starting_preview_does_not_publish_a_ready_comment_on_timeout(self):
        from sbx_control import github_api as previews, github_app
        self.workspace()
        self.lifecycle.begin("fixture", "prepare")
        with patch.object(previews, "preview_name", return_value="fixture"), \
                patch.object(previews.time, "sleep"), \
                patch.object(self.core, "find", return_value=self.info), \
                patch.object(github_app, "comment_preview") as comment:
            previews.comment_when_up("owner/repo", 42)
        comment.assert_not_called()

    def test_application_requires_workspace_and_services(self):
        self.workspace()
        op = self.lifecycle.begin("fixture", "restart")
        with self.assertRaises(self.lifecycle.Conflict):
            op.advance("starting_app")
        self.assertEqual(self.lifecycle.get("fixture")["phase"], "queued")

    def test_operation_kind_and_sandbox_binding_are_enforced(self):
        op = self.lifecycle.begin("fixture", "create")
        with self.assertRaises(self.lifecycle.Conflict):
            op.advance("deleting")
        with self.assertRaises(self.lifecycle.Conflict):
            with self.lifecycle.operation("other", "create", op):
                pass
        op.configure_workspace("owner/one")
        with self.assertRaises(self.lifecycle.Conflict):
            op.configure_workspace("owner/two")
        op.advance("allocating")
        op.advance("preparing_workspace", sandbox_id="first-vm")
        with self.assertRaises(self.lifecycle.Conflict):
            op.advance("workspace_ready", sandbox_id="other-vm", finish=True)
        self.assertFalse(self.lifecycle.get("fixture")["workspace_ready"])

    def test_phase_receipt_and_status_roll_back_together(self):
        self.workspace()
        before = self.lifecycle.get("fixture")
        with self.state.db() as conn:
            conn.execute("create trigger reject_lifecycle_event before insert on sandbox_lifecycle_events "
                         "begin select raise(abort,'fixture write failure'); end")
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                self.lifecycle.begin("fixture", "prepare")
            after = self.lifecycle.get("fixture")
            self.assertEqual(before["revision"], after["revision"])
            self.assertEqual(before["phase"], after["phase"])
            self.assertEqual(before["status"], after["status"])
        finally:
            with self.state.db() as conn:
                conn.execute("drop trigger reject_lifecycle_event")

    def test_health_cannot_be_claimed_without_a_start(self):
        self.workspace(services=True)
        op = self.lifecycle.begin("fixture", "resume")
        op.advance("restoring_runtime")
        with self.assertRaises(self.lifecycle.Conflict):
            op.advance("app_ready", finish=True)

    def test_failed_app_keeps_workspace_and_service_checkpoints(self):
        self.workspace(services=True)
        op = self.lifecycle.begin("fixture", "restart")
        op.advance("starting_app")
        op.fail(RuntimeError("private diagnostic must not be exported"))
        current = self.lifecycle.get("fixture")
        self.assertEqual(current["readiness"], "services_ready")
        self.assertEqual(current["application"], "failed")
        self.assertNotIn("private diagnostic", current["error"])

    def test_only_one_concurrent_operation_is_accepted(self):
        self.workspace(services=True)
        def begin(_):
            try:
                return self.lifecycle.begin("fixture", "prepare")
            except self.lifecycle.Conflict:
                return None
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(begin, range(4)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_restart_recovery_fences_old_completion(self):
        self.workspace(services=True)
        old = self.lifecycle.begin("fixture", "restart")
        old.advance("starting_app")
        self.lifecycle.recover_interrupted()
        self.assertEqual(self.lifecycle.get("fixture")["phase"], "interrupted")
        current = self.lifecycle.begin("fixture", "restart")
        with self.assertRaises(self.lifecycle.Superseded):
            old.advance("app_ready", finish=True)
        old.fail(RuntimeError("old worker"))
        self.assertEqual(self.lifecycle.get("fixture")["operation_id"], current.id)
        current.advance("starting_app")
        current.advance("app_ready", finish=True)

    def test_recreated_name_does_not_accept_old_callbacks(self):
        old = self.workspace()
        deletion = self.lifecycle.begin("fixture", "delete")
        deletion.advance("deleting")
        deletion.advance("deleted", finish=True)
        new = self.lifecycle.begin("fixture", "create")
        with self.assertRaises(self.lifecycle.Superseded):
            old.advance("workspace_ready", finish=True)
        self.assertEqual(self.lifecycle.get("fixture")["operation_id"], new.id)

    def test_paused_keeper_does_not_delete_a_replacement_using_an_old_observation(self):
        self.workspace()
        replacement = SimpleNamespace(**{**vars(self.info), "sandbox_id": "new-vm"})
        from sbx_control import keeper
        with patch.object(self.core, "find", return_value=replacement), patch.object(self.core, "delete") as delete:
            keeper.delete_paused(self.info)
        delete.assert_not_called()

    def test_public_api_excludes_unregistered_same_owner_sandboxes(self):
        foreign = SimpleNamespace(**{**vars(self.info), "sandbox_id": "foreign-vm",
                                  "metadata": {**self.info.metadata, "sbx": "foreign-controller"}})
        with patch.object(self.core, "sandboxes", return_value=[foreign, self.info]), \
                patch("sbx_control.sandboxes.describe", side_effect=lambda info: {"name": info.metadata["sbx"]}):
            self.assertEqual(self.client.get("/api/sandboxes").json(), [{"name": "fixture"}])
        with patch.object(self.core, "find", return_value=foreign) as find, \
                patch.object(self.core, "delete") as delete, patch.object(self.core.runtime, "kill") as kill:
            for method, path in (("get", ""), ("post", "/prepare"), ("delete", "")):
                response = getattr(self.client, method)("/api/sandboxes/foreign-controller" + path)
                self.assertEqual(response.status_code, 404, response.text)
        find.assert_not_called()
        delete.assert_not_called()
        kill.assert_not_called()

    def test_a_new_sandbox_publishes_its_own_host_key_never_the_template_s(self):
        checkout = checkout_of(Path(self.enterContext(tempfile.TemporaryDirectory())))
        recipe = self.recipes.App("owner/repo", checkout.parent / "recipe", {"workdir": str(checkout)})
        vm = FakeVM("fresh-vm")
        with self.creating(recipe, vm):
            response = self.client.post("/api/sandboxes?start_app=false",
                                        json={"name": "fresh-key", "repo": "owner/repo", "public_keys": ""})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["host_key"], vm.host_key)
        self.assertNotEqual(vm.host_key, FakeVM.TEMPLATE_KEY)
        with self.creating(recipe, FakeVM("failed-vm", identity_fails=True)):
            response = self.client.post("/api/sandboxes?start_app=false",
                                        json={"name": "failed-key", "repo": "owner/repo", "public_keys": ""})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("template-key", response.text)
        self.assertFalse(self.lifecycle.get("failed-key")["workspace_ready"])

    def test_public_keys_may_be_authorized_keys_text_or_a_list(self):
        checkout = checkout_of(Path(self.enterContext(tempfile.TemporaryDirectory())))
        recipe = self.recipes.App("owner/repo", checkout.parent / "recipe", {"workdir": str(checkout)})
        for name, keys in (("keys-list", ["ssh-ed25519 AAAA one", " ssh-ed25519 BBBB two ", ""]),
                           ("keys-text", "ssh-ed25519 AAAA one\nssh-ed25519 BBBB two\n")):
            vm, written = FakeVM(f"{name}-vm"), {}
            vm.files = SimpleNamespace(write=lambda path, content, **_: written.__setitem__(path, content))
            with self.creating(recipe, vm):
                response = self.client.post("/api/sandboxes?start_app=false",
                                            json={"name": name, "repo": "owner/repo", "public_keys": keys})
            self.assertEqual(response.status_code, 200, response.text)
            authorized = next(v for k, v in written.items() if k.endswith("/.ssh/authorized_keys"))
            self.assertEqual(authorized, "ssh-ed25519 AAAA one\nssh-ed25519 BBBB two\n")
        response = self.client.post("/api/sandboxes", json={"name": "keys-bad", "public_keys": [1, 2]})
        self.assertEqual(response.status_code, 422)

    def test_preview_urls_never_replace_the_sandbox_s_own_fields(self):
        checkout = checkout_of(Path(self.enterContext(tempfile.TemporaryDirectory())))
        recipe = self.recipes.App("owner/repo", checkout.parent / "recipe", {"workdir": str(checkout)})
        urls = {"web": "https://web.example.test", "repo": "https://repo.example.test", "name": "https://n.example.test"}
        with self.creating(recipe, FakeVM("preview-names-vm")), \
                patch.object(self.core.addressing, "urls", return_value=urls):
            made = self.core.create("preview-names", "preview-names", "owner@example.test", "main", "", "owner/repo",
                                    template="fixture-template")
        self.assertEqual((made["name"], made["repo"], made["urls"]), ("preview-names", str(checkout), urls))
        self.assertNotIn("web", made)

    def test_a_slug_must_be_a_free_dns_label_and_no_host_of_the_installation(self):
        recipe = self.recipes.App("owner/repo", Path("/unused"), {})
        with self.creating(recipe, FakeVM()) as create:
            for slug in ("sandbox", self.core.settings.current().domain.login_host, "Not_A_Label"):
                response = self.client.post("/api/sandboxes?start_app=false", json={
                    "name": "slug-check", "slug": slug, "repo": "owner/repo", "public_keys": ""})
                self.assertEqual(response.status_code, 400, response.text)
        create.assert_not_called()

    def test_failed_sandbox_name_cannot_be_reclaimed_by_a_different_owner(self):
        self.lifecycle.begin("fixture", "create").fail(RuntimeError("fixture failure"))
        self.app.app.dependency_overrides[self.auth.api_user] = lambda: "other@example.test"
        with patch.object(self.core, "find", return_value=None), patch.object(self.core, "create") as create:
            response = self.client.post("/api/sandboxes", json={"name": "fixture", "public_keys": ""})
        self.assertEqual(response.status_code, 409)
        create.assert_not_called()
        with self.state.db() as conn:
            self.assertEqual(conn.execute("select owner from sandboxes where name='fixture'").fetchone()[0],
                             "owner@example.test")

    def test_paused_runtime_is_not_ready_even_after_healthy_start(self):
        self.healthy()
        self.lifecycle.observe("fixture", "fixture-vm", "paused")
        current = self.lifecycle.get("fixture")
        self.assertEqual(current["application"], "healthy")
        self.assertEqual(current["readiness"], "not_ready")
        self.assertFalse(current["workspace_usable"])

    def test_replacement_vm_cannot_inherit_readiness_or_service_credentials(self):
        self.healthy()
        current = self.lifecycle.get("fixture", "running", "replacement-vm")
        self.assertFalse(current["binding_matches"])
        self.assertEqual(current["readiness"], "not_ready")
        with self.assertRaises(self.lifecycle.Conflict):
            self.lifecycle.assert_binding("fixture", "replacement-vm")

    def test_control_startup_reports_interruption_without_replaying_setup(self):
        from fastapi.testclient import TestClient
        self.workspace()
        old = self.lifecycle.begin("fixture", "prepare")
        with patch.object(self.core, "prepare_and_start") as prepare, patch.object(self.core, "create") as create:
            with TestClient(self.app.app) as client:
                with patch.object(self.core, "find", return_value=self.info):
                    response = client.get("/api/sandboxes/fixture/lifecycle")
            self.assertEqual(response.json()["phase"], "interrupted")
            prepare.assert_not_called()
            create.assert_not_called()
        with self.assertRaises(self.lifecycle.Superseded):
            old.check()

    def test_lifecycle_endpoint_is_owner_scoped_and_shows_missing_vm(self):
        self.workspace()
        with patch.object(self.core, "find", return_value=None):
            current = self.client.get("/api/sandboxes/fixture/lifecycle").json()
        self.assertEqual(current["runtime"], "missing")
        self.assertEqual(current["readiness"], "not_ready")
        self.app.app.dependency_overrides[self.auth.api_user] = lambda: "other@example.test"
        self.assertEqual(self.client.get("/api/sandboxes/fixture/lifecycle").status_code, 404)

    def test_missing_vm_can_still_clean_its_owned_resources(self):
        self.workspace(services=True)
        with patch.object(self.core, "find", return_value=None), \
                patch.object(self.core.extensions.packages(), "release", return_value=[]) as cleanup:
            response = self.client.delete("/api/sandboxes/fixture")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(cleanup.call_args.args[0], "fixture")
        self.assertEqual(self.lifecycle.get("fixture")["phase"], "deleted")
        self.assertEqual(self.client.get("/api/sandboxes/fixture/lifecycle").status_code, 404)

    def test_deleting_a_deleted_sandbox_again_does_nothing(self):
        # A Claude session whose sandbox the keeper deleted deletes it again
        # before it makes a new one.
        from sbx_control import sandboxes
        self.workspace()
        with patch.object(self.core, "find", return_value=None), \
                patch.object(self.core.extensions.packages(), "release", return_value=[]) as cleanup:
            sandboxes.delete("fixture")
            sandboxes.delete("fixture")
        cleanup.assert_called_once_with("fixture")
        self.assertEqual(self.lifecycle.get("fixture")["phase"], "deleted")

    def cleanup_recipe(self, script="#!/bin/sh\necho released\n", executable=True, env=None):
        """The fixture's recipe with a .sbx/cleanup (and env: its sbx.yaml's env)."""
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (folder / "sbx.yaml").write_text("previews: {'': 3000}\n")
        if script is not None:
            (folder / "cleanup").write_text(script)
            (folder / "cleanup").chmod(0o755 if executable else 0o644)
        recipe = self.recipes.App("owner/repo", folder, {"previews": {"": 3000}, "workdir": "/home/user/repo",
                                                         **({"env": env} if env else {})})
        self.enterContext(patch.object(self.recipes, "load", return_value=recipe))
        return recipe

    def deleting(self, info, vm, connect=None):
        """Delete the fixture through the API; (response, the runtime's kill mock, order of what ran)."""
        order = []
        vm.ran = order
        with patch.object(self.core, "find", return_value=info), \
                patch.object(self.core.runtime, "connect", side_effect=connect or (lambda *a, **k: vm)) as connected, \
                patch.object(self.core.runtime, "kill", side_effect=lambda vm_id: order.append(("kill", vm_id))) as kill, \
                patch.object(self.core.extensions.packages(), "release", return_value=[]):
            response = self.client.delete("/api/sandboxes/fixture")
        return response, kill, order, connected

    class CleanupVM(FakeVM):
        """A VM whose .sbx/cleanup prints or fails as told; it records the commands that ran it."""

        def __init__(self, exit_code=0, output="released\n", has_hook=True, app_env="", env="", readable=""):
            super().__init__()
            self.exit_code, self.output, self.ran = exit_code, output, []
            self.has_hook, self.app_env, self.env, self.readable = has_hook, app_env, env, readable

        def run(self, command, **kwargs):
            from sbx_control import runtime
            if "test -f /run/sbx/env" in command:
                return done("ok")
            if "echo present" in command:
                return done("present" if self.has_hook else "")
            if command.startswith("cat /run/sbx/env && echo && "):
                if self.app_env is None:
                    raise runtime.CommandFailed(stdout="", stderr="unreadable", exit_code=1)
                return done(self.env + "\n" + self.app_env)
            if command.startswith("test -r /run/sbx/"):
                return done(self.readable)
            if '"$d/cleanup"' in command:
                self.ran.append(("cleanup", command, kwargs.get("timeout")))
                if self.exit_code:
                    raise runtime.CommandFailed(stdout=self.output, stderr="it broke\n", exit_code=self.exit_code)
                return done(self.output)
            return super().run(command, **kwargs)

    def test_delete_runs_the_recipe_cleanup_first_and_reports_its_output(self):
        self.healthy()
        self.cleanup_recipe()
        vm = self.CleanupVM()
        response, kill, order, _ = self.deleting(self.info, vm)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["cleanup"], {"exit_code": 0, "output": "released\n"})
        self.assertEqual([step[0] for step in order], ["cleanup", "kill"])
        command, timeout = order[0][1], order[0][2]
        self.assertIn(". /run/sbx/app-env", command)              # the app's env, as setup gets
        self.assertIn("could not be loaded", command)             # and nothing runs without it
        self.assertIn("cd /home/user/repo", command)
        self.assertEqual(timeout, self.core.CLEANUP_TIMEOUT_SECONDS)
        self.assertIn(("cleanup ran", "exit 0: released"),
                      [(kind, detail) for _, kind, detail, _ in self.state.events("fixture")])
        self.assertEqual(self.lifecycle.get("fixture")["phase"], "deleted")

    def test_a_failing_cleanup_is_recorded_and_the_sandbox_is_deleted_anyway(self):
        self.healthy()
        self.cleanup_recipe()
        response, kill, order, _ = self.deleting(self.info, self.CleanupVM(exit_code=3, output="half done\n"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["cleanup"]["exit_code"], 3)
        self.assertIn("half done", response.json()["cleanup"]["output"])
        self.assertIn("it broke", response.json()["cleanup"]["output"])
        kill.assert_called_once_with("fixture-vm")
        self.assertIn("cleanup failed", [kind for _, kind, _, _ in self.state.events("fixture")])
        self.assertEqual(self.lifecycle.get("fixture")["phase"], "deleted")

    def test_a_paused_sandbox_is_resumed_for_its_cleanup(self):
        self.healthy()
        self.cleanup_recipe()
        paused = SimpleNamespace(**{**vars(self.info), "state": "paused"})
        vm = self.CleanupVM()
        with patch.object(self.core.extensions, "on_resume"):
            response, kill, order, connected = self.deleting(paused, vm)
        self.assertEqual(response.status_code, 200, response.text)
        connected.assert_called_once()
        self.assertEqual([step[0] for step in order], ["cleanup", "kill"])
        self.assertIn("resumed", [kind for _, kind, _, _ in self.state.events("fixture")])

    def test_a_paused_sandbox_whose_resume_fails_is_deleted_without_its_cleanup(self):
        self.healthy()
        self.cleanup_recipe()
        paused = SimpleNamespace(**{**vars(self.info), "state": "paused"})

        def refuse(*args, **kwargs):
            raise RuntimeError("no room to resume")
        response, kill, order, _ = self.deleting(paused, self.CleanupVM(), connect=refuse)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("no room to resume", response.json()["cleanup"]["error"])
        self.assertEqual(order, [("kill", "fixture-vm")])
        self.assertEqual(self.lifecycle.get("fixture")["phase"], "deleted")

    def test_without_a_cleanup_hook_delete_does_not_touch_the_vm(self):
        self.healthy()
        self.cleanup_recipe(script=None)
        paused = SimpleNamespace(**{**vars(self.info), "state": "paused"})
        response, kill, order, connected = self.deleting(paused, self.CleanupVM())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["cleanup"])
        connected.assert_not_called()   # a paused sandbox is not woken for nothing
        self.assertEqual(order, [("kill", "fixture-vm")])

    def test_a_cleanup_that_is_not_executable_is_skipped_with_a_reason(self):
        self.healthy()
        self.cleanup_recipe(executable=False)
        response, kill, order, connected = self.deleting(self.info, self.CleanupVM())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("not executable", response.json()["cleanup"]["error"])
        connected.assert_not_called()
        self.assertIn("cleanup skipped", [kind for _, kind, _, _ in self.state.events("fixture")])

    def test_an_unreadable_recipe_is_a_failed_cleanup_not_a_missing_one(self):
        self.healthy()
        self.cleanup_recipe()
        with patch.object(self.recipes, "load", side_effect=ValueError("bad yaml")):
            response, kill, order, connected = self.deleting(self.info, self.CleanupVM())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["cleanup"],
                         {"error": "the recipe's sbx.yaml could not be read, so .sbx/cleanup did not run"})
        connected.assert_not_called()
        kill.assert_called_once_with("fixture-vm")
        self.assertIn("cleanup failed", [kind for _, kind, _, _ in self.state.events("fixture")])

    def test_a_checkout_without_the_hook_is_reported_as_skipped(self):
        self.healthy()
        self.cleanup_recipe()
        response, kill, order, _ = self.deleting(self.info, self.CleanupVM(has_hook=False))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("did not run", response.json()["cleanup"]["error"])
        self.assertEqual(order, [("kill", "fixture-vm")])
        kinds = [kind for _, kind, _, _ in self.state.events("fixture")]
        self.assertIn("cleanup skipped", kinds)
        self.assertNotIn("cleanup ran", kinds)

    def test_the_hook_check_looks_where_the_hook_would_run(self):
        from sbx_control import sandbox_runtime
        shipped = Path(self.enterContext(tempfile.TemporaryDirectory()))
        marker = shipped / "ran"
        (shipped / "cleanup").write_text(f"#!/bin/sh\ntouch {marker}\n")
        (shipped / "cleanup").chmod(0o755)
        checkout = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (checkout / ".sbx").mkdir()
        recipe = self.recipes.App("owner/repo", shipped, {"workdir": str(checkout)})

        class Local:
            def __init__(self):
                self.commands = self

            def run(self, command, **_):
                return subprocess.run(["bash", "-c", command], capture_output=True, text=True)
        with patch.object(sandbox_runtime, "APP_SCRIPTS", str(shipped)):
            vm = Local()
            # The checkout's .sbx/ governs, and has no cleanup: neither checked nor run.
            self.assertFalse(sandbox_runtime.has_app_script(vm, "cleanup", recipe))
            with patch.object(self.core, "run_in", side_effect=lambda sbx, command, **_: vm.run(command)):
                sandbox_runtime.app_script(vm, "cleanup", recipe)
            self.assertFalse(marker.exists())
            (checkout / ".sbx" / "cleanup").write_text(f"#!/bin/sh\ntouch {marker}\n")
            (checkout / ".sbx" / "cleanup").chmod(0o755)
            self.assertTrue(sandbox_runtime.has_app_script(vm, "cleanup", recipe))

    def test_cleanup_output_is_masked_with_the_env_the_hook_ran_with(self):
        self.healthy()
        self.cleanup_recipe()
        vm = self.CleanupVM(output="token old-app-secret-1\n", app_env="export APP_TOKEN=old-app-secret-1\n")
        response, *_ = self.deleting(self.info, vm)
        self.assertNotIn("old-app-secret-1", response.text)
        self.assertNotIn("old-app-secret-1", str(self.state.events("fixture")))
        # When the env it ran with can't be read, only its exit status is told.
        self.assertNotIn("old-app-secret-1", self.core._cleanup_output("token old-app-secret-1", self.info, None))

    def test_cleanup_output_is_masked_with_the_sandbox_env_the_app_env_sources(self):
        self.healthy()
        self.cleanup_recipe()
        vm = self.CleanupVM(output="token rotated-away-1\n", env="export SHARED_TOKEN=rotated-away-1\n")
        response, *_ = self.deleting(self.info, vm)
        self.assertNotIn("rotated-away-1", response.text)
        self.assertNotIn("rotated-away-1", str(self.state.events("fixture")))

    def test_cleanup_output_is_withheld_when_the_env_sources_a_login_sbx_does_not_hold(self):
        self.healthy()
        self.cleanup_recipe()
        sources = "if [ -r /run/sbx/claude-env ]; then . /run/sbx/claude-env; fi\n"
        vm = self.CleanupVM(output="token agent-login-token-1\n", env=sources, readable="claude-env\n")
        response, *_ = self.deleting(self.info, vm)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["cleanup"]["exit_code"], 0)
        self.assertIn("cannot mask", response.json()["cleanup"]["output"])
        self.assertNotIn("agent-login-token-1", response.text)
        self.assertNotIn("agent-login-token-1", str(self.state.events("fixture")))

    def test_cleanup_output_is_shown_when_a_sourced_login_is_not_there(self):
        self.healthy()
        self.cleanup_recipe()
        sources = "if [ -r /run/sbx/claude-env ]; then . /run/sbx/claude-env; fi\n"
        response, *_ = self.deleting(self.info, self.CleanupVM(output="released\n", env=sources))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["cleanup"]["output"], "released\n")

    def test_a_short_secret_rotated_since_is_withheld_from_cleanup_output(self):
        self.healthy()
        self.cleanup_recipe()
        self.trusted()
        self.core.variables.put("owner/repo", "PIN", "n3w9", "test")
        response, *_ = self.deleting(self.info, self.CleanupVM(output="pin 0ld7\n", env="export PIN=0ld7\n"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("too short to mask", response.json()["cleanup"]["output"])
        self.assertNotIn("0ld7", response.text)

    def test_cleanup_output_holding_a_short_secret_is_withheld(self):
        self.healthy()
        self.cleanup_recipe()
        self.trusted()
        self.core.variables.put("owner/repo", "PIN", "k3y9", "test")
        response, *_ = self.deleting(self.info, self.CleanupVM(output="logged in with k3y9\n"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("k3y9", response.text)
        self.assertIn("too short to mask", response.json()["cleanup"]["output"])

    def test_a_short_secret_rotated_since_under_an_alias_is_withheld(self):
        self.healthy()
        self.cleanup_recipe(env={"SERVICE_PIN": "${secret:PIN}"})
        self.trusted()
        self.core.variables.put("owner/repo", "PIN", "n3w9", "test")
        response, *_ = self.deleting(self.info, self.CleanupVM(output="pin 0ld7\n",
                                                              app_env="export SERVICE_PIN=0ld7\n"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("too short to mask", response.json()["cleanup"]["output"])
        self.assertNotIn("0ld7", response.text)
        self.assertNotIn("0ld7", str(self.state.events("fixture")))

    def test_an_installation_secret_the_sandbox_was_not_given_does_not_change_its_output(self):
        self.healthy()
        self.cleanup_recipe()
        self.trusted()
        self.core.variables.put(None, "PIN", "k3y9", "test")   # named by no env the sandbox gets
        response, *_ = self.deleting(self.info, self.CleanupVM(output="code k3y9\n"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["cleanup"]["output"], "code k3y9\n")

    def trusted(self):
        self.enterContext(patch.object(self.core.settings, "trust", return_value="trusted"))

    def test_mask_hides_everything_when_the_recipe_cannot_be_read(self):
        with patch.object(self.recipes, "load", side_effect=ValueError("bad yaml")):
            self.assertIn("hidden", self.core.mask("a secret", self.info))

    def test_a_resume_for_cleanup_that_needs_the_app_started_invalidates_it(self):
        self.healthy()
        self.cleanup_recipe()

        def fail(vm_id):
            raise RuntimeError("the runtime refused the kill")
        with patch.object(self.core, "find", return_value=self.info), \
                patch.object(self.core.runtime, "connect", return_value=self.CleanupVM()), \
                patch.object(self.core, "restore_runtime", return_value=True) as restore, \
                patch.object(self.core.runtime, "kill", side_effect=fail), \
                patch.object(self.core.extensions.packages(), "release", return_value=[]):
            with self.assertRaises(RuntimeError):
                self.core.delete("fixture")
        self.assertEqual(restore.call_args.kwargs, {"start_app": False})
        self.assertNotEqual(self.lifecycle.get("fixture")["application"], "healthy")

    def test_a_restore_for_cleanup_that_fails_invalidates_the_app(self):
        self.healthy()
        self.cleanup_recipe()

        def fail(*args, **kwargs):
            raise RuntimeError("writing the env failed")
        with patch.object(self.core, "find", return_value=self.info), \
                patch.object(self.core.runtime, "connect", return_value=self.CleanupVM()), \
                patch.object(self.core, "restore_runtime", side_effect=fail), \
                patch.object(self.core.runtime, "kill", side_effect=fail), \
                patch.object(self.core.extensions.packages(), "release", return_value=[]):
            with self.assertRaises(RuntimeError):
                self.core.delete("fixture")
        self.assertNotEqual(self.lifecycle.get("fixture")["application"], "healthy")

    def test_a_restore_for_cleanup_owes_the_restart_before_writing_the_env(self):
        from sbx_control import delivery
        self.healthy()
        delivery.queue("fixture", ["FEATURE"], True)
        paused = SimpleNamespace(**{**vars(self.info), "state": "paused"})
        with patch.object(self.core.extensions, "on_resume"), \
                patch.object(self.core, "_refresh_env", side_effect=RuntimeError("part way")):
            with self.assertRaises(RuntimeError):
                self.core.restore_runtime(self.CleanupVM(), paused, start_app=False)
        self.assertTrue(delivery.restart_required("fixture"))

    def test_cleanup_does_not_run_when_the_app_env_cannot_be_loaded(self):
        marker = Path(self.enterContext(tempfile.TemporaryDirectory())) / "ran"

        class Local:
            def __init__(self):
                self.commands = self

            def run(self, command, **_):
                return subprocess.run(["bash", "-c", command], capture_output=True, text=True)
        result = self.core.run_in(Local(), f"touch {marker}", env="/nonexistent/app-env", env_required=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not be loaded", result.stderr)
        self.assertFalse(marker.exists())

    def test_cleanup_does_not_run_when_the_env_the_app_env_sources_cannot_be_loaded(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        marker = folder / "ran"

        class Local:
            def __init__(self):
                self.commands = self

            def run(self, command, **_):
                return subprocess.run(["bash", "-c", command], capture_output=True, text=True)
        # env is missing: as an app-env from before writes it, and as one written now
        for first in (f". {folder}/env\n", f". {folder}/env || return 1\n"):
            (folder / "app-env").write_text(first + "export APP_ONLY=1\n")
            result = self.core.run_in(Local(), f"touch {marker}", env=f"{folder}/app-env", env_required=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(f"{folder}/env could not be loaded", result.stderr)
            self.assertFalse(marker.exists())
        (folder / "env").write_text("export SHARED=1\n")   # readable: the hook runs
        result = self.core.run_in(Local(), f"touch {marker}", env=f"{folder}/app-env", env_required=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists())

    def test_a_shadowed_short_installation_secret_is_still_withheld(self):
        self.healthy()
        self.cleanup_recipe()
        self.trusted()
        self.enterContext(patch.object(self.core.variables, "installation_layers",
                                       return_value=[{"SERVICE_PIN": "${secret:PIN}"}]))
        self.core.variables.put(None, "PIN", "k3y9", "test")
        self.core.variables.put("owner/repo", "PIN", "a-longer-personal-pin", "test", person="owner@example.test")
        response, *_ = self.deleting(self.info, self.CleanupVM(output="logged in with k3y9\n",
                                                              app_env="export SERVICE_PIN=k3y9\n"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("k3y9", response.text)
        self.assertIn("too short to mask", response.json()["cleanup"]["output"])

    def test_a_short_repo_secret_under_an_alias_shadowed_by_a_personal_one_is_withheld(self):
        self.healthy()
        self.cleanup_recipe()
        self.trusted()
        self.core.variables.put("owner/repo", "PIN", "r3p9", "test")
        self.core.variables.put("owner/repo", "PIN", "a-longer-personal-pin", "test", person="owner@example.test")
        response, *_ = self.deleting(self.info, self.CleanupVM(output="logged in with r3p9\n",
                                                              app_env="export SERVICE_PIN=r3p9\n"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("r3p9", response.text)
        self.assertNotIn("r3p9", str(self.state.events("fixture")))
        self.assertIn("too short to mask", response.json()["cleanup"]["output"])

    def test_a_restore_for_cleanup_writes_queued_env_without_restarting_and_keeps_it_queued(self):
        from sbx_control import delivery
        self.healthy()
        delivery.queue("fixture", ["FEATURE"], True)
        paused = SimpleNamespace(**{**vars(self.info), "state": "paused"})
        with patch.object(self.core.extensions, "on_resume"), \
                patch.object(self.core, "_refresh_env", return_value={"restarted": False}) as refresh:
            self.assertTrue(self.core.restore_runtime(self.CleanupVM(), paused, start_app=False))
        refresh.assert_called_once()
        self.assertFalse(refresh.call_args.args[2])          # no restart
        self.assertIsNotNone(delivery.pending("fixture"))    # still owed, should the delete fail
        self.assertTrue(delivery.restart_required("fixture"))

    def test_a_restart_requested_while_cleanup_refreshes_the_env_is_owed(self):
        from sbx_control import delivery
        self.healthy()
        delivery.queue("fixture", ["FEATURE"], False)
        paused = SimpleNamespace(**{**vars(self.info), "state": "paused"})

        def refresh(sbx, info, restart):   # a second update, asking for a restart, lands as it reads the store
            delivery.queue("fixture", ["OTHER"], True)
            return {"restarted": False}
        with patch.object(self.core.extensions, "on_resume"), \
                patch.object(self.core, "_refresh_env", side_effect=refresh):
            self.assertTrue(self.core.restore_runtime(self.CleanupVM(), paused, start_app=False))
        self.assertTrue(delivery.restart_required("fixture"))

    def test_a_cleanup_refresh_with_no_restart_requested_owes_none(self):
        from sbx_control import delivery
        self.healthy()
        delivery.queue("fixture", ["FEATURE"], False)
        paused = SimpleNamespace(**{**vars(self.info), "state": "paused"})
        with patch.object(self.core.extensions, "on_resume"), \
                patch.object(self.core, "_refresh_env", return_value={"restarted": False}):
            self.core.restore_runtime(self.CleanupVM(), paused, start_app=False)
        self.assertFalse(delivery.restart_required("fixture"))

    def test_cleanup_treats_a_vm_sbx_just_paused_as_paused(self):
        self.healthy()
        self.cleanup_recipe()
        seen = []
        with patch.object(self.core, "_paused_by_us", return_value=True), \
                patch.object(self.core, "restore_runtime",
                             side_effect=lambda sbx, info, **kw: seen.append(info.state) or False):
            response, *_ = self.deleting(self.info, self.CleanupVM())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.info.state, "running")   # the listing's record is left alone
        self.assertEqual(seen, ["paused"])

    def test_the_keeper_runs_the_cleanup_of_a_long_paused_sandbox_it_deletes(self):
        from datetime import datetime
        from sbx_control import keeper
        self.healthy()
        self.cleanup_recipe()
        paused = SimpleNamespace(**{**vars(self.info), "state": "paused"})
        vm = self.CleanupVM()
        order = []
        vm.ran = order
        with patch.object(self.core, "find", return_value=paused), \
                patch.object(self.core.runtime, "connect", return_value=vm), \
                patch.object(self.core.runtime, "kill", side_effect=lambda vm_id: order.append(("kill", vm_id))), \
                patch.object(self.core.extensions, "on_resume"), \
                patch.object(self.core.extensions.packages(), "release", return_value=[]), \
                patch.object(keeper.extensions, "keeper_policy", return_value=None):
            keeper.delete_paused(SimpleNamespace(**vars(paused), end_at=datetime(2026, 1, 1)))
        self.assertEqual([step[0] for step in order], ["cleanup", "kill"])

    def test_deleting_again_while_the_runtime_still_lists_the_killed_vm_does_nothing_more(self):
        # The runtime lists a killed VM for a while (gate.gone).
        from sbx_control import sandboxes
        self.workspace()
        with patch.object(self.core, "find", return_value=self.info), patch.object(self.core.runtime, "kill") as kill, \
                patch.object(self.core.extensions.packages(), "release", return_value=[]) as cleanup:
            sandboxes.delete("fixture")
            sandboxes.delete("fixture")
        cleanup.assert_called_once_with("fixture")
        self.assertEqual([c.args for c in kill.call_args_list], [("fixture-vm",), ("fixture-vm",)])
        self.assertEqual(self.lifecycle.get("fixture")["phase"], "deleted")

    def test_every_way_out_of_queued_begins_some_operation(self):
        self.assertEqual(self.lifecycle.EDGES["queued"], set().union(*self.lifecycle.FIRST.values()))

    def test_a_sandbox_whose_name_became_reserved_can_still_be_deleted(self):
        self.workspace()
        with patch.object(self.core, "preview_suffixes", return_value={"ture"}), \
                patch.object(self.core, "find", return_value=self.info), patch.object(self.core.runtime, "kill") as kill, \
                patch.object(self.core.extensions.packages(), "release", return_value=[]):
            response = self.client.delete("/api/sandboxes/fixture")
        self.assertEqual(response.status_code, 200, response.text)
        kill.assert_called_once_with("fixture-vm")

    def test_busy_restart_returns_conflict_without_starting_a_worker(self):
        self.workspace()
        self.lifecycle.begin("fixture", "prepare")
        with patch.object(self.core, "find", return_value=self.info), patch.object(self.core, "restart") as restart:
            response = self.client.post("/api/sandboxes/fixture/restart")
        self.assertEqual(response.status_code, 409)
        restart.assert_not_called()

    def test_a_paused_sandbox_reports_paused(self):
        self.healthy()
        paused = SimpleNamespace(**{**vars(self.info), "state": "paused"})
        with patch.object(self.core, "find", return_value=self.info), patch.object(self.core.runtime, "pause"):
            self.assertEqual(self.client.post("/api/sandboxes/fixture/pause").status_code, 200)
        with patch.object(self.core, "find", return_value=paused):
            self.assertEqual(self.client.get("/api/sandboxes/fixture").json()["status"], "paused")
        self.assertEqual(self.lifecycle.statuses()["fixture"], "paused")
        self.lifecycle.observe("fixture", "fixture-vm", "running")
        self.assertEqual(self.lifecycle.get("fixture")["status"], "up")

    def test_background_preparation_preserves_agent_edits(self):
        self.workspace(services=True)
        checkout = checkout_of(Path(self.enterContext(tempfile.TemporaryDirectory())))
        git("-C", checkout, "checkout", "-q", "-b", "agent-work")
        (checkout / "example.txt").write_text("the agent's edit\n")
        (checkout.parent / "recipe").mkdir()
        recipe = self.recipes.App("owner/repo", checkout.parent / "recipe",
                                  {"workdir": str(checkout), "health": ["http://localhost:3000/"]})
        with patch.object(self.core, "find", return_value=self.info), \
                patch.object(self.recipes, "load", return_value=recipe), \
                patch.object(self.core.runtime, "connect", return_value=FakeVM()), \
                patch.object(self.core.time, "sleep"):
            self.core.prepare_and_start("fixture", "main")
        self.assertEqual((checkout / "example.txt").read_text(), "the agent's edit\n")
        self.assertEqual(git("-C", checkout, "branch", "--show-current").stdout.strip(), "agent-work")
        self.assertEqual(self.lifecycle.get("fixture")["phase"], "app_ready")

    def test_a_pr_preview_follows_a_push_to_its_branch(self):
        from sbx_control import github_api
        self.workspace(services=True)
        checkout = checkout_of(Path(self.enterContext(tempfile.TemporaryDirectory())))
        source = checkout.parent / "source"
        (source / "example.txt").write_text("pushed\n")
        git("-C", source, "commit", "-qam", "pushed")
        (checkout.parent / "recipe").mkdir()
        recipe = self.recipes.App("owner/repo", checkout.parent / "recipe",
                                  {"workdir": str(checkout), "health": ["http://localhost:3000/"]})
        with patch.object(github_api, "preview_name", return_value="fixture"), \
                patch.object(github_api.lifecycle, "in_background",
                             side_effect=lambda name, kind, work: work(self.lifecycle.begin(name, kind))), \
                patch.object(self.core, "find", return_value=self.info), \
                patch.object(self.recipes, "load", return_value=recipe), \
                patch.object(self.core.runtime, "connect", return_value=FakeVM()), \
                patch.object(self.core.time, "sleep"):
            github_api.upsert(7, "main", "owner/repo")
        self.assertEqual((checkout / "example.txt").read_text(), "pushed\n")
        self.assertEqual(self.lifecycle.get("fixture")["phase"], "app_ready")

    def test_a_pr_preview_follows_a_push_over_what_its_setup_changed(self):
        # Nobody edits a preview: what .sbx/setup rewrote (a lockfile) must not stop the push.
        from sbx_control import github_api
        self.workspace(services=True)
        checkout = checkout_of(Path(self.enterContext(tempfile.TemporaryDirectory())))
        source = checkout.parent / "source"
        (source / "example.txt").write_text("pushed\n")
        git("-C", source, "commit", "-qam", "pushed")
        (checkout / "example.txt").write_text("rewritten by setup\n")
        (checkout.parent / "recipe").mkdir()
        recipe = self.recipes.App("owner/repo", checkout.parent / "recipe",
                                  {"workdir": str(checkout), "health": ["http://localhost:3000/"]})
        with patch.object(github_api, "preview_name", return_value="fixture"), \
                patch.object(github_api.lifecycle, "in_background",
                             side_effect=lambda name, kind, work: work(self.lifecycle.begin(name, kind))), \
                patch.object(self.core, "find", return_value=self.info), \
                patch.object(self.recipes, "load", return_value=recipe), \
                patch.object(self.core.runtime, "connect", return_value=FakeVM()), \
                patch.object(self.core.time, "sleep"):
            github_api.upsert(7, "main", "owner/repo")
        self.assertEqual((checkout / "example.txt").read_text(), "pushed\n")
        self.assertEqual(self.lifecycle.get("fixture")["phase"], "app_ready")

    def test_a_checkout_with_local_edits_is_not_moved_and_the_refusal_names_them(self):
        checkout = checkout_of(Path(self.enterContext(tempfile.TemporaryDirectory())))
        (checkout / "example.txt").write_text("an edit\n")
        recipe = self.recipes.App("owner/repo", checkout.parent / "recipe", {"workdir": str(checkout)})
        with self.assertRaises(self.core.SandboxError) as raised:
            self.core.prepare_workspace(FakeVM(), recipe, "feature", checkout=True)
        self.assertIn("M example.txt", str(raised.exception))
        self.assertEqual((checkout / "example.txt").read_text(), "an edit\n")

    def test_a_push_while_the_preview_is_busy_is_prepared_once_it_is_free(self):
        # Two pushes minutes apart are normal; the second must not fail the preview.
        from sbx_control import github_api, github_app, keeper, sandboxes
        self.workspace(services=True)
        with self.state.db() as conn:
            conn.execute("update sandboxes set owner = ? where name = 'fixture'", (github_api.PREVIEW_OWNER,))
        preview = SimpleNamespace(**{**vars(self.info), "metadata": {**self.info.metadata,
                                                                      "owner": github_api.PREVIEW_OWNER}})
        running = self.lifecycle.begin("fixture", "prepare")   # the first push's, still running
        payload = {"action": "synchronize", "number": 7, "repository": {"full_name": "owner/repo"},
                   "pull_request": {"state": "open", "labels": [{"name": "preview"}],
                                    "head": {"ref": "feature", "repo": {"full_name": "owner/repo"}}}}
        with patch.object(github_api, "preview_name", return_value="fixture"), \
                patch.object(github_app, "verify", return_value=True), \
                patch.object(self.core.settings, "trust", return_value="trusted"), \
                patch.object(self.core, "find", return_value=preview), \
                patch.object(github_api.threading, "Thread"), \
                patch.object(github_app, "comment_preview") as comment:
            response = self.client.post("/github/webhook", json=payload, headers={"x-github-event": "pull_request"})
        self.assertEqual((response.status_code, response.json()), (200, {"preview": "queued"}))
        comment.assert_not_called()
        round_ = (patch.object(keeper.core, "sandboxes", return_value=[preview]),
                  patch.object(keeper, "use_of"), patch.object(keeper, "keep_awake"),
                  patch.object(keeper.routing, "detect_ports"),
                  patch.object(keeper.extensions, "keeper_policy", return_value=None),
                  patch.object(sandboxes.lifecycle, "in_background",
                               side_effect=lambda name, kind, work: work(self.lifecycle.begin(name, kind))),
                  patch.object(self.core, "prepare_and_start"))
        with contextlib.ExitStack() as stack:
            prepare = [stack.enter_context(p) for p in round_][-1]
            keeper.look_once()
            prepare.assert_not_called()   # the first push's prepare still runs
            running.advance("preparing_workspace", sandbox_id="fixture-vm", runtime="running")
            running.advance("workspace_ready", finish=True)
            keeper.look_once()
            keeper.look_once()
        prepare.assert_called_once()
        self.assertEqual(prepare.call_args.args, ("fixture", "feature"))
        self.assertEqual((prepare.call_args.kwargs["checkout"], prepare.call_args.kwargs["force"]), (True, True))

    @unittest.skipUnless(sys.platform == "linux", "sandbox setup uses Linux flock")
    def test_surviving_remote_setup_excludes_a_second_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recipe_dir = root / ".sbx"
            recipe_dir.mkdir()
            script = recipe_dir / "setup"
            script.write_text("#!/bin/sh\ntouch started\nsleep 1\nprintf done >> completed\n")
            script.chmod(0o700)
            recipe = self.recipes.App("owner/repo", recipe_dir, {"workdir": str(root)})
            def run(_sandbox, command, **kwargs):
                return subprocess.run(["bash", "-ec", command], check=True, capture_output=True, text=True)
            from sbx_control import sandbox_runtime
            with patch.object(sandbox_runtime, "SETUP_LOCK", str(root / "setup.lock")), \
                    patch.object(self.core, "run_in", side_effect=run):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    first = pool.submit(self.core.app_script, Mock(), "setup", recipe, operation=Mock())
                    deadline = time.monotonic() + 5
                    while not (root / "started").exists():
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.01)
                    with self.assertRaises(subprocess.CalledProcessError):
                        self.core.app_script(Mock(), "setup", recipe, operation=Mock())
                    first.result(timeout=5)
            self.assertEqual((root / "completed").read_text(), "done")

    def test_create_api_waits_for_requested_branch_before_starting_app(self):
        checkout = checkout_of(Path(self.enterContext(tempfile.TemporaryDirectory())))
        recipe = self.recipes.App("owner/repo", checkout.parent / "recipe", {"workdir": str(checkout)})
        with self.creating(recipe, FakeVM()), patch.object(self.lifecycle, "in_background") as background:
            response = self.client.post("/api/sandboxes", json={
                "name": "source-check", "repo": "owner/repo", "branch": "feature", "public_keys": "",
            })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual((checkout / "example.txt").read_text(), "feature\n")
            self.assertTrue(response.json()["lifecycle"]["workspace_ready"])
            self.assertTrue(response.json()["lifecycle"]["services_ready"])
            background.assert_called_once()

    def test_a_ready_check_is_judged_by_its_exit_status_not_its_output(self):
        # Amp's check starts with `command -v amp`, which prints the path.
        from sbx_control import contributions
        def run(_sandbox, command, **kwargs):
            return subprocess.run(["bash", "-c", command], capture_output=True, text=True)
        cases = [(command, ready, where) for command, ready in (("command -v sh && echo noise && true", True),
                                                                 ("echo noise && false", False))
                 for where in ("contribution", "base_contribution")]   # Amp's is in the base
        for command, ready, where in cases:
            with self.subTest(command=command, where=where):
                connector = Mock(contribution=Mock(return_value=None), base_contribution=Mock(return_value=None))
                getattr(connector, where).return_value = contributions.Contribution(
                    "fixture", ready=contributions.Check(command))
                with patch.object(self.core.extensions, "connector_of", return_value=connector), \
                        patch.object(self.core, "target_of"), patch.object(self.core, "run_in", side_effect=run):
                    if ready:
                        self.core.verify_ready(Mock(), self.info)
                    else:
                        with self.assertRaises(self.core.NotReady):
                            self.core.verify_ready(Mock(), self.info)

    def test_a_new_sandbox_moves_over_what_its_template_s_build_changed(self):
        # The template's setup rewrote a tracked file (a generated route tree); nobody has edited yet.
        checkout = checkout_of(Path(self.enterContext(tempfile.TemporaryDirectory())))
        (checkout / "example.txt").write_text("generated by the template's setup\n")
        recipe = self.recipes.App("owner/repo", checkout.parent / "recipe", {"workdir": str(checkout)})
        with self.creating(recipe, FakeVM()), patch.object(self.lifecycle, "in_background"):
            response = self.client.post("/api/sandboxes", json={
                "name": "template-edits", "repo": "owner/repo", "branch": "feature", "public_keys": "",
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((checkout / "example.txt").read_text(), "feature\n")

    def test_a_create_before_the_repo_s_template_is_built_is_a_503_that_starts_its_build(self):
        from sbx_control import sandboxes, template_builds
        with patch.object(template_builds, "own", return_value=True), \
                patch.object(template_builds, "plan", return_value=SimpleNamespace(fingerprint="f1")), \
                patch.object(template_builds, "start") as start, \
                patch.object(template_builds, "template_for", side_effect=self.template_for):
            response = self.client.post("/api/sandboxes?start_app=false",
                                        json={"name": "too-early", "repo": "owner/repo", "public_keys": ""})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.headers["retry-after"], "60")
        self.assertIn("owner/repo template is building", response.json()["detail"])
        start.assert_called_once_with("owner/repo", "embed")
        self.assertIsNone(sandboxes.registered_owner("too-early"))

    def test_a_runtime_that_does_not_take_the_sandbox_asks_for_the_same_request_again(self):
        # As while `sbx-admin network apply` recreates Embed: no VM is made, so a retry may succeed.
        checkout = checkout_of(Path(self.enterContext(tempfile.TemporaryDirectory())))
        recipe = self.recipes.App("owner/repo", checkout.parent / "recipe", {"workdir": str(checkout)})
        body = {"name": "runtime-restart", "repo": "owner/repo", "public_keys": ""}
        with self.creating(recipe, FakeVM()) as create:
            create.side_effect = self.core.runtime.Failed("500: failed to create sandbox: orchestrator unavailable")
            response = self.client.post("/api/sandboxes?start_app=false", json=body)
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.headers["retry-after"], "30")
            self.assertIn("try again", response.json()["detail"])
            create.side_effect = None
            response = self.client.post("/api/sandboxes?start_app=false", json=body)
        self.assertEqual(response.status_code, 200, response.text)

    def test_a_failure_after_the_vm_exists_is_no_retry(self):
        recipe = self.recipes.App("owner/repo", Path("/unused"), {})
        vm = FakeVM("kept-vm")
        with self.creating(recipe, vm), \
                patch.object(self.core, "ensure_started", side_effect=self.core.runtime.Failed("envd did not answer")):
            response = self.client.post("/api/sandboxes?start_app=false", json={
                "name": "kept-vm", "repo": "owner/repo", "public_keys": ""})
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(self.lifecycle.get("kept-vm")["sandbox_id"], "kept-vm")

    def test_exact_source_commit_keeps_named_branch(self):
        checkout = checkout_of(Path(self.enterContext(tempfile.TemporaryDirectory())))
        source = checkout.parent / "source"
        selected = git("-C", source, "rev-parse", "HEAD").stdout.strip()
        (source / "example.txt").write_text("new upstream\n")
        git("-C", source, "commit", "-am", "new upstream")
        recipe = self.recipes.App("owner/repo", checkout.parent / "recipe", {"workdir": str(checkout)})
        body = {"name": "pinned-source", "repo": "owner/repo", "branch": "main", "source_commit": selected,
                "public_keys": ""}
        with self.creating(recipe, FakeVM("pinned-vm")):
            response = self.client.post("/api/sandboxes?start_app=false", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(git("-C", checkout, "symbolic-ref", "--short", "HEAD").stdout.strip(), "main")
        self.assertEqual(git("-C", checkout, "rev-parse", "HEAD").stdout.strip(), selected)

if __name__ == "__main__":
    unittest.main()
