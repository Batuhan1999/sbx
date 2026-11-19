"""Secret requests: an agent asks the owner for the keys it needs, and the
owner pastes them once on the keys page (/keys/<id>), signed in.

A request lists items, each a target that says where the key goes:

  runtime:KIND[:KEY]        a sandbox runtime's secret setting (KEY: its secret_key);
                            added with it when the runtime is not enabled yet
  dns:cloudflare            the DNS provider token the certificate's DNS-01 challenge uses
  connector:KIND[:FIELD]    an extension's secret field (FIELD: its first secret one)
  repo:OWNER/NAME:VAR       a trusted repo's secret value VAR

A repo item may reuse a key the server holds already instead of asking for
it again: `repo:OWNER/NAME:VAR=from:SOURCE`, SOURCE one of runtime:KIND[:KEY],
dns:cloudflare or repo:OWNER/NAME:VAR (held now, or an item of the same
request, whose pasted value it then takes).

A value goes straight into the store its target already has, by that
store's own code path (runtime_settings and runtime_changes, vault, the
connector's connect, variables), checked where that is cheap (a runtime by
its provider, the DNS token by Cloudflare, a connector by its connect).
Values are never kept here, shown, logged or sent back: the table holds
targets, labels and which items are done. A request is good until it
expires (24 hours unless asked otherwise), and is done once every item is."""
from __future__ import annotations

import dataclasses
import datetime
import json
import re
import secrets
import threading
import urllib.error
import urllib.request

from . import state

DEFAULT_SECONDS = 24 * 3600
MIN_SECONDS = 5 * 60
MAX_SECONDS = 7 * 24 * 3600
MAX_ITEMS = 20
MAX_VALUE = 16384
KEEP_DAYS = 30
CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
KINDS = ("runtime", "dns", "connector", "repo")
REUSE = "=from:"

_lock = threading.Lock()   # one submit at a time: an item is applied once


class Refused(ValueError):
    """A request or target that can't be: the message says why (the API answers 400)."""


class Gone(Refused):
    """The request is done, cancelled or expired (the API answers 409)."""


# Targets


@dataclasses.dataclass(frozen=True)
class Target:
    kind: str                  # runtime, dns, connector, repo
    name: str                  # the runtime's kind, the DNS provider, the connector's kind, the repo
    key: str                   # the setting, field or variable
    source: "Target | None" = None

    def __str__(self) -> str:
        own = f"{self.kind}:{self.name}" if self.kind == "dns" else f"{self.kind}:{self.name}:{self.key}"
        return own + (f"{REUSE}{self.source}" if self.source else "")

    @property
    def own(self) -> str:
        """The target without its source: where the value goes."""
        return str(dataclasses.replace(self, source=None))


def parse(text: str, *, reusing: bool = False) -> Target:
    """A target as given, completed with its default key, checked against
    what the server has: a runtime that is a provider's, a connector with
    that secret field, a trusted repo. Refused, saying why."""
    text = (text or "").strip()
    head, sep, rest = text.partition(REUSE)
    if sep and reusing:
        raise Refused(f"{text}: a reused key names where it is held, not another source")
    source = parse(rest, reusing=True) if sep else None
    kind, _, spec = head.partition(":")
    if kind not in KINDS or not spec:
        raise Refused(f"{text or '(empty)'}: a target is runtime:KIND, dns:cloudflare, connector:KIND or "
                      "repo:OWNER/NAME:VAR")
    if kind == "runtime":
        target = Target(kind, *_runtime_key(*_split(spec, text)))
    elif kind == "dns":
        if spec.lower() != "cloudflare":
            raise Refused(f"{text}: the DNS provider whose token sbx keeps is cloudflare (dns:cloudflare)")
        target = Target(kind, "cloudflare", "CLOUDFLARE_API_TOKEN")
    elif kind == "connector":
        target = Target(kind, *_connector_field(*_split(spec, text)))
    else:
        repo, _, var = spec.rpartition(":")
        target = Target(kind, _repo(repo, text), _var(var, text))
    if source and target.kind != "repo":
        raise Refused(f"{text}: only a repo value may reuse a key the server holds")
    if source and source.kind == "connector":
        raise Refused(f"{text}: a connector's secret can't be reused; reuse a runtime's, dns:cloudflare or a repo's")
    return dataclasses.replace(target, source=source)


