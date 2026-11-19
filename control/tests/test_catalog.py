"""Catalog packages through their boundaries: package folders on disk (the
operator's catalog), real scripts that log what they were run with, the
connections and attachments tables, and the HTTP API."""
import base64
import contextlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from catalog_scripts import RedirectingServer
from support import Server

SCRIPT = """#!/bin/sh
set -eu
echo "$SBX_ACTION $1 $SBX_ID $SBX_SERVICE ${PG_ADMIN_URL:-} ${SBX_RESOURCE_STATUS} $SBX_PREVIOUS_OUTPUT" >> "$INPUT_LOG"
case "$SBX_ACTION" in
  create)
    [ ! -e "$INPUT_LOG.fail-create" ] || { rm "$INPUT_LOG.fail-create"; printf '_job_id=fixture-private-job\\n' >> "$SBX_OUTPUT"; echo 'job still running' >&2; exit 1; }
    printf 'url=fixture-private-%s\\ndatabase=%s\\n_role_id=fixture-private-role\\n' "$SBX_ID" "$SBX_ID" >> "$SBX_OUTPUT" ;;
  delete) [ ! -e "$INPUT_LOG.fail-delete" ] || { echo 'fixture cleanup failed' >&2; exit 1; } ;;
  list) [ ! -e "$INPUT_LOG.fail-list" ] || exit 1; echo "sbx-$SBX_ID" ;;
esac
"""


