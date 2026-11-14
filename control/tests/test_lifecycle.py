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
        """A new sandbox of recipe (a repo this installation serves publicly) on vm; yields Sandbox.create."""
        with patch.object(self.core, "find", return_value=None), \
                patch.object(self.recipes, "load", return_value=recipe), \
                patch.object(self.core.settings, "trust", return_value="public"), \
                patch.object(self.core.Sandbox, "connect", return_value=vm), \
                patch.object(self.core.Sandbox, "create", return_value=vm) as create:
            yield create

    def test_pause_runs_the_services_pause_hooks_after_the_vm_is_paused(self):
        marker = Path(self.enterContext(tempfile.TemporaryDirectory())) / "hooked"
        self.pause_hook(f"#!/bin/sh\necho \"$SBX_ID\" > {marker}\n")
        self.healthy()
        seen = []
        with patch.object(self.core.Sandbox, 'pause', side_effect=lambda _: seen.append(marker.exists())):
            self.core.pause('fixture', self.info)
        self.assertEqual(seen, [False])
        self.assertEqual(marker.read_text(), "fixture-db\n")
        self.assertEqual(self.lifecycle.get('fixture')['runtime'], 'paused')

    def test_pause_hook_failure_is_deferred_without_losing_the_pause(self):
        self.pause_hook("#!/bin/sh\necho unavailable >&2\nexit 1\n")
        self.healthy()
        with patch.object(self.core.Sandbox, 'pause'):
            self.core.pause('fixture', self.info)
        self.assertEqual(self.lifecycle.get('fixture')['runtime'], 'paused')
        self.assertEqual(self.lifecycle.get('fixture')['phase'], 'app_ready')
        self.assertIn(('service pause hook deferred', True), [(kind, internal) for _, kind, _, internal in self.state.events('fixture')])

    def amp_vm(self, state="running"):
        """The fixture as one of Amp's orbs, its bootstrap stops recorded in calls."""
        from sbx_control.connectors.amp import orbs
        self.info.metadata.update(connector="amp", orbId="orb-1")
        self.info.state = state
        calls = []
        self.enterContext(patch.object(orbs, "stop_bootstrap", side_effect=lambda *_: calls.append("bootstrap stopped")))
        self.enterContext(patch.object(self.core.Sandbox, "connect", return_value=Mock()))
        return calls

    def test_amp_pause_stops_the_bootstrap_before_the_checkpoint_and_the_vm_pause(self):
        self.healthy()
        calls = self.amp_vm()
        with patch.object(self.core.Sandbox, "pause", side_effect=lambda _: calls.append("paused")):
            self.core.pause("fixture", self.info, before_pause=lambda: calls.append("checkpoint"))
        self.assertEqual(calls, ["bootstrap stopped", "checkpoint", "paused"])

    def test_amp_pause_is_cancelled_when_the_bootstrap_does_not_stop(self):
        from sbx_control.connectors.amp import orbs
        self.healthy()
        self.amp_vm()
        with patch.object(orbs, "stop_bootstrap", side_effect=RuntimeError("transport unavailable")), \
                patch.object(self.core.Sandbox, "pause") as pause:
            with self.assertRaisesRegex(RuntimeError, "transport unavailable"):
                self.core.pause("fixture", self.info)
        pause.assert_not_called()

    def test_amp_pause_of_a_paused_vm_does_not_wake_it(self):
        self.healthy()
        calls = self.amp_vm(state="paused")
        with patch.object(self.core.Sandbox, "pause"):
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
        with patch.object(orbs, "stop_bootstrap") as stop, patch.object(self.core.Sandbox, "pause"):
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

        def room(memory_mb=None):  # the host's hugepages
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
        with patch.object(self.core.Sandbox, "connect", side_effect=RuntimeError("unavailable")), \
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
                patch.object(self.core, "delete") as delete, patch.object(self.core.Sandbox, "kill") as kill:
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

    def test_deleting_again_while_the_runtime_still_lists_the_killed_vm_does_nothing_more(self):
        # The runtime lists a killed VM for a while (gate.gone).
        from sbx_control import sandboxes
        self.workspace()
        with patch.object(self.core, "find", return_value=self.info), patch.object(self.core.Sandbox, "kill") as kill, \
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
                patch.object(self.core, "find", return_value=self.info), patch.object(self.core.Sandbox, "kill") as kill, \
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
        with patch.object(self.core, "find", return_value=self.info), patch.object(self.core.Sandbox, "pause"):
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
                patch.object(self.core.Sandbox, "connect", return_value=FakeVM()), \
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
                patch.object(self.core.Sandbox, "connect", return_value=FakeVM()), \
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
                patch.object(self.core.Sandbox, "connect", return_value=FakeVM()), \
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
            with patch.object(self.core, "SETUP_LOCK", str(root / "setup.lock")), \
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
        start.assert_called_once_with("owner/repo")
        self.assertIsNone(sandboxes.registered_owner("too-early"))

    def test_a_runtime_that_does_not_take_the_sandbox_asks_for_the_same_request_again(self):
        # As while `sbx-admin network apply` recreates Embed: no VM is made, so a retry may succeed.
        from e2b.exceptions import SandboxException
        checkout = checkout_of(Path(self.enterContext(tempfile.TemporaryDirectory())))
        recipe = self.recipes.App("owner/repo", checkout.parent / "recipe", {"workdir": str(checkout)})
        body = {"name": "runtime-restart", "repo": "owner/repo", "public_keys": ""}
        with self.creating(recipe, FakeVM()) as create:
            create.side_effect = SandboxException("500: failed to create sandbox: orchestrator unavailable")
            response = self.client.post("/api/sandboxes?start_app=false", json=body)
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.headers["retry-after"], "30")
            self.assertIn("try again", response.json()["detail"])
            create.side_effect = None
            response = self.client.post("/api/sandboxes?start_app=false", json=body)
        self.assertEqual(response.status_code, 200, response.text)

    def test_a_failure_after_the_vm_exists_is_no_retry(self):
        from e2b.exceptions import SandboxException
        recipe = self.recipes.App("owner/repo", Path("/unused"), {})
        vm = FakeVM("kept-vm")
        with self.creating(recipe, vm), \
                patch.object(self.core, "ensure_started", side_effect=SandboxException("envd did not answer")):
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
