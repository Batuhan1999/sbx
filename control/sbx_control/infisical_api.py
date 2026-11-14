"""Bringing sandboxes what changed in the secret store outside sbx (the
store's watch: Infisical's UI): its signed webhook, and a poll on a thread
of the server's for when a webhook is missed or cannot be set up."""
import hashlib
import hmac
import json
import re
import secrets
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from . import platform, sandboxes, secret_store, settings, variables, vault, views

POLL_SECONDS = 60

router = APIRouter()
_syncing = threading.Lock()


async def store_failed(request: Request, error: secret_store.StoreError):
    """Exception handler: the secret store failed, not this request."""
    return JSONResponse({"detail": f"the secret store: {error}"}, status_code=502)


def _watch():
    store = secret_store.store()
    return store, store.watch


def sync():
    """Bring every repo's sandboxes what changed in the store since the last
    look (the webhook, and the poll)."""
    store, watch = _watch()
    if watch is None or not _syncing.acquire(blocking=False):
        return
    try:
        _sync(store, watch)
    except Exception as error:
        print(f"secret store synchronization deferred: {type(error).__name__}", flush=True)
    finally:
        _syncing.release()


def _changed(watch, namespace: str, deliver):
    names, snapshot = watch.changes(namespace)
    if names:
        deliver(names)
    watch.acknowledge(namespace, snapshot)


def _sync(store, watch):
    store.clear_cache()
    base = settings.current().base_url
    if base.startswith("https://"):
        try:
            key = vault.get(vault.INFISICAL_WEBHOOK_TOKEN)
            if not key:
                key = secrets.token_urlsafe(32)
                vault.put(vault.INFISICAL_WEBHOOK_TOKEN, key, by="webhook setup")
            store.ensure_webhook(base, key)
        except secret_store.StoreError:
            print("the store's webhook is not set up yet; polling meanwhile", flush=True)
    trusted = [repo for repo in views.known_repos() if settings.trust(repo) == "trusted"]

    def everywhere(names):
        for repo in trusted:
            sandboxes.deliver_env(repo, names)

    def objects(names):
        platform.connect()  # the platform's own secrets may be among them
        everywhere([])
    _changed(watch, secret_store.OBJECTS, objects)
    _changed(watch, variables.scope_of(None), everywhere)
    for repo in trusted:
        try:
            _changed(watch, variables.scope_of(repo), lambda names, repo=repo: sandboxes.deliver_env(repo, names))
            for person in variables.people(repo):
                _changed(watch, variables.scope_of(repo, person),
                         lambda names, repo=repo, person=person: sandboxes.deliver_env(repo, names, person=person))
        except secret_store.StoreError as error:
            print(f"secret store: {repo}: {error}", flush=True)


def poll(stop: threading.Event):
    while not stop.wait(POLL_SECONDS):
        try:
            watched = _watch()[1] is not None
        except Exception:
            watched = False
        if watched:
            threading.Thread(target=sync, daemon=True).start()


@router.post("/webhooks/infisical")
async def infisical_webhook(request: Request):
    """Infisical's webhook, signed with INFISICAL_WEBHOOK_TOKEN: what changed
    is looked up, never taken from the request."""
    expected = vault.get(vault.INFISICAL_WEBHOOK_TOKEN) or ''
    raw = await request.body()
    signature = re.fullmatch(r't=(\d+);([a-f0-9]{64})', request.headers.get('x-infisical-signature', ''))
    if not expected or not signature or len(raw) > 65536:
        raise HTTPException(404)
    stamp = int(signature[1])
    if abs(time.time() - stamp / 1000) > 300 or not hmac.compare_digest(
            hmac.new(expected.encode(), raw, hashlib.sha256).hexdigest(), signature[2]):
        raise HTTPException(404)
    try:
        payload = json.loads(raw)
        if payload.get('timestamp') != stamp:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(404)
    threading.Thread(target=sync, daemon=True).start()
    return {'ok': True}
