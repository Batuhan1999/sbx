"""What the settings page and the first-run setup show of a Boat account:
its plan, the key's expiry and this installation's running sandboxes."""
from __future__ import annotations

import datetime
import json
import time

from .plans import API_KEY, KEY_WARN_DAYS, KIND, TRIAL_NOTE


class Account:
    """The account part of the Boat backend (mixed into Boat)."""

    def why_busy(self, memory_mb):
        return "Boat would not stop the sandbox now (it may be saving its disk); pause again in a minute"

    def readiness(self, facts):
        from ... import vault
        from ...connections.base import Check
        return [Check("Boat configured", bool(vault.runtime((API_KEY,)).get(API_KEY)), "a Boat API key",
                      f"Add it: sbx-admin runtime add {KIND} (the key on stdin).")]

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

    def status(self):
        from ...connections.base import Field, Status
        if not self._key:
            return Status("runtime", "Sandbox runtime", "not_connected", f"Boat: no {API_KEY} in runtime.env",
                          {}, (), ("test",))
        try:
            from ... import runtime   # this installation's sandboxes, not the whole account's
            running = len(runtime.sandboxes(None, [runtime.RUNNING], kind=self.kind))
            facts = self._account()
        except Exception as error:
            return Status("runtime", "Sandbox runtime", "attention", f"Boat did not answer: {error}", {}, (),
                          ("test",))
        facts["running"] = running
        notes = []
        if facts.get("plan") == "trial":
            notes.append(TRIAL_NOTE.rstrip("."))
        if warning := self._expiry_warning(facts):
            notes.append(warning)
        most = self._limits.sandboxes
        full = most is not None and running >= most
        summary = " · ".join([f"Boat · {running} running here", *notes, *(["full"] if full else [])])
        return Status("runtime", "Sandbox runtime", "attention" if notes or full else "connected", summary, facts,
                      (Field("api_url", "API URL", value=self._url),), ("test",))

    def test(self):
        from ...connections.base import Check
        try:
            facts = self._account()
            count = len(self.templates())
        except Exception as error:
            return [Check("Boat API", False, f"{type(error).__name__}: {error}",
                          f"check {API_KEY} in /etc/sbx/runtime.env (sbx-admin runtime add {KIND})")]
        checks = [Check("Boat API", True, f"answers; plan {facts.get('plan') or 'unknown'}; {count} saved snapshots")]
        if facts.get("plan") == "trial":
            checks.append(Check("Boat plan", False, "the trial: 2 sandboxes at once, up to 2 hours, no saved snapshots",
                                "Templates need the first payment: Boat answers \"Saved snapshots are not available "
                                "during the free trial\". Pay in Boat's dashboard (Billing), then build again."))
        if warning := self._expiry_warning(facts):
            checks.append(Check("Boat API key", False, warning, "create a new key and add it again"))
        return checks