def _split(spec: str, text: str) -> tuple[str, str]:
    name, _, key = spec.partition(":")
    if not name or ":" in key:
        raise Refused(f"{text}: expected KIND or KIND:KEY")
    return name.lower(), key


def _runtime_key(kind: str, key: str) -> tuple[str, str]:
    from . import runtime
    if kind not in runtime.kinds():
        raise Refused(f"there is no runtime {kind}; the runtimes here are {', '.join(runtime.kinds())}")
    backend = _backend(kind)
    if backend.host:
        raise Refused(f"{backend.title} ({kind}) runs on this server: install.sh sets up its keys")
    secrets_ = [f.key for f in backend.settings if f.secret] or ([backend.secret_key] if backend.secret_key else [])
    key = key or backend.secret_key
    if key not in secrets_:
        raise Refused(f"{backend.title} has no secret setting {key or '(none)'}; its secrets: "
                      f"{', '.join(secrets_) or 'none'}")
    return kind, key


def _backend(kind: str):
    from . import runtime
    return runtime.get(kind) if kind in runtime.enabled() else runtime.make(kind)


def _connector(kind: str):
    from . import extensions
    found = extensions.get(kind)
    if found is None:
        raise Refused(f"there is no connector {kind}; the connectors here are "
                      f"{', '.join(c.kind for c in extensions.connectors())}")
    return found


def _connector_field(kind: str, field: str) -> tuple[str, str]:
    connector = _connector(kind)
    secret = [f.name for f in connector.status().fields if f.secret]
    field = field or (secret[0] if secret else "")
    if field not in secret:
        raise Refused(f"{connector.title} has no secret field {field or '(none)'}; its secret fields: "
                      f"{', '.join(secret) or 'none'}")
    return kind, field


def _repo(text: str, target: str) -> str:
    from . import repo_path, settings
    try:
        repo = repo_path.normalize(text)
    except ValueError as error:
        raise Refused(f"{target}: {error}") from None
    if settings.trust(repo) != "trusted":
        raise Refused(f"{target}: {repo} is not a trusted repo here (sbx repo connect, or the web app)")
    return repo


def _var(name: str, target: str) -> str:
    if not VAR_RE.match(name or ""):
        raise Refused(f"{target}: {name or '(none)'} is not a variable name")
    return name


# Where a target's value is, and what it says


def held(target: Target) -> str | None:
    """The value the server holds for target now (a source), or None.
    Server-side only: never sent anywhere but into another store."""
    from . import runtime, secret_store, variables, vault
    if target.kind == "runtime":
        if target.name not in runtime.enabled():
            return None
        return vault.runtime(runtime.get(target.name).credential_keys).get(target.key) or None
    if target.kind == "dns":
        return vault.get(vault.CLOUDFLARE_API_TOKEN) or None
    if target.kind == "repo":
        try:
            return variables.values(target.name).get(target.key) or None
        except secret_store.StoreError:
            return None
    return None


def describe(target: Target) -> dict:
    """{what, title, help_url, hint}: where the value goes, in words, and how to get it."""
    from . import howto
    if target.kind == "runtime":
        backend = _backend(target.name)
        field = next((f for f in backend.settings if f.key == target.key), None)
        label = field.label if field else target.key
        return {"what": f"{backend.title}'s {label}", "title": backend.title, "help_url": backend.help_url,
                "hint": field.help if field else ""}
    if target.kind == "dns":
        return {"what": "the Cloudflare API token for this server's certificate", "title": "Cloudflare",
                "help_url": howto.CLOUDFLARE_TOKEN, "hint": "A token that may edit the zone's DNS records."}
    if target.kind == "connector":
        connector = _connector(target.name)
        field = next((f for f in connector.status().fields if f.name == target.key), None)
        return {"what": f"{connector.title}'s {field.label if field else target.key}", "title": connector.title,
                "help_url": connector.help_url, "hint": field.help if field else ""}
    topic = next((topic for prefix, topic in (("E2B_", "e2b"), ("BOAT_", "boat"), ("BEAM_", "beam"), ("CLOUDFLARE_", "cloudflare"),
                                              ("GITHUB_", "github"), ("AMP_", "amp"))
                  if target.key.upper().startswith(prefix)), "")
    return {"what": f"{target.key} for {target.name}'s sandboxes", "title": target.name,
            "help_url": howto.url(topic) if topic else "", "hint": ""}


