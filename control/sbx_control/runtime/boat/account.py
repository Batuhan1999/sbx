"""What the settings page and the first-run setup show of a Boat account:
its plan, the key's expiry and this installation's running sandboxes."""
from __future__ import annotations

import datetime
import json
import time

from ... import howto
from ..hosted import hosted_status
from .plans import API_KEY, KEY_WARN_DAYS, KIND, TRIAL_NOTE, preset_of


class Account:
    """The account part of the Boat backend (mixed into Boat)."""

    def why_busy(self, memory_mb):
        return "Boat would not stop the sandbox now (it may be saving its disk); pause again in a minute"

    def readiness(self, facts):
        from ... import vault
        from ...connections.base import Check
        return [Check("Boat configured", bool(vault.runtime((API_KEY,)).get(API_KEY)), "a Boat API key",
                      f"Add it: sbx-admin runtime add {KIND} (the key on stdin).", help_url=self.help_url)]

    def _account(self) -> dict:
        """What Boat says of the account: its plan, and the key's expiry when Boat lists it."""
        found = self.call(lambda: self.api().limits(**self._org_args()))
        self._plan = (time.monotonic(), found.sandbox_plan_key or "")
        facts = {"plan": found.sandbox_plan_key or "", "running_at_boat": found.active_sandboxes,
                 "at_once": found.max_active_sandboxes}
        try:
            raw = self.api().api_keys_without_preload_content()
            keys = json.loads(raw.data or b"{}").get("apiKeys") or []
            mine = next((k for k in keys if self._key.endswith(k.get("keyLastFour") or "\0")), None)
            if mine and mine.get("expiresAt"):
                facts["key_expires_at"] = mine["expiresAt"]
        except Exception:   # a scoped key may not list keys: no expiry warning then
            pass
        return facts

    def account_plan(self):
        """The preset Boat's own plan key for the account names (plans.preset_of), asked now."""
        return preset_of(self.plan(fresh=True))

    def _expiry_warning(self, facts: dict) -> str:
        when = facts.get("key_expires_at")
        if not when:
            return ""
        try:
            at = datetime.datetime.fromisoformat(str(when).replace("Z", "+00:00"))
        except ValueError:
            return ""
        days = (at - datetime.datetime.now(datetime.timezone.utc)).days
        if days >= KEY_WARN_DAYS:
            return ""
        return "the API key has expired" if days < 0 else f"the API key expires in {days} days"

    def _notes(self, facts: dict) -> list:
        notes = [TRIAL_NOTE.rstrip(".")] if facts.get("plan") == "trial" else []
        if warning := self._expiry_warning(facts):
            notes.append(warning)
        return notes

    def status(self):
        from ...connections.base import Field
        return hosted_status(self, configured=bool(self._key), account=self._account, notes=self._notes,
                             fields=(Field("api_url", "API URL", value=self._url, kind="url"),))

    def test(self):
        from ...connections.base import Check
        try:
            facts = self._account()
            count = len(self.templates())
        except Exception as error:
            return [Check("Boat API", False, f"{type(error).__name__}: {error}",
                          f"check {API_KEY} in /etc/sbx/runtime.env (sbx-admin runtime add {KIND})",
                          help_url=self.help_url)]
        checks = [Check("Boat API", True, f"answers; plan {facts.get('plan') or 'unknown'}; {count} saved snapshots")]
        if facts.get("plan") == "trial":
            checks.append(Check("Boat plan", False, "the trial: 2 sandboxes at once, up to 2 hours, no saved snapshots",
                                "Templates need the first payment: Boat answers \"Saved snapshots are not available "
                                "during the free trial\". Pay in Boat's dashboard (Billing), then build again.",
                                help_url=howto.BOAT_PLAN))
        if warning := self._expiry_warning(facts):
            checks.append(Check("Boat API key", False, warning, "create a new key and add it again",
                                help_url=self.help_url))
        return checks