class CatalogTests(Server):
    SETTINGS = {"repos": ["owner/repo", "owner/app"]}

    def setUp(self):
        super().setUp()
        from sbx_control import catalog, paths
        from sbx_control.catalog import attachments, connections, resources
        self.catalog, self.attachments, self.connections, self.resources = catalog, attachments, connections, resources
        self.log = self.root / "scripts.log"
        for left in self.root.glob("scripts.log*"):
            left.unlink()
        for name in os.listdir(paths.packages()) if paths.packages().is_dir() else ():
            shutil.rmtree(paths.packages() / name)
        self.package("fixture", "requires: {server: {kind: fixture-server, expose: {url: PG_ADMIN_URL}}}\n")
        self.package("fixture-server", None, kind="connection")

    def package(self, name, extra, kind="provision", script=SCRIPT, timeout="1m"):
        from sbx_control import paths
        folder = paths.packages() / name
        folder.mkdir(parents=True, exist_ok=True)
        if kind == "provision":
            text = (f"kind: provision\ninputs: {{log: {{default: '{self.log}'}}}}\n"
                    "outputs: {url: {sensitive: true}, database: {}}\n"
                    f"runs: {{create: run.sh, delete: run.sh, list: run.sh}}\ntimeout: {timeout}\n" + (extra or ""))
            (folder / "run.sh").write_text(script)
            (folder / "run.sh").chmod(0o755)
        else:
            text = "kind: connection\ninputs: {url: {required: true, secret: true}}\n" + (extra or "")
        (folder / "package.yaml").write_text(text)

    def connect(self, key="dev-pg", url="postgresql://admin:fixture@original/db"):
        return self.connections.save(key, "fixture-server", {"url": url}, by="test")

    def runs(self):
        return [line.split(" ") for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def create(self, name="workspace", key="db", position=0, chosen=None, uses="fixture"):
        return self.resources.create(name, "owner/repo", key, uses, {}, position, chosen)

    def test_resource_ids_are_per_attachment_and_stable_on_retry(self):
        self.connect()
        first, second, again = self.create(), self.create(key="analytics", position=1), self.create()
        self.assertNotEqual(first.outputs["database"], second.outputs["database"])
        self.assertEqual(first.outputs, again.outputs)
        self.resources.delete_one("workspace", "db")
        action, argument, resource_id, key, url, *_ = self.runs()[-1]
        self.assertEqual((action, argument, resource_id, key, url),
                         ("delete", "delete", first.outputs["database"], "db", "postgresql://admin:fixture@original/db"))

    def test_a_failed_create_keeps_its_job_checkpoint_sealed_and_retries_the_same_id(self):
        self.connect()
        Path(f"{self.log}.fail-create").touch()
        with self.assertRaisesRegex(self.catalog.PackageError, "job still running"):
            self.create()
        from sbx_control import secret_store, state
        with state.db() as conn:
            public, sealed, status = conn.execute("select outputs, sensitive, status from sandbox_services").fetchone()
        self.assertNotIn("fixture-private-job", public + sealed)
        self.assertEqual(secret_store.unseal(sealed)["_job_id"], "fixture-private-job")
        self.assertEqual(status, "creating")
        self.create()
        first, retry = self.runs()
        self.assertEqual(first[2], retry[2])
        self.assertEqual(retry[5], "creating")
        self.assertEqual(json.loads(" ".join(retry[6:]))["_job_id"], "fixture-private-job")

    def test_saved_outputs_are_private_and_given_back_only_to_their_own_resource(self):
        self.connect()
        self.create()
        self.create(key="other", position=1)
        self.create()
        self.resources.delete_one("workspace", "db")
        runs = self.runs()
        self.assertEqual(json.loads(" ".join(runs[0][6:])), {})
        self.assertEqual(json.loads(" ".join(runs[1][6:])), {})
        for run in runs[2:]:
            self.assertEqual(run[5], "ready")
            self.assertEqual(json.loads(" ".join(run[6:]))["_role_id"], "fixture-private-role")
        from sbx_control import state
        with state.db() as conn:
            self.assertNotIn("fixture-private", conn.execute("select outputs from sandbox_services where key = 'other'").fetchone()[0])

    def test_an_existing_resources_binding_cannot_change_before_it_is_deleted(self):
        self.connect("original")
        self.connect("replacement")
        self.create(chosen={"server": "original"})
        with self.assertRaisesRegex(self.catalog.PackageError, "different package or connection"):
            self.create(chosen={"server": "replacement"})
        self.resources.delete_one("workspace", "db")
        self.assertEqual(self.runs()[-1][4], "postgresql://admin:fixture@original/db")

    def test_the_only_connection_of_a_kind_is_bound_and_recorded_for_delete(self):
        from sbx_control import state
        self.connect("implicit-pg")
        self.create()
        with state.db() as conn:
            self.assertEqual(json.loads(conn.execute("select connections from sandbox_services").fetchone()[0]),
                             {"server": "implicit-pg"})
        self.connect("second-pg", "postgresql://admin:fixture@second/db")
        self.resources.delete_one("workspace", "db")
        self.assertEqual(self.runs()[-1][4], "postgresql://admin:fixture@original/db")

    def test_reconcile_deletes_only_the_recorded_resources_of_gone_sandboxes(self):
        from sbx_control import state
        self.connect()
        self.create("gone")
        self.create("alive")
        with state.db() as conn:
            conn.execute("update sandbox_services set updated_at = datetime('now', '-2 hours')")
        self.resources.reconcile({"alive"})
        self.assertEqual([(r[0], r[2].startswith("gone")) for r in self.runs()],
                         [("create", True), ("create", False), ("delete", True)])

    def test_a_failed_delete_keeps_ownership_so_the_connection_cannot_go(self):
        self.connect()
        self.create(chosen={"server": "dev-pg"})
        Path(f"{self.log}.fail-delete").touch()
        self.assertTrue(self.resources.delete_all("workspace"))
        with self.assertRaisesRegex(self.catalog.PackageError, "sandbox workspace:db"):
            self.connections.remove("dev-pg")

    def test_a_connections_endpoint_is_fixed_while_resources_use_it_but_credentials_rotate(self):
        self.connect()
        self.create()
        with self.assertRaisesRegex(self.catalog.PackageError, "owns resources"):
            self.connect(url="postgresql://admin:fixture@different/db")
        self.connect(url="postgresql://admin:rotated@original/db")

    def test_a_retry_uses_the_package_version_that_made_the_resource(self):
        from sbx_control import paths
        self.connect()
        self.create()
        spec = paths.packages() / "fixture/package.yaml"
        spec.write_text(spec.read_text().replace("inputs: {log:", "inputs: {new_required: {required: true}, log:"))
        with self.assertRaisesRegex(self.catalog.PackageError, "needs new_required"):
            self.create(key="new")
        self.create()
        self.assertEqual(len(self.runs()), 2)

    def test_a_scripts_timeout_keeps_what_it_wrote_so_far(self):
        from sbx_control import secret_store, state
        self.package("slow", None, script='#!/bin/sh\nprintf "_job_id=fixture-private-job\\n" >> "$SBX_OUTPUT"\nsleep 3\n',
                     timeout="1s")
        with self.assertRaisesRegex(self.catalog.PackageError, "timed out after 1s"):
            self.create(uses="slow")
        with state.db() as conn:
            sealed = conn.execute("select sensitive from sandbox_services").fetchone()[0]
        self.assertEqual(secret_store.unseal(sealed)["_job_id"], "fixture-private-job")

    def test_a_package_trial_cleans_up_after_its_list_fails_and_shows_no_secret(self):
        from sbx_control import state
        self.repo_admin()
        self.connect()
        Path(f"{self.log}.fail-list").touch()
        result = self.client.post("/api/catalog/fixture/test", json={"inputs": {}})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()["ok"])
        self.assertEqual(result.json()["steps"][-1], {"step": "delete", "ok": True, "detail": "owned resources deleted"})
        self.assertNotIn("fixture-private", result.text)
        self.assertEqual([r[0] for r in self.runs()], ["create", "create", "list", "delete"])
        with state.db() as conn:
            self.assertEqual(conn.execute("select count(*) from sandbox_services").fetchone()[0], 0)

    def test_a_package_trial_finds_its_resource_by_the_naming_contract(self):
        self.repo_admin()
        self.connect()
        result = self.client.post("/api/catalog/fixture/test", json={"inputs": {}}).json()
        self.assertTrue(result["ok"], result)
        self.assertEqual([s["step"] for s in result["steps"]], ["create", "retry", "list", "delete"])

    def test_every_shipped_package_parses_and_binds_to_a_shipped_connection(self):
        shipped = {p.name: p for p in self.catalog.shipped()}
        self.assertIn("postgres-clone", shipped)
        self.assertEqual(shipped["postgres-clone"].files["logo.svg"], shipped["postgres-server"].files["logo.svg"])
        self.assertEqual(shipped["static"].logo, "")
        for package in shipped.values():
            with self.subTest(package=package.name):
                for requirement in package.requires.values():
                    connection = shipped[requirement["kind"]]
                    self.assertEqual(connection.kind, "connection")
                    self.assertTrue(set(requirement["expose"]) <= set(connection.inputs))
                if package.kind == "provision":
                    for script in {*package.runs.values(), *package.hooks.values()}:
                        self.assertTrue(package.files[script].startswith("#!"), script)

    def test_an_operators_broken_package_is_reported_not_served(self):
        from sbx_control import paths
        (paths.packages() / "broken").mkdir()
        (paths.packages() / "broken/package.yaml").write_text("kind: pointer\n")
        self.assertNotIn("broken", {p.name for p in self.catalog.listing()})
        self.assertTrue(any("broken" in problem for problem in self.catalog.problems()))
        self.repo_admin()
        self.assertTrue(self.client.get("/api/catalog").json()["problems"])

    def test_macos_metadata_files_are_not_package_files(self):
        from sbx_control import paths
        (paths.packages() / "fixture/._run.sh").write_bytes(b"\x00\x05\x16\x07")
        (paths.packages() / "fixture/.DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
        files = self.catalog.get("fixture").files
        self.assertIn("run.sh", files)
        self.assertFalse({"._run.sh", ".DS_Store"} & set(files))

    def test_a_packages_logo_reaches_its_listing_attachments_and_connections_as_an_image(self):
        from sbx_control import paths
        svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><path d="M0 0h1v1z"/></svg>\n'
        for name in ("fixture", "fixture-server"):
            (paths.packages() / name / "logo.svg").write_text(svg)
            with (paths.packages() / name / "package.yaml").open("a") as spec:
                spec.write("logo: logo.svg\n")
        self.repo_admin()
        self.recipe("owner/app", "services:\n  db:\n    uses: fixture\n")
        self.connect()
        listed = {p["name"]: p for p in self.client.get("/api/catalog").json()["packages"]}
        prefix = "data:image/svg+xml;base64,"
        self.assertTrue(listed["fixture"]["logo"].startswith(prefix))
        self.assertEqual(base64.b64decode(listed["fixture"]["logo"].removeprefix(prefix)).decode(), svg)
        entry = self.client.get("/api/catalog/fixture").json()
        self.assertEqual(entry["logo"], listed["fixture"]["logo"])
        self.assertNotIn("logo.svg", entry["scripts"])
        attachment = self.client.get("/api/repos/owner/app/attachments").json()["attachments"][0]
        self.assertEqual(attachment["logo"], listed["fixture"]["logo"])
        connection = self.client.get("/api/catalog/connections").json()["connections"][0]
        self.assertEqual(connection["logo"], listed["fixture-server"]["logo"])
        self.assertEqual(self.catalog.parse("plain", {"package.yaml": "kind: connection\n"}, "repo").as_dict()["logo"], "")

    def test_a_logo_is_a_small_svg_in_the_packages_folder(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg"/>'
        refused = {
            "a missing file": {},
            "a file that is not .svg": {"logo.png": svg},
            "text that is not SVG": {"logo.svg": "<html></html>"},
            "HTML that mentions svg": {"logo.svg": "<html><!-- <svg --></html>"},
            "an svg root outside the SVG namespace": {"logo.svg": "<svg/>"},
            "a document that does not parse": {"logo.svg": '<svg xmlns="http://www.w3.org/2000/svg">'},
            "an entity declaration": {"logo.svg": '<!DOCTYPE svg [<!ENTITY a "a">]><svg xmlns="http://www.w3.org/2000/svg"/>'},
            "an SVG over the limit": {"logo.svg": svg + " " * self.catalog.LOGO_BYTES},
        }
        for why, files in refused.items():
            with self.subTest(why), self.assertRaisesRegex(self.catalog.PackageError, "logo|SVG"):
                logo = next(iter(files), "logo.svg")
                self.catalog.parse("brand", {"package.yaml": f"kind: connection\nlogo: {logo}\n", **files}, "repo")
        package = self.catalog.parse("brand", {"package.yaml": "kind: connection\nlogo: logo.svg\n", "logo.svg": svg}, "repo")
        self.assertEqual(package.logo, "logo.svg")

    def test_a_blank_input_goes_back_to_its_default_and_a_blank_secret_is_kept(self):
        self.repo_admin()
        self.recipe("owner/app", "services: {}\n")
        self.connect()
        self.package("tokened", f"inputs: {{log: {{default: '{self.log}'}}, mode: {{default: plain, choices: [plain, fancy]}}, "
                                "token: {secret: true}}\nrequires: {server: {kind: fixture-server, expose: {url: PG_ADMIN_URL}}}\n")
        post = lambda inputs: self.client.post("/api/repos/owner/app/attachments", json={"package": "tokened", "key": "t", "inputs": inputs})
        self.assertEqual(post({"mode": "fancy", "token": "fixture-token"}).status_code, 200)
        self.assertEqual(post({"mode": "", "token": ""}).status_code, 200)
        [(_, _, given, _)] = [a for a in self.attachments.for_create("owner/app") if a[0] == "t"]
        self.assertNotIn("mode", given)
        self.assertEqual(given["token"], "fixture-token")
        self.assertEqual(self.catalog.get("tokened").with_defaults(given, "t")["mode"], "plain")

    def test_clickhouse_check_sends_a_password_over_plain_http_only_on_a_private_network(self):
        server = self.catalog.get("clickhouse-server")
        refused = self.catalog.check(server, {"url": "http://8.8.8.8:8123", "user": "default", "password": "p"})
        self.assertIn("must use https://", refused)
        # Loopback passes the address check and reaches the SELECT 1 (nothing listens there).
        private = self.catalog.check(server, {"url": "http://127.0.0.1:9", "user": "default", "password": "p"})
        self.assertIn("did not answer SELECT 1", private)
        with RedirectingServer() as redirecting:
            redirected = self.catalog.check(server, {"url": redirecting.url, "user": "default", "password": "p"})
        self.assertIn("did not answer SELECT 1", redirected)
        self.assertEqual(redirecting.paths, ["/"])

    def test_clearing_an_input_the_recipe_sets_checks_the_recipes_value(self):
        self.repo_admin()
        self.recipe("owner/app", "services:\n  t:\n    uses: tokened\n    with: {mode: fancy}\n")
        self.connect()
        self.package("tokened", f"inputs: {{log: {{default: '{self.log}'}}, mode: {{default: plain, choices: [plain, fancy]}}}}\n"
                                "requires: {server: {kind: fixture-server, expose: {url: PG_ADMIN_URL}}}\n")
        post = lambda inputs: self.client.post("/api/repos/owner/app/attachments", json={"package": "tokened", "key": "t", "inputs": inputs})
        self.assertEqual(post({"mode": "plain"}).status_code, 200)
        with patch("sbx_control.catalog.attachments.check", return_value="") as checked:
            self.assertEqual(post({"mode": ""}).status_code, 200)
        self.assertEqual(checked.call_args[0][1]["mode"], "fancy")
        [(_, _, given, _)] = [a for a in self.attachments.for_create("owner/app") if a[0] == "t"]
        self.assertEqual(given["mode"], "fancy")

    def test_an_input_can_name_itself_for_people_and_offer_choices(self):
        spec = ("kind: connection\ninputs:\n"
                "  limit: {label: Credit limit (USD), required: true}\n"
                "  reset:\n    default: ''\n    choices: {'': Never, daily: Daily, weekly: Weekly}\n"
                "  size: {default: small, choices: [small, large]}\n")
        package = self.catalog.parse("budget", {"package.yaml": spec}, "repo")
        inputs = package.as_dict()["inputs"]
        self.assertEqual(inputs["limit"]["label"], "Credit limit (USD)")
        self.assertNotIn("choices", inputs["limit"])
        self.assertEqual(inputs["reset"]["choices"], [{"value": "", "label": "Never"}, {"value": "daily", "label": "Daily"},
                                                      {"value": "weekly", "label": "Weekly"}])
        self.assertEqual([c["label"] for c in inputs["size"]["choices"]], ["small", "large"])
        self.assertEqual(package.with_defaults({"limit": "5", "reset": "daily"}, "x")["reset"], "daily")
        self.assertEqual(package.with_defaults({"limit": "5"}, "x")["reset"], "")
        with self.assertRaisesRegex(self.catalog.PackageError, r"x: reset is one of \(blank\), daily, weekly, not 'hourly'"):
            package.with_defaults({"limit": "5", "reset": "hourly"}, "x")
        refused = {
            "a default that is not a choice": "  a: {default: tiny, choices: [small, large]}\n",
            "no choices": "  a: {choices: []}\n",
            "choices that are text": "  a: {choices: small}\n",
            "the same value twice": "  a: {choices: [small, small]}\n",
            "a blank value without a label": "  a: {choices: ['', small]}\n",
        }
        for why, line in refused.items():
            with self.subTest(why), self.assertRaisesRegex(self.catalog.PackageError, "input a"):
                self.catalog.parse("budget", {"package.yaml": "kind: connection\ninputs:\n" + line}, "repo")

    def test_an_input_says_what_it_holds_and_values_are_checked_against_it(self):
        spec = ("kind: connection\ninputs:\n"
                "  budget: {type: integer, min: 20, default: '20'}\n"
                "  limit: {type: number, min: 0}\n"
                "  data: {default: 'false'}\n"
                "  email: {type: email}\n"
                "  url: {type: url, advanced: true}\n"
                "  name: {}\n")
        package = self.catalog.parse("typed", {"package.yaml": spec}, "repo")
        inputs = package.as_dict()["inputs"]
        self.assertEqual({k: v["type"] for k, v in inputs.items()},
                         {"budget": "integer", "limit": "number", "data": "boolean", "email": "email", "url": "url", "name": "text"})
        self.assertEqual((inputs["budget"]["min"], inputs["url"]["advanced"]), (20, True))
        self.assertNotIn("advanced", inputs["name"])
        self.assertEqual(package.with_defaults({"limit": "2.5", "email": "ops@example.com", "url": "https://x.example"}, "x")["budget"], "20")
        refused = {"budget": "19", "limit": "lots", "data": "yes", "email": "ops", "url": "x.example"}
        for name, value in refused.items():
            with self.subTest(name), self.assertRaisesRegex(self.catalog.PackageError, f"x: {name} is"):
                package.with_defaults({name: value}, "x")
        hidden = self.catalog.parse("sealed", {"package.yaml": "kind: connection\ninputs:\n  token: {type: url, secret: true}\n"}, "repo")
        with self.assertRaises(self.catalog.PackageError) as refusal:
            hidden.with_defaults({"token": "tok-private-value"}, "x")
        self.assertNotIn("tok-private-value", str(refusal.exception))
        bad = {
            "an unknown type": "  a: {type: color}\n",
            "a type with choices": "  a: {type: integer, choices: ['1', '2']}\n",
            "a bound on text": "  a: {min: 1}\n",
            "a default outside the bounds": "  a: {type: integer, min: 5, default: '1'}\n",
        }
        for why, line in bad.items():
            with self.subTest(why), self.assertRaisesRegex(self.catalog.PackageError, "input a"):
                self.catalog.parse("typed", {"package.yaml": "kind: connection\ninputs:\n" + line}, "repo")

    def test_the_shipped_packages_parse_with_their_input_types(self):
        root = Path(__file__).resolve().parents[2] / "catalog"
        for folder in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")):
            files = {str(f.relative_to(folder)): f.read_text() for f in folder.rglob("*") if f.is_file()}
            with self.subTest(folder.name):
                package = self.catalog.parse(folder.name, files, "shipped")
                for spec in package.inputs.values():
                    self.assertIn(spec["type"], self.catalog.INPUT_TYPES)

    def recipe(self, repo, text):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (directory / repo / ".sbx").mkdir(parents=True)
        (directory / repo / ".sbx/sbx.yaml").write_text(text)
        self.enterContext(patch("sbx_control.paths.apps", return_value=directory))

    def test_attachments_merge_the_recipe_with_records_and_never_show_secrets(self):
        self.repo_admin()
        self.recipe("owner/app", "services:\n  db:\n    uses: fixture\n    with: {log: /tmp/declared}\n")
        self.connect()
        response = self.client.post("/api/repos/owner/app/attachments", json={
            "package": "fixture", "key": "analytics", "inputs": {"log": "/tmp/recorded"}})
        self.assertEqual(response.status_code, 200, response.text)
        found = {a["key"]: a for a in self.client.get("/api/repos/owner/app/attachments").json()["attachments"]}
        self.assertEqual((found["db"]["origin"], found["analytics"]["origin"]), ("sbx.yaml", "here"))
        self.assertEqual(found["db"]["connections"], {"server": "dev-pg"})
        self.assertEqual(found["analytics"]["state"], "ready")
        self.assertEqual([(k, w["log"]) for k, _, w, _ in self.attachments.for_create("owner/app")],
                         [("db", "/tmp/declared"), ("analytics", "/tmp/recorded")])
        self.assertEqual(self.client.delete("/api/repos/owner/app/attachments/db").json(), {"key": "db", "disabled": True})
        self.assertEqual(self.client.delete("/api/repos/owner/app/attachments/analytics").json(),
                         {"key": "analytics", "removed": True})
        self.assertEqual(self.attachments.for_create("owner/app"), [])

    def test_a_yaml_boolean_in_the_recipe_reaches_scripts_as_true_or_false(self):
        self.recipe("owner/app", "services:\n  db:\n    uses: fixture\n    with: {log: /tmp/x, flag: true, quiet: false}\n")
        withs = self.attachments.merged("owner/app")[0][2]
        self.assertEqual((withs["flag"], withs["quiet"]), ("true", "false"))

    def test_an_attachment_without_its_connection_says_what_it_needs(self):
        self.recipe("owner/app", "services:\n  db:\n    uses: fixture\n")
        found = self.attachments.effective("owner/app")[0]
        self.assertEqual(found["state"], "needs_connection")
        self.assertIn("fixture-server", found["problem"])

    def test_connections_api_lists_values_only_where_the_package_says_they_are_not_secret(self):
        self.repo_admin()
        response = self.client.post("/api/catalog/connections", json={
            "package": "fixture-server", "key": "dev-pg", "inputs": {"url": "postgresql://admin:fixture-private@h/db"}})
        self.assertEqual(response.status_code, 200, response.text)
        listed = self.client.get("/api/catalog/connections").json()["connections"]
        self.assertEqual(listed[0]["inputs"], {"url": "(set)"})
        self.assertNotIn("fixture-private", json.dumps(listed))
        self.assertEqual(self.client.post("/api/catalog/connections/dev-pg/check").json()["ok"], True)
        self.assertEqual(self.client.get("/api/catalog/fixture-server").json()["connections"][0]["key"], "dev-pg")
        self.assertTrue(self.client.delete("/api/catalog/connections/dev-pg").json()["removed"])

    def test_setup_instructions_name_saved_connections_without_their_credentials(self):
        from sbx_control import repos
        self.repo_admin()
        self.connect("existing-pg", "postgresql://admin:fixture-private-api-key@h/db")
        brief = repos.setup_brief("owner/app", "operator@example.com")
        self.assertIn("existing-pg", brief)
        self.assertIn("fixture-server", brief)
        self.assertNotIn("fixture-private-api-key", brief)
        # The canonical guides follow the steps, read from the docs themselves.
        for heading in ("# Service selection", "# Resource guide"):
            self.assertIn(f"\n{heading}\n", brief)
        # One sandbox and its preview link for the owner: the acceptance checklist is for changes to sbx itself.
        self.assertNotIn("# Acceptance checklist", brief)
        self.assertNotIn("check-b", brief)
        self.assertIn("give the owner its\npreview link", brief)

    def test_a_release_ships_the_docs_the_setup_brief_carries(self):
        import subprocess
        import sys
        root = Path(__file__).resolve().parents[2]
        if not (root / "control/sbx_control/web_dist/index.html").is_file():
            self.skipTest("release.sh needs the web app built: npm --prefix web run build")
        out = Path(self.enterContext(tempfile.TemporaryDirectory())) / "release"
        subprocess.run(["sh", str(root / "control/release.sh"), str(out), "test"], check=True, capture_output=True)
        shipped = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                                  "from sbx_control import paths; print(paths.shipped('docs'))", str(out)],
                                 check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(Path(shipped), (out / "docs").resolve())
        self.assertTrue((out / "docs/service-selection.md").is_file())
        self.assertIn("## Acceptance checklist", (out / "docs/agent-install.md").read_text())
        shipped_examples = {p.relative_to(out / "examples").as_posix() for p in (out / "examples").rglob("*")
                            if p.is_file()}
        expected = {p.relative_to(root / "examples").as_posix() for p in (root / "examples").glob("*/README.md")}
        expected |= {"README.md"} if (root / "examples/README.md").is_file() else set()
        self.assertEqual(shipped_examples, expected)

    def test_the_setup_brief_carries_the_examples_how_their_recipes_were_made(self):
        from sbx_control import paths, repos
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (folder / "examples/alpha").mkdir(parents=True)
        (folder / "examples/plain").mkdir()
        (folder / "examples/README.md").write_text("# Examples\n\n- alpha: a database per sandbox\n")
        (folder / "examples/alpha/README.md").write_text(
            "# Alpha\n\n## Run it\n\nsbx up\n\n## How this recipe was made\n\nIt chose postgres-clone "
            "([recipe](.sbx/), [services](../../docs/reference.md#services)).\n\n## Later\n\nnot this\n")
        (folder / "examples/plain/README.md").write_text("# Plain\n\n## Run it\n\nsbx up\n")
        real = paths.shipped
        with patch.object(paths, "shipped", lambda name: folder / name if name == "examples" else real(name)):
            guides = repos.guides()
        self.assertEqual([g.splitlines()[0] for g in guides],
                         ["# Examples", f"# How this recipe was made: Alpha ({repos.SOURCE}/examples/alpha)",
                          "# Service selection", "# Resource guide"])
        # The agent reading the brief has no sbx source: relative links go to GitHub, anchors and URLs stay.
        self.assertEqual(guides[1].split("\n\n", 1)[1],
                         f"It chose postgres-clone ([recipe]({repos.SOURCE}/examples/alpha/.sbx), "
                         f"[services]({repos.SOURCE}/docs/reference.md#services)).")
        self.assertEqual(repos.linked("[a](#x) [b](https://e.test/y) [c](reference.md)", "docs"),
                         f"[a](#x) [b](https://e.test/y) [c]({repos.SOURCE}/docs/reference.md)")
        self.assertNotRegex("\n".join(guides), r"\]\((?!https://|#)")


class ManagedServerTests(Server):
    """Managed servers through their boundaries: docker and the storage tools."""

    def setUp(self):
        super().setUp()
        from sbx_control import cow_storage, hosted_services, secret_store, state
        self.hosted, self.cow, self.secret_store, self.state = hosted_services, cow_storage, secret_store, state

    def record(self, name, kind, values):
        with self.state.db() as conn:
            conn.execute("insert into managed_servers (name, kind, config) values (?, ?, ?)",
                         (name, kind, self.secret_store.seal(values, key=f"managed-server/{name}")))

    def test_postgres_from_before_its_cow_storage_is_never_reused(self):
        self.record("old-pg", "postgres", {"address": "172.29.50.10", "password": "fixture"})
        with patch.object(self.hosted, "docker") as docker:
            with self.assertRaisesRegex(ValueError, "predates PostgreSQL 18"):
                self.hosted.up("old-pg", "postgres")
            docker.assert_not_called()

    def test_an_unrecorded_container_or_volume_is_not_adopted(self):
        network = {"Labels": {self.hosted.LABEL: "true"}, "IPAM": {"Config": [{"Subnet": self.hosted.CIDR}]}, "Containers": {}}
        for existing in ("container", "volume"):
            with self.subTest(existing=existing):
                def docker(*args, **kwargs):
                    if args[:2] == ("network", "inspect"):
                        return Mock(returncode=0, stdout=json.dumps([network]))
                    return Mock(returncode=0 if (existing == "container") == (args[0] == "inspect") else 1)
                with patch.object(self.hosted, "docker", side_effect=docker):
                    with self.assertRaisesRegex(ValueError, "without a managed record"):
                        self.hosted.up("unrecorded-audit", "clickhouse")
                self.assertEqual(self.hosted.records(), [])

    def test_a_name_another_connection_has_is_refused(self):
        from sbx_control.catalog import connections
        connections.save("taken", "static", {"url": "x"}, by="test")
        network = {"Labels": {self.hosted.LABEL: "true"}, "IPAM": {"Config": [{"Subnet": self.hosted.CIDR}]}, "Containers": {}}
        with patch.object(self.hosted, "docker", return_value=Mock(returncode=0, stdout=json.dumps([network]))):
            with self.assertRaisesRegex(ValueError, "not a managed server"):
                self.hosted.up("taken", "clickhouse")

    def bring_up(self, server, details):
        """_up for a recorded server whose container docker inspect describes
        (None: it has none); returns the docker calls it made."""
        network = {"Labels": {self.hosted.LABEL: "true"}, "IPAM": {"Config": [{"Subnet": self.hosted.CIDR}]}, "Containers": {}}
        calls = []

        def docker(*args, **kwargs):
            calls.append(args)
            if args[:2] == ("network", "inspect"):
                return Mock(returncode=0, stdout=json.dumps([network]))
            if args[:2] == ("volume", "inspect"):
                return Mock(returncode=0, stdout=json.dumps({self.hosted.LABEL: server.name}))
            if args[0] == "inspect":
                return Mock(returncode=0 if details else 1, stdout=json.dumps([details]))
            return Mock(returncode=0, stdout="")
        with patch.object(self.hosted, "docker", side_effect=docker), patch.object(self.hosted.connections, "save"):
            self.hosted._finish(server, self.hosted._record(server.name)[1], self.hosted._details(server))
        return calls

    def test_a_new_server_keeps_a_bounded_log(self):
        # Docker keeps a container's log forever otherwise, and a server failing on a full disk fills it again.
        server = self.hosted.Server("ch-new", "clickhouse")
        self.record("ch-new", "clickhouse", {"address": "172.29.50.11", "password": "fixture"})
        run = next(c for c in self.bring_up(server, None) if c[0] == "run")
        self.assertIn(("--log-opt", "max-size=50m", "--log-opt", "max-file=5"),
                      [run[i:i + 4] for i in range(len(run))])
        self.assertLess(run.index("--log-opt"), run.index(server.image))   # an option of the container, not its command

    def test_a_server_made_without_a_log_limit_is_made_again_around_its_data(self):
        server = self.hosted.Server("ch-old", "clickhouse")
        self.record("ch-old", "clickhouse", {"address": "172.29.50.11", "password": "fixture"})
        labels = {"Config": {"Labels": {self.hosted.LABEL: "ch-old"}}}
        unlimited = {**labels, "HostConfig": {"LogConfig": {"Type": "json-file", "Config": {}}}}
        calls = self.bring_up(server, unlimited)
        kinds = [c[0] for c in calls]
        self.assertLess(calls.index(("rm", "-f", server.container)), kinds.index("run"))
        self.assertNotIn(("volume", "rm", server.volume), calls)
        limited = {**labels, "HostConfig": {"LogConfig": {"Type": "json-file", "Config": {"max-size": "50m"}}}}
        calls = self.bring_up(server, limited)
        self.assertNotIn("rm", [c[0] for c in calls])
        self.assertNotIn("run", [c[0] for c in calls])
        self.assertIn(("start", server.container), calls)

    def test_a_postgres_made_without_a_log_limit_is_stopped_by_its_unit_before_it_is_made_again(self):
        server = self.hosted.Server("pg-old", "postgres")
        with tempfile.TemporaryDirectory() as directory:
            storage = {"path": f"{directory}/data"}
            values = {"address": "172.29.50.10", "password": "fixture", "postgres_major": self.hosted.POSTGRES_MAJOR,
                      "storage": storage}
            self.record("pg-old", "postgres", values)
            (Path(directory) / server.unit).touch()
            details = {"Config": {"Labels": {self.hosted.LABEL: "pg-old"}, "Image": server.image},
                       "Mounts": [{"Source": storage["path"], "Destination": server.data_path}],
                       "HostConfig": {"LogConfig": {"Type": "json-file", "Config": {}}}}
            order = []
            with patch.object(self.cow, "UNITS", Path(directory)), \
                    patch.object(self.cow, "prepare", return_value="fixture.mount"), \
                    patch.object(self.cow, "run", side_effect=lambda *a: order.append(a)), \
                    patch.object(self.cow, "start", side_effect=lambda *a: order.append(("start unit",))), \
                    patch.object(self.hosted, "_check", return_value=values):
                calls = self.bring_up(server, details)
        self.assertEqual(order, [("systemctl", "stop", server.unit), ("start unit",)])
        create = next(c for c in calls if c[0] == "create")
        self.assertLess(calls.index(("rm", "-f", server.container)), calls.index(create))
        self.assertIn("max-size=50m", create)

    LIMITS = "    mem_limit: 256m\n    cpus: 1\n"
    COMPOSE = ("services:\n  app:\n    image: example/app:1\n    volumes: [data:/data]\n" + LIMITS +
               "  worker:\n    image: example/app:1\n    restart: 'no'\n" + LIMITS + "volumes:\n  data:\n")
    SERVER = ("server:\n  compose: compose.yaml\n  service: app\n  port: 8080\n  storage_gb: 3\n"
              "  secrets: [APP_TOKEN]\n"
              "  inputs: {url: 'http://${address}:${port}', token: '${secrets.APP_TOKEN}'}\n")

    def server_package(self, name="fixture-app", compose=COMPOSE, server=SERVER):
        """A connection package that runs its server; returns its files."""
        from sbx_control import paths
        folder = paths.packages() / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "compose.yaml").write_text(compose)
        (folder / "package.yaml").write_text(
            "kind: connection\ninputs: {url: {required: true}, token: {secret: true}}\n" + server)
        return {"package.yaml": (folder / "package.yaml").read_text(), "compose.yaml": compose}

    @contextlib.contextmanager
    def docker_without_containers(self):
        """The host's boundaries with the managed network and nothing on it:
        docker, and the storage tools that make and remove a server's disk
        (self.prepared, self.removed)."""
        network = {"Labels": {self.hosted.LABEL: "true"}, "IPAM": {"Config": [{"Subnet": self.hosted.CIDR}]}, "Containers": {}}

        def docker(*args, **kwargs):
            if args[:2] == ("network", "inspect"):
                return Mock(returncode=0, stdout=json.dumps([network]))
            return Mock(returncode=1, stdout="")
        with patch.object(self.hosted, "docker", side_effect=docker), \
                patch.object(self.cow, "volume_prepare", side_effect=lambda name, storage, volumes: {
                    volume: f"{storage['path']}/{volume}" for volume in volumes}) as self.prepared, \
                patch.object(self.cow, "volume_remove") as self.removed:
            yield

    def test_a_package_runs_its_server_and_removing_it_leaves_nothing(self):
        from sbx_control.catalog import connections
        self.server_package()
        compose = self.hosted.hosted_compose
        with self.docker_without_containers(), patch.object(compose, "exists", return_value=False), \
                patch.object(compose, "up") as up, patch.object(compose, "down") as down:
            started = self.hosted.up("app", package="fixture-app")
            name, package, values = up.call_args.args[:3]
            self.assertEqual((name, package.name), ("app", "fixture-app"))
            # Its volumes are directories on a disk of its own, of the size its package says.
            self.assertEqual(values["storage"]["size_gb"], 3)
            self.assertEqual(up.call_args.args[5], {"data": values["storage"]["path"] + "/data"})
            self.assertEqual(self.prepared.call_args.args, ("app", values["storage"], ["data"]))
            self.assertRegex(values["secrets"]["APP_TOKEN"], "^[0-9a-f]{64}$")
            self.assertEqual(started["status"], "ready")
            self.assertEqual(self.hosted.target("app"), f"{values['address']}:8080")
            saved = connections.listing()[0]
            self.assertEqual((saved["key"], saved["package"]), ("app", "fixture-app"))
            self.assertEqual(saved["inputs"], {"url": f"http://{values['address']}:8080", "token": "(set)"})
            listed = self.hosted.records()
            self.assertEqual([(r["name"], r["kind"], r["package"], r["status"], r["port"]) for r in listed],
                             [("app", "package", "fixture-app", "ready", 8080)])
            self.assertNotIn(values["secrets"]["APP_TOKEN"], json.dumps(listed))
            with self.assertRaisesRegex(ValueError, "keeps the secrets"):
                self.hosted.up("app", package="fixture-app", presets={"APP_TOKEN": "other"})
            with self.assertRaisesRegex(ValueError, "this server runs fixture-app"):
                self.server_package("other-app")
                self.hosted.up("app", package="other-app")
            with self.assertRaisesRegex(ValueError, "disk cannot change size"):
                self.hosted.up("app", package="fixture-app", storage_gb=9)
            self.hosted.down("app")
            self.removed.assert_not_called()   # stopped: its disk stays
            self.hosted.up("app", package="fixture-app")
            self.assertEqual(self.hosted.down("app", delete_data=True)["data_deleted"], True)
            self.assertEqual(down.call_args.kwargs, {"delete_data": True})
            self.removed.assert_called_once_with(values["storage"])
        self.assertEqual(self.hosted.records(), [])
        self.assertEqual(connections.listing(), [])

    def test_a_package_server_takes_given_secrets_only_ones_it_declares(self):
        self.server_package()
        compose = self.hosted.hosted_compose
        with self.docker_without_containers(), patch.object(compose, "exists", return_value=False), \
                patch.object(compose, "up") as up:
            with self.assertRaisesRegex(ValueError, "no secret OTHER"):
                self.hosted.up("app", package="fixture-app", presets={"OTHER": "x"})
            self.hosted.up("app", package="fixture-app", presets={"APP_TOKEN": "kept-from-before"}, storage_gb=7)
            self.assertEqual(up.call_args.args[2]["secrets"], {"APP_TOKEN": "kept-from-before"})
            self.assertEqual(up.call_args.args[2]["storage"]["size_gb"], 7)

    def test_a_server_that_does_not_start_says_why_and_starts_again(self):
        self.server_package()
        compose = self.hosted.hosted_compose
        with self.docker_without_containers(), patch.object(compose, "exists", return_value=False), \
                patch.object(compose, "up", side_effect=RuntimeError("docker compose up failed (exit 1)")) as up:
            self.assertEqual(self.hosted.begin("app", package="fixture-app")["status"], "preparing")
            with self.assertRaises(RuntimeError):
                self.hosted.finish("app")
            failed = self.hosted.records()[0]
            self.assertEqual((failed["status"], failed["error"]), ("failed", "docker compose up failed (exit 1)"))
            up.side_effect = None
            # The same name and package again: it reads as starting before the API answers.
            self.assertEqual(self.hosted.begin("app", package="fixture-app")["status"], "preparing")
            self.hosted.finish("app")
            again = self.hosted.records()[0]
            self.assertEqual((again["status"], again["error"]), ("ready", ""))

    def test_a_failed_start_keeps_what_it_saved_and_its_check_waits_after_compose_up(self):
        self.server_package()
        compose = self.hosted.hosted_compose
        clock = [0, 1000]   # compose up took longer than the check's whole window

        def up(name, package, values, *rest):
            self.hosted._save(self.hosted.Server(name, "package"), {**values, "marker": "saved by the run"})
        with self.docker_without_containers(), patch.object(compose, "exists", return_value=False), \
                patch.object(compose, "up", side_effect=up), patch.object(self.hosted.time, "sleep"), \
                patch.object(self.hosted.time, "monotonic", side_effect=lambda: clock.pop(0) if len(clock) > 1 else clock[0]), \
                patch.object(self.hosted.connections, "save",
                             side_effect=[self.hosted.PackageError("not yet"), RuntimeError("refused")]):
            self.hosted.begin("app", package="fixture-app")
            with self.assertRaisesRegex(RuntimeError, "refused"):   # retried once, not given up at once
                self.hosted.finish("app")
        values = self.hosted._record("app")[1]
        self.assertEqual((values["marker"], values["error"]), ("saved by the run", "refused"))

    def test_a_server_removed_while_listed_is_left_out(self):
        self.record("gone", "clickhouse", {"address": "172.29.50.10", "password": "fixture"})
        real = self.hosted._record
        with patch.object(self.hosted, "_record", side_effect=lambda name: None if name == "gone" else real(name)):
            self.assertEqual(self.hosted.records(), [])
        self.assertTrue(self.hosted.exists("gone"))
        self.assertFalse(self.hosted.exists("other"))

    def test_a_name_with_containers_but_no_record_is_not_adopted_by_a_package(self):
        self.server_package()
        with self.docker_without_containers(), patch.object(self.hosted.hosted_compose, "exists", return_value=True):
            with self.assertRaisesRegex(ValueError, "without a managed record"):
                self.hosted.up("app", package="fixture-app")
        self.assertEqual(self.hosted.records(), [])

    def test_a_package_that_runs_no_server_and_one_of_the_two_built_in(self):
        from sbx_control import catalog
        self.server_package("plain", server="")
        with self.docker_without_containers():
            with self.assertRaisesRegex(ValueError, "somebody else runs"):
                self.hosted.up("app", package="plain")
            with self.assertRaisesRegex(ValueError, "kind .* or the connection package"):
                self.hosted.up("app")
        self.assertTrue(self.hosted.runs(catalog.get("postgres-server")))   # built in: no server: of its own
        self.assertFalse(self.hosted.runs(catalog.get("plain")))

    def test_what_a_package_server_may_not_ask_of_the_host(self):
        from sbx_control import catalog
        files = self.server_package()
        self.assertEqual(catalog.parse("fixture-app", files, "operator").server["port"], 8080)
        refused = {
            "sets mem_limit and cpus": self.COMPOSE.replace(self.LIMITS, "    mem_limit: 256m\n", 1),
            "sets ports": self.COMPOSE.replace("    volumes: [data:/data]\n", "    ports: ['8080:8080']\n"),
            "sets network_mode, privileged": self.COMPOSE.replace("    restart: 'no'\n", "    privileged: true\n    network_mode: host\n"),
            "not one of its volumes": self.COMPOSE.replace("[data:/data]", "['/etc:/host-etc']"),
            "services and volumes only": self.COMPOSE + "networks:\n  other: {}\n",
        }
        for why, compose in refused.items():
            with self.subTest(why=why), self.assertRaisesRegex(catalog.PackageError, why):
                catalog.parse("fixture-app", {**files, "compose.yaml": compose}, "operator")
        with self.assertRaisesRegex(catalog.PackageError, "only a connection of the installation's catalog"):
            catalog.parse("fixture-app", files, "repo", "owner/app")
        with self.assertRaisesRegex(catalog.PackageError, "names secrets.OTHER"):
            catalog.parse("fixture-app", {**files, "package.yaml": files["package.yaml"].replace("secrets.APP_TOKEN", "secrets.OTHER")}, "operator")
        with self.assertRaisesRegex(catalog.PackageError, "server.storage_gb is the size"):
            catalog.parse("fixture-app", {**files, "package.yaml": files["package.yaml"].replace("  storage_gb: 3\n", "")}, "operator")
        with self.assertRaisesRegex(catalog.PackageError, "server.service is one of"):
            catalog.parse("fixture-app", {**files, "package.yaml": files["package.yaml"].replace("service: app", "service: web")}, "operator")
        with self.assertRaisesRegex(catalog.PackageError, "server.service is one of"):
            catalog.parse("fixture-app", {**files, "package.yaml": files["package.yaml"].replace("service: app", "service: [app]")}, "operator")

    def test_the_shipped_packages_that_run_a_server_declare_all_their_compose_files_read(self):
        import re
        from sbx_control import catalog
        runs = {p.name: p for p in catalog.shipped() if p.server}
        self.assertEqual(sorted(runs), ["langfuse-server", "temporal-server"])
        self.assertEqual((runs["temporal-server"].server["service"], runs["temporal-server"].server["port"]),
                         ("temporal", 7233))
        for name, package in runs.items():
            # A variable Compose finds unset is an empty value: a password of nothing, or a refused start.
            read = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", package.files[package.server["compose"]]))
            self.assertEqual(read - set(package.server["secrets"]) - {"SBX_ADDRESS", "SBX_NAME"}, set(), name)
            self.assertEqual(set(package.server["secrets"]) - read, set(), name)   # and none made for nothing
        inputs = catalog.render_server_inputs(runs["langfuse-server"], "172.29.50.20",
                                              {key: "0" * 64 for key in runs["langfuse-server"].server["secrets"]})
        self.assertEqual((inputs["url"], inputs["public_key"]), ("http://172.29.50.20:3000", "pk-lf-" + "0" * 64))
        tracing = next(p for p in catalog.shipped() if p.name == "langfuse-tracing")
        self.assertEqual(tracing.requires["server"]["kind"], "langfuse-server")

    def test_this_host_adds_the_address_the_log_limit_and_its_label_to_a_package_server(self):
        import yaml
        from sbx_control import catalog
        package = catalog.parse("fixture-app", self.server_package(), "operator")
        added = self.hosted.hosted_compose.override("app", package.server, yaml.safe_load(self.COMPOSE), "172.29.50.20",
                                                    self.hosted.NETWORK, self.hosted.LABEL, {"data": "/srv/app/data"})
        app, worker = added["services"]["app"], added["services"]["worker"]
        self.assertEqual(app["networks"], {"default": {}, self.hosted.NETWORK: {"ipv4_address": "172.29.50.20"}})
        self.assertNotIn("networks", worker)   # only the service sandboxes reach is on their network
        self.assertEqual(app["restart"], "unless-stopped")
        self.assertNotIn("restart", worker)    # what the package chose stays
        for service in (app, worker):
            self.assertEqual(service["logging"]["options"], {"max-size": "50m", "max-file": "5"})
            self.assertEqual(service["labels"], {self.hosted.LABEL: "app"})
        # A volume is a directory on the server's own disk: what fills it fills that disk only.
        self.assertEqual(added["volumes"], {"data": {"labels": {self.hosted.LABEL: "app"}, "driver": "local", "driver_opts": {
            "type": "none", "o": "bind", "device": "/srv/app/data"}}})
        self.assertEqual(added["networks"], {self.hosted.NETWORK: {"external": True}})

    def test_servers_api_starts_one_behind_its_answer_and_removes_it_with_its_connection(self):
        self.repo_admin()
        self.server_package()
        compose = self.hosted.hosted_compose

        class Now:   # the thread's work, done before the answer: what follows the 202
            def __init__(self, target, args, **kwargs):
                self.run = lambda: target(*args)

            def start(self):
                self.run()
        from sbx_control import servers_api
        with self.docker_without_containers(), patch.object(compose, "exists", return_value=False), \
                patch.object(compose, "up"), patch.object(compose, "down") as down, \
                patch.object(servers_api.threading, "Thread", Now):
            self.assertIn("fixture-app", [p["name"] for p in self.client.get("/api/servers").json()["packages"]])
            refused = self.client.post("/api/servers", json={"name": "Bad Name", "package": "fixture-app"})
            self.assertEqual(refused.status_code, 400, refused.text)
            started = self.client.post("/api/servers", json={"name": "app", "package": "fixture-app"})
            self.assertEqual((started.status_code, started.json()["status"]), (202, "preparing"))
            listed = self.client.get("/api/servers").json()["servers"]
            self.assertEqual([(s["name"], s["status"], s["package"]) for s in listed], [("app", "ready", "fixture-app")])
            connection = self.client.get("/api/catalog/connections").json()["connections"][0]
            self.assertEqual((connection["key"], connection["managed"]["status"]), ("app", "ready"))
            orphaning = self.client.delete("/api/catalog/connections/app")   # would leave the server running unseen
            self.assertEqual(orphaning.status_code, 409)
            self.assertIn("sbx servers rm app", orphaning.json()["detail"])
            taking = self.client.post("/api/catalog/connections", json={"package": "fixture-app", "key": "app",
                                                                        "inputs": {"url": "http://x", "token": "x"}})
            self.assertEqual(taking.status_code, 409, taking.text)   # the server's name stays its own
            removed = self.client.delete("/api/servers/app")
            self.assertEqual((removed.status_code, removed.json()["data_deleted"]), (200, True))
            self.assertEqual(down.call_args.kwargs, {"delete_data": True})
            self.assertEqual(self.client.get("/api/servers").json()["servers"], [])
            self.assertEqual(self.client.get("/api/catalog/connections").json()["connections"], [])
            self.assertEqual(self.client.delete("/api/servers/app").status_code, 404)
        self.assertTrue(next(p for p in self.client.get("/api/catalog").json()["packages"]
                             if p["name"] == "fixture-app")["server"])

    def test_sql_streams_into_the_owned_container_and_its_failure_is_returned(self):
        import io
        self.record("seed-exec", "postgres", {})
        details = [{"Config": {"Labels": {self.hosted.LABEL: "seed-exec"}}}]
        stream = io.StringIO("invalid sql;")
        with patch.object(self.hosted, "docker", return_value=Mock(stdout=json.dumps(details))), \
                patch.object(self.hosted.subprocess, "run", return_value=Mock(returncode=3)) as run:
            self.assertEqual(self.hosted.execute("seed-exec", "development_seed", stream), 3)
        self.assertIs(run.call_args.kwargs["stdin"], stream)
        self.assertEqual(run.call_args.args[0][-2:], ["-d", "development_seed"])
        self.assertIn("ON_ERROR_STOP=1", run.call_args.args[0])

    def test_sql_is_refused_for_a_container_the_server_does_not_own(self):
        self.record("foreign-exec", "clickhouse", {})
        with patch.object(self.hosted, "docker", return_value=Mock(stdout=json.dumps([{"Config": {"Labels": {}}}]))), \
                patch.object(self.hosted.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "not owned"):
                self.hosted.execute("foreign-exec", None, None)
            run.assert_not_called()

    def test_the_managed_postgres_url_and_its_clones_say_the_server_has_no_tls(self):
        from urllib.parse import parse_qs, urlsplit
        from catalog_scripts import load
        url = self.hosted.connection_inputs("postgres", "172.29.50.10", "fixture")["url"]
        self.assertEqual(parse_qs(urlsplit(url).query), {"sslmode": ["disable"]})
        with patch.dict("os.environ", {"PG_ADMIN_URL": url}):
            clone = load("postgres-clone", "connection.py").connection("sbx_fixture", "fixture-password")
        self.assertEqual(parse_qs(urlsplit(clone["url"]).query), {"sslmode": ["disable"]})
        self.assertNotIn("sslmode", self.hosted.connection_inputs("clickhouse", "172.29.50.11", "fixture")["url"])

    def test_shared_extents_must_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            a, b = Path(directory) / "a", Path(directory) / "b"
            a.touch()
            b.touch()
            with patch.object(self.cow, "extents", side_effect=[[(4096, 4096, 0x2000)], [(8192, 4096, 0x2000)]]):
                self.assertEqual(self.cow.shared_bytes(a, b), 0)
            with patch.object(self.cow, "extents", side_effect=[[(4096, 4096, 0x2000)], [(4096, 4096, 0x2000)]]):
                self.assertEqual(self.cow.shared_bytes(a, b), 4096)

    def test_a_package_servers_disk_is_mounted_before_docker_and_removed_only_once_unmounted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = {"mode": "xfs-image", "image": str(root / "v/volume.xfs"), "path": str(root / "v/data"), "size_gb": 3}
            ran = []

            def run(*args):
                ran.append(args)
                if args[0] == "mkfs.xfs":
                    self.assertEqual(Path(args[-1]).stat().st_size, 3 * 1024 ** 3)   # all of it reserved
                return {"blkid": "xfs", "systemd-escape": "fixture.mount"}.get(args[0], "")
            with patch.object(self.cow, "UNITS", root), patch.object(self.cow, "run", side_effect=run), \
                    patch.object(self.cow.os, "posix_fallocate", create=True,   # Linux's; not on every developer's machine
                                 side_effect=lambda fd, start, size: os.ftruncate(fd, size)), \
                    patch.object(self.cow.shutil, "disk_usage", return_value=Mock(free=10 * 1024 ** 3)), \
                    patch("os.path.ismount", return_value=True):
                found = self.cow.volume_prepare("app", storage, ["data", "logs"])
                self.assertEqual(found, {"data": f"{storage['path']}/data", "logs": f"{storage['path']}/logs"})
                unit = (root / "fixture.mount").read_text()
                self.assertIn("Before=docker.service", unit)   # a container never starts on the host's own disk
                self.assertIn(f"What={storage['image']}\nWhere={storage['path']}\nType=xfs", unit)
                self.assertIn(("systemctl", "enable", "--now", "fixture.mount"), ran)
                with self.assertRaisesRegex(RuntimeError, "still mounted"):
                    self.cow.volume_remove(storage)
                self.assertTrue(Path(storage["image"]).exists())
            with patch.object(self.cow, "UNITS", root), patch.object(self.cow, "run", side_effect=run), \
                    patch.object(self.cow.shutil, "disk_usage", return_value=Mock(free=4 * 1024 ** 3)):
                with self.assertRaisesRegex(ValueError, "not enough disk"):
                    self.cow.volume_prepare("other", {**storage, "image": str(root / "o/volume.xfs"), "path": str(root / "o/data")}, [])
            self.assertFalse((root / "o/volume.xfs").exists())

    def test_a_mounted_image_is_never_unlinked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "volume.xfs"
            image.touch()
            (root / "data").mkdir()
            storage = {"mode": "xfs-image", "image": str(image), "path": str(root / "data")}
            with patch.object(self.cow, "UNITS", root), patch.object(self.cow, "run", return_value="fixture.mount"), \
                    patch("os.path.ismount", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "still mounted"):
                    self.cow.remove(self.hosted.Server("fixture", "postgres"), storage)
            self.assertTrue(image.exists())



