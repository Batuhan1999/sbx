"""What the settings page and the first-run setup show of a Lizard account:
its plan (GET /api/sandboxes/limits: tier, unit, limits, running), the
project sandboxes are billed to and this installation's running sandboxes."""
from __future__ import annotations

from ..base import Failed
from ..hosted import hosted_status
from .plans import API_KEY, KIND, PROJECT_KEY, TRIAL_NOTE


class Account:
    """The account part of the Lizard backend (mixed into Lizard)."""

    def project(self) -> str:
        """The id of the project sandboxes are billed to: LIZARD_PROJECT (an
        id, slug or name, as Lizard's SDK resolves one), or the only project
        the key sees. Asked once."""
        if self._project_id:
            return self._project_id
        projects = [p for p in self.call("GET", "/api/projects") or [] if isinstance(p, dict) and p.get("id")]
        wanted = self._project.lower()
        if wanted:
            found = [p for p in projects if wanted in (str(p["id"]).lower(), str(p.get("slug") or "").lower(),
                                                       str(p.get("name") or "").lower())]
        else:
            found = projects
        if len(found) != 1:
            names = ", ".join(str(p.get("slug") or p["id"]) for p in projects) or "none"
            why = (f"no project {self._project!r}" if wanted and not found else
                   f"{self._project!r} names several projects" if wanted else
                   "the key sees no project" if not projects else "the key sees several projects")
            raise Failed(f"Lizard: {why} (it sees: {names}). Set {PROJECT_KEY} to the project's ID: "
                         f"sbx runtimes set {KIND} --set {PROJECT_KEY}=...")
        self._project_id = str(found[0]["id"])
        return self._project_id

    def why_busy(self, memory_mb):
        return "Lizard would not pause the sandbox now; pause again in a minute"

    def readiness(self, facts):
        from ... import vault
        from ...connections.base import Check
        return [Check("Lizard configured", bool(vault.runtime((API_KEY,)).get(API_KEY)), "a Lizard API key",
                      f"Add it: sbx-admin runtime add {KIND} (the key on stdin).", help_url=self.help_url)]

    def _account(self) -> dict:
        """What Lizard says of the account: its tier and what runs there."""
        found = self.call("GET", "/api/sandboxes/limits") or {}
        if not isinstance(found, dict):
            return {}
        unit = found.get("unit") if found.get("unit") in ("vcpu", "sandboxes") else ""
        return {"plan": found.get("tier") or "", "unit": unit,
                "running_at_lizard": found.get("runningVcpu") if unit == "vcpu" else found.get("running")}

    def _notes(self, facts: dict) -> list:
        return [TRIAL_NOTE.rstrip(".")] if facts.get("plan") == "trial" else []

    def status(self):
        from ...connections.base import Field
        return hosted_status(self, configured=bool(self._key), facts={"region": self._region},
                             account=self._account, notes=self._notes,
                             fields=(Field("api_url", "API URL", value=self._url, kind="url"),))

    def test(self):
        """The key, the project and the snapshots answer; the trial is said."""
        from ... import howto
        from ...connections.base import Check
        try:
            project = self.project()
            count = len(self.templates())
        except Exception as error:
            return [Check("Lizard API", False, f"{type(error).__name__}: {error}",
                          f"check {API_KEY} and {PROJECT_KEY} in /etc/sbx/runtime.env (sbx-admin runtime add {KIND})",
                          help_url=self.help_url)]
        try:
            facts = self._account()
        except Exception:   # a key limited to a project may not read the account's limits
            facts = {}
        checks = [Check("Lizard API", True, f"answers; project {project}; plan {facts.get('plan') or 'unknown'}; "
                                            f"{count} snapshots")]
        if facts.get("plan") == "trial":
            checks.append(Check("Lizard plan", False, "the trial: every sandbox is deleted an hour after it starts",
                                "sbx keeps sandboxes paused for days; on the trial Lizard deletes them, with what is "
                                "in them. Start Pro in Lizard's dashboard (Billing).", help_url=howto.LIZARD_PLAN))
        return checks
