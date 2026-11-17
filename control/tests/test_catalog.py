"""Catalog packages through their boundaries: package folders on disk (the
operator's catalog), real scripts that log what they were run with, the
connections and attachments tables, and the HTTP API."""
import base64
import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

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
        for heading in ("# Service selection", "# Acceptance checklist", "# Resource guide"):
            self.assertIn(f"\n{heading}\n", brief)
        self.assertIn("Delete A (`sbx down check-a`)", brief)
        self.assertNotIn("Record what you verified", brief.split("# Acceptance checklist")[0])

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
            "# Alpha\n\n## Run it\n\nsbx up\n\n## How this recipe was made\n\nIt chose postgres-clone.\n\n## Later\n\nnot this\n")
        (folder / "examples/plain/README.md").write_text("# Plain\n\n## Run it\n\nsbx up\n")
        real = paths.shipped
        with patch.object(paths, "shipped", lambda name: folder / name if name == "examples" else real(name)):
            guides = repos.guides()
        self.assertEqual([g.splitlines()[0] for g in guides],
                         ["# Examples", "# How this recipe was made: Alpha (examples/alpha)", "# Service selection",
                          "# Acceptance checklist", "# Resource guide"])
        self.assertEqual(guides[1].split("\n\n", 1)[1], "It chose postgres-clone.")


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

