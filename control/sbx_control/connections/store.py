"""The secret store: where every secret value lives (secret_store.py).
Local (sealed in sbx.db) or the Infisical the installer runs on this host;
`sbx-admin store enable` sets Infisical up and moves the values into it."""
from __future__ import annotations

from .. import secret_store, settings, variables, vault
from .base import Check, Connection, Field, Refused, Status

MODES = ("local", "infisical")


class Store(Connection):
    kind = "store"
    title = "Secrets store"
    group = "internal"

    def status(self) -> Status:
        s = settings.current().store
        client_id, client = vault.facts(vault.INFISICAL_CLIENT_ID), vault.facts(vault.INFISICAL_CLIENT_SECRET)
        facts: dict = {"mode": s.mode}
        if s.mode == "local":
            state, summary = "connected", "Local · sealed in sbx.db with the recovery key (sbx-admin store enable: Infisical)"
        else:
            identity = client_id.set and client.set
            state = "connected" if identity else "attention"
            summary = "Infisical · live updates from its UI" if identity else "Infisical · no machine identity"
            facts["project"] = s.project
        fields = (Field("mode", "Mode", value=s.mode, help=", ".join(MODES)),
                  Field("port", "Infisical port", value=str(s.port), help=f"on this host: {s.url}"),
                  Field("project", "Project id", value=s.project),
                  Field("environment", "Environment", value=s.environment),
                  Field("client_id", "Machine identity client id", secret=True, set=client_id.set),
                  Field("client_secret", "Machine identity client secret", secret=True, set=client.set),
                  Field("webhook_token", "Webhook token", secret=True, set=vault.facts(vault.INFISICAL_WEBHOOK_TOKEN).set,
                        help="signing key for https://<login>/webhooks/infisical; made automatically"))
        return Status(self.kind, self.title, state, summary, facts, fields, ("connect", "test"))

    def test(self, values: dict | None = None) -> list[Check]:
        if settings.current().store.mode == "local":
            return [Check("store", True, "local: sealed in sbx.db")]
        checks = []
        try:
            store = secret_store.infisical()
            store.answers()
            checks.append(Check("Infisical answers", True, store.url))
        except secret_store.StoreError as error:
            return [Check("Infisical answers", False, str(error), "systemctl status sbx-infisical")]
        try:
            store.signed_in()
            checks.append(Check("machine identity", True, "signed in"))
        except secret_store.StoreError as error:
            checks.append(Check("machine identity", False, str(error), "INFISICAL_CLIENT_ID and INFISICAL_CLIENT_SECRET"))
            return checks
        repos = [r for r in settings.current().repos if "*" not in r]
        if repos:
            try:
                found = store.list(variables.scope_of(repos[0]), fresh=True)
                checks.append(Check(f"{repos[0]}'s folder", True, f"{len(found)} values"))
            except secret_store.StoreError as error:
                checks.append(Check(f"{repos[0]}'s folder", False, str(error), "the project and environment"))
        return checks

    def connect(self, values: dict, *, by: str) -> Status:
        s = settings.current().store
        mode = values.get("mode", s.mode)
        if mode not in MODES:
            raise Refused(f"mode must be one of {', '.join(MODES)}")
        if mode != s.mode:
            raise Refused("moving the values between stores is sbx-admin store enable's job")
        new = {**s.model_dump(), **{f: values[f].strip() for f in ("port", "project", "environment") if values.get(f)}}
        try:
            settings.update("store", new, by=by)
        except settings.Invalid as error:
            raise Refused(str(error)) from error
        for field, key in (("client_id", vault.INFISICAL_CLIENT_ID), ("client_secret", vault.INFISICAL_CLIENT_SECRET),
                           ("webhook_token", vault.INFISICAL_WEBHOOK_TOKEN)):
            if values.get(field):
                vault.put(key, values[field].strip(), by=by)
        if mode == "infisical":
            checks = self.test()
            if not all(c.ok for c in checks):
                raise Refused("saved, but a check failed", checks)
        return self.status()