class ConnectionChoiceTests(Server):
    def bind(self, chosen, keys):
        from types import SimpleNamespace
        from sbx_control.catalog import attachments
        package = SimpleNamespace(requires={"server": {"kind": "postgres-server", "expose": {}}})
        found = [{"key": k, "package": "postgres-server", "disabled": False, "inputs": {}} for k in keys]
        with patch.object(attachments, "connections", return_value=found), \
                patch.object(attachments, "connection", side_effect=lambda k: next(c for c in found if c["key"] == k)):
            return attachments.bind(package, chosen, "service db")

    def test_a_named_connection_the_server_lacks_is_said_so(self):
        from sbx_control.catalog.attachments import MissingConnection
        with self.assertRaises(MissingConnection) as refused:
            self.bind({"server": "postgres"}, ["dev-pg"])
        self.assertEqual(str(refused.exception), "service db: names the postgres-server connection postgres, which this "
                         "server doesn't have (it has: dev-pg); name one with connections: {server: <key>}")

    def test_one_connection_is_bound_by_itself_and_of_several_one_is_named(self):
        from sbx_control.catalog.attachments import MissingConnection
        self.assertEqual(self.bind({}, ["dev-pg"])[0], {"server": "dev-pg"})
        self.assertEqual(self.bind({"server": "b"}, ["a", "b"])[0], {"server": "b"})
        with self.assertRaisesRegex(MissingConnection, r"several postgres-server connections \(a, b\)"):
            self.bind({}, ["a", "b"])