def _source_words(source: Target) -> str:
    if source.kind == "repo":
        return f"{source.key} of {source.name}"
    return describe(source)["what"]


# Requests


SCHEMA = state.Schema(tables="""
create table if not exists secret_requests (
    id text primary key, created_by text not null, created_at text not null, expires_at text not null,
    state text not null default 'pending', note text not null default '', items text not null,
    finished_at text, finished_by text);
""")


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def _stamp(at: datetime.datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _help_url(url: str | None) -> str:
    url = (url or "").strip()
    if url and not re.fullmatch(r"https://[^\s<>\"']+", url):
        raise Refused(f"{url}: a help link is an https:// URL")
    return url


def create(items: list[dict], *, by: str, expires_in: int | None = None, note: str = "") -> dict:
    """A new request for items ({target, label, help_url}), good for
    expires_in seconds (24 hours by default, 5 minutes to 7 days). Each
    target is checked now; a reused key must be held already or be another
    item's. The request as view() shows it, without its URL."""
    if not items:
        raise Refused("name at least one key to ask for")
    if len(items) > MAX_ITEMS:
        raise Refused(f"at most {MAX_ITEMS} keys in one request")
    seconds = DEFAULT_SECONDS if expires_in is None else int(expires_in)
    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        raise Refused("a request expires after 5 minutes to 7 days")
    found, seen = [], set()
    for item in items:
        target = parse(str(item.get("target") or ""))
        if target.own in seen:
            raise Refused(f"{target.own} is asked for twice")
        seen.add(target.own)
        label = " ".join(str(item.get("label") or "").split())[:200]
        found.append((target, label, _help_url(item.get("help_url"))))
    for target, _, _ in found:
        if target.source and str(target.source) not in seen and held(target.source) is None:
            raise Refused(f"{target}: the server holds no {_source_words(target.source)} to reuse; ask for it "
                          f"instead (or add {target.source} to this request)")
    stored = []
    for target, label, help_url in found:
        words = describe(target)
        stored.append({"target": str(target), "label": label or words["what"][:1].upper() + words["what"][1:],
                       "help_url": help_url or words["help_url"], "done_at": None, "done_by": None})
    now = _now()
    request_id = secrets.token_urlsafe(12)
    with state.db() as conn:
        conn.execute("delete from secret_requests where created_at < ?",
                     (_stamp(now - datetime.timedelta(days=KEEP_DAYS)),))
        conn.execute("insert into secret_requests (id, created_by, created_at, expires_at, note, items) "
                     "values (?, ?, ?, ?, ?, ?)",
                     (request_id, by, _stamp(now), _stamp(now + datetime.timedelta(seconds=seconds)),
                      " ".join((note or "").split())[:500], json.dumps(stored)))
    print(f"secret request {request_id}: {by} asked for {', '.join(str(t) for t, _, _ in found)}", flush=True)
    return get(request_id)


def _row(request_id: str, conn) -> dict | None:
    if not ID_RE.match(request_id or ""):
        return None
    row = conn.execute("select id, created_by, created_at, expires_at, state, note, items, finished_at, finished_by "
                       "from secret_requests where id = ?", (request_id,)).fetchone()
    if not row:
        return None
    keys = ("id", "created_by", "created_at", "expires_at", "state", "note", "items", "finished_at", "finished_by")
    found = dict(zip(keys, row))
    found["items"] = json.loads(found["items"])
    if found["state"] == "pending" and found["expires_at"] <= _stamp(_now()):
        found["state"] = "expired"
    return found


def view(row: dict) -> dict:
    """What anyone allowed may see of a request: its state and each item's
    target, label, where it goes, how to get it and whether it is done;
    never a value."""
    items = []
    _, progress = runtime_progress(row["items"])
    for item in row["items"]:
        try:
            target = parse(item["target"])
            words = describe(target)
        except Refused as error:   # gone since (a runtime, a repo): said, and refused on submit
            target, words = None, {"what": item["target"], "title": "", "hint": str(error)}
        items.append({"target": item["target"], "label": item["label"], "help_url": item["help_url"],
                      "what": words["what"], "hint": words.get("hint", ""),
                      "reuse": str(target.source) if target and target.source else None,
                      "reuse_from": _source_words(target.source) if target and target.source else None,
                      "done": bool(item["done_at"]), "done_at": item["done_at"], "done_by": item["done_by"],
                      "applied": progress.get(item["target"])})
    return {"id": row["id"], "state": row["state"], "created_by": row["created_by"], "created_at": row["created_at"],
            "expires_at": row["expires_at"], "note": row["note"], "finished_at": row["finished_at"],
            "finished_by": row["finished_by"], "items": items,
            "remaining": sum(1 for i in items if not i["done"]), "runtime_changes": _overall(progress)}


def get(request_id: str) -> dict | None:
    with state.db() as conn:
        row = _row(request_id, conn)
    return view(row) if row else None


def listed(limit: int = 50) -> list:
    """The latest requests, newest first."""
    with state.db() as conn:
        ids = [r[0] for r in conn.execute("select id from secret_requests order by created_at desc, rowid desc "
                                          "limit ?", (limit,))]
        rows = [_row(i, conn) for i in ids]
    return [view(r) for r in rows if r]


def cancel(request_id: str, *, by: str) -> dict:
    with _lock, state.db() as conn:
        row = _row(request_id, conn)
        if row is None:
            raise KeyError(request_id)
        if row["state"] != "pending":
            raise Gone(f"the request is {row['state']} already")
        conn.execute("update secret_requests set state = 'cancelled', finished_at = ?, finished_by = ? where id = ?",
                     (_stamp(_now()), by, request_id))
    print(f"secret request {request_id}: cancelled by {by}", flush=True)
    return get(request_id)


def submit(request_id: str, values: dict, *, by: str) -> dict:
    """The owner's values by target, put where they go: the pasted ones
    first (apply; the runtime keys of every runtime together, as one change
    the host applies, _queue_runtimes), then each item that reuses a key.
    An item that fails stays pending with its error. {request, ok, results:
    [{target, ok, error}]}; the request is completed once no item is left.
    KeyError for no such request, Gone when it is not pending."""
    given = {}
    for text, value in (values or {}).items():
        value = "" if value is None else str(value).strip()
        if not value:
            continue
        try:
            given[parse(str(text)).own] = value
        except Refused:
            given[str(text)] = value   # matched below by the item's own text, or refused as unknown
    with _lock:
        with state.db() as conn:
            row = _row(request_id, conn)
        if row is None:
            raise KeyError(request_id)
        if row["state"] != "pending":
            raise Gone(f"this request is {row['state']}" + ("; ask your agent for a new one"
                                                            if row["state"] == "expired" else ""))
        targets = {}
        for item in row["items"]:
            try:
                targets[item["target"]] = parse(item["target"])
            except Refused as error:
                targets[item["target"]] = error
        known = {t.own for t in targets.values() if isinstance(t, Target)}
        unknown = [t for t in given if t not in known]
        if unknown:
            raise Refused(f"this request does not ask for {', '.join(unknown)}")
        outcome, pasted, runtime_items = {}, {}, []

        def done(item, value, change=""):
            pasted[targets[item["target"]].own] = value
            item["done_at"], item["done_by"] = _stamp(_now()), by
            if change:
                item["change"] = change
            outcome[item["target"]] = ""

        waiting = [i for i in row["items"] if not i["done_at"]]
        for item in waiting:
            target = targets[item["target"]]
            if isinstance(target, Refused):
                outcome[item["target"]] = str(target)
            elif not target.source and (value := given.get(target.own)) is not None:
                if target.kind == "runtime":
                    runtime_items.append((item, target, value))
                elif error := apply(target, value, by=by):
                    outcome[item["target"]] = error
                else:
                    done(item, value)
        if runtime_items:
            change, errors = _queue_runtimes([(t, v) for _, t, v in runtime_items], by=by)
            for item, target, value in runtime_items:
                if errors.get(target.own):
                    outcome[item["target"]] = errors[target.own]
                else:
                    done(item, value, change)
        asked = {t.own: i for i, t in ((i, targets[i["target"]]) for i in row["items"]) if isinstance(t, Target)}
        for item in waiting:
            target = targets[item["target"]]
            if not isinstance(target, Target) or not target.source:
                continue
            # A key this request asks for anew that is not in yet: never the one it replaces.
            source = asked.get(str(target.source))
            value = pasted.get(str(target.source)) or (None if source and not source["done_at"] else held(target.source))
            if not value:
                outcome[item["target"]] = f"this uses {_source_words(target.source)}, which is not saved yet"
            elif error := apply(target, value, by=by):
                outcome[item["target"]] = error
            else:
                done(item, value)
        if not outcome:
            raise Refused("paste at least one of the keys")
        complete = all(i["done_at"] for i in row["items"])
        with state.db() as conn:
            conn.execute("update secret_requests set items = ?, state = ?, finished_at = ?, finished_by = ? "
                         "where id = ?", (json.dumps(row["items"]), "completed" if complete else "pending",
                                          _stamp(_now()) if complete else None, by if complete else None,
                                          request_id))
    results = [{"target": i["target"], "ok": not outcome[i["target"]], "error": outcome[i["target"]]}
               for i in row["items"] if i["target"] in outcome]
    if saved := [r["target"] for r in results if r["ok"]]:
        print(f"secret request {request_id}: {by} set {', '.join(saved)}", flush=True)
    return {"request": get(request_id), "ok": all(r["ok"] for r in results), "results": results}


# Putting a value where it goes


def apply(target: Target, value: str, *, by: str) -> str:
    """Store value for target by that store's own code path: "" when it is
    stored, else why not (never the value)."""
    if len(value) > MAX_VALUE or any(c in value for c in "\r\n\0"):
        return "paste the key alone, on one line"
    try:
        return {"dns": _apply_dns, "connector": _apply_connector, "repo": _apply_repo}[target.kind](target, value, by)
    except Refused as error:
        return str(error)
    except Exception as error:   # a store or provider that failed: said, the item stays pending
        print(f"secret request: {target} failed: {type(error).__name__}", flush=True)
        return f"not saved: {type(error).__name__}: {_scrub(str(error), value)}"


def _scrub(text: str, value: str) -> str:
    return text.replace(value, "••••") if value else text


def _queue_runtimes(found: list, *, by: str) -> tuple[str, dict]:
    """Runtime keys ([(target, value)]) as Settings → Sandbox runtimes takes
    them, one runtime at a time (runtime_settings.prepare: checked by the
    provider, the secret sealed; a runtime not enabled is added on its first
    plan, as `sbx runtimes add`), then every runtime that passed queued
    together as one change (runtime_changes.request_batch), which the host
    applies in order before restarting sbx-control once. (the change's id
    to follow it by, or ""; {target.own: why it was refused}) ."""
    from . import runtime, runtime_changes, runtime_settings, runtimes
    errors, by_kind = {}, {}
    for target, value in found:
        if len(value) > MAX_VALUE or any(c in value for c in "\r\n\0"):
            errors[target.own] = "paste the key alone, on one line"
        else:
            by_kind.setdefault(target.name, []).append((target, value))
    if by_kind and runtime_changes.busy():
        why = "another runtime change is being applied on the server; save this key again once it is done"
        return "", {**errors, **{t.own: why for pairs in by_kind.values() for t, _ in pairs}}
    changes = []
    for kind, pairs in by_kind.items():
        adding = kind not in runtime.enabled()
        backend = _backend(kind)
        plan = backend.presets[0].name if adding and backend.presets else None
        try:
            values = runtime_settings.prepare(kind, {t.key: v for t, v in pairs}, adding=adding, plan=plan)
        except runtimes.Refused as error:
            for target, value in pairs:
                errors[target.own] = _scrub(str(error), value)
            continue
        except Exception as error:   # the provider or the store failed: said, the item stays pending
            for target, value in pairs:
                errors[target.own] = f"not saved: {type(error).__name__}: {_scrub(str(error), value)}"
            continue
        changes.append(({"operation": "add" if adding else "change", "kind": kind, "values": values}, pairs))
    if not changes:
        return "", errors
    try:
        queued = runtime_changes.request_batch([c for c, _ in changes], by=by)
    except (runtimes.Refused, OSError) as error:   # Busy (another change was queued meanwhile), or not written
        why = str(error) if isinstance(error, runtimes.Refused) else f"not queued: {type(error).__name__}"
        for change, pairs in changes:
            runtime_changes.discard_references(change["values"])
            errors.update({t.own: f"{why}; save this key again once it is done" for t, _ in pairs})
        return "", errors
    return queued["id"], errors


def runtime_progress(items: list) -> tuple[dict, dict]:
    """Where the host is with the runtime changes these stored items were
    queued with ({change id: outcome}, and {target: {state, message}} per
    runtime item): queued, applying, restarting (made; sbx-control restarts
    to take it), done or failed, with the host's message; unknown when the
    host's record no longer has it."""
    from . import runtime_changes
    found, per_item = {}, {}
    for item in items:
        change = item.get("change")
        if not change:
            continue
        if change not in found:
            found[change] = runtime_changes.outcome(change)
        record = found[change]
        if record is None:
            per_item[item["target"]] = {"state": "unknown", "message": "the server no longer says how this went"}
            continue
        state, message = record.get("state"), record.get("message") or ""
        kind = item["target"].split(":")[1]
        mine = next((r for r in record.get("results") or [] if r.get("kind") == kind), None)
        if state in ("done", "failed") and mine is not None:   # this runtime's own outcome in the batch
            state, message = ("done" if mine.get("ok") else "failed"), mine.get("message") or ""
        if state == "done" and record.get("restarting"):
            state = "restarting"
        per_item[item["target"]] = {"state": state, "message": message}
    return found, per_item


def _overall(per_item: dict) -> dict:
    """One state for every runtime change of a request: failed if any did,
    else the earliest still in progress, else done (none without any)."""
    if not per_item:
        return {"state": "none", "message": ""}
    for name in ("failed", "queued", "applying", "restarting", "unknown", "done"):
        found = [p for p in per_item.values() if p["state"] == name]
        if found:
            return {"state": name, "message": "; ".join(dict.fromkeys(p["message"] for p in found if p["message"]))}
    return {"state": "done", "message": ""}


def verify_cloudflare(token: str) -> str:
    """Whether Cloudflare takes the token: "" when it does, else why not. A
    user token answers /user/tokens/verify; an account token is tried by
    listing the zones it may see."""
    def call(path):
        request = urllib.request.Request(f"{CLOUDFLARE_API}{path}",
                                         headers={"Authorization": f"Bearer {token}", "User-Agent": "sbx"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            try:
                return json.loads(error.read() or b"{}")
            except ValueError:
                return {"success": False, "errors": [{"message": f"HTTP {error.code}"}]}
    try:
        found = call("/user/tokens/verify")
        if found.get("success") and (found.get("result") or {}).get("status") == "active":
            return ""
        if (found.get("result") or {}).get("status") not in (None, "active"):
            return f"Cloudflare says the token is {found['result']['status']}"
        zones = call("/zones?per_page=1")
        if zones.get("success"):
            return ""
        errors = zones.get("errors") or found.get("errors") or []
        return "Cloudflare refused the token: " + ("; ".join(str(e.get("message")) for e in errors) or "no reason given")
    except (OSError, ValueError) as error:
        return f"could not reach Cloudflare to check the token ({type(error).__name__}); try again"


def _apply_dns(target: Target, value: str, by: str) -> str:
    """The token the domain's DNS-01 challenge uses, as the domain step stores it (vault)."""
    from . import vault
    if why := verify_cloudflare(value):
        return _scrub(why, value)
    vault.put(vault.CLOUDFLARE_API_TOKEN, value, by=by)
    return ""


def _apply_connector(target: Target, value: str, by: str) -> str:
    """The connector's own connect with its other settings as they are, so
    only the secret changes (as saving its card with them unchanged)."""
    from . import connections
    connector = _connector(target.name)
    values = {f.name: f.value for f in connector.status().fields if not f.secret and f.value is not None}
    try:
        connector.connect({**values, target.key: value}, by=by)
    except connections.Refused as error:
        failed = [f"{c.name}: {c.detail}" for c in error.checks if not c.ok]
        return _scrub("; ".join([str(error), *failed]), value)
    return ""


def _apply_repo(target: Target, value: str, by: str) -> str:
    """A repo's secret value, as `sbx env set` stores it (its audience and
    agent kept when it is set already; always a secret, even over a plain
    variable), delivered to its sandboxes."""
    from . import sandboxes, settings, variables
    if settings.trust(target.name) != "trusted":
        return f"{target.name} is not a trusted repo here any more"
    before = next((e for e in variables.entries(target.name) if e["name"] == target.key and not e["mine"]), None)
    audience = before["audience"] if before else "all"
    try:
        variables.put(target.name, target.key, value, by, secret=True,
                      audience=audience, agent=before["agent"] if before else True)
    except ValueError as error:
        return str(error)
    try:
        sandboxes.deliver_env(target.name, [target.key], {audience})
    except Exception as error:   # stored; its delivery is the keeper's to retry
        print(f"secret request: {target} stored, delivery failed: {type(error).__name__}", flush=True)
    return ""
