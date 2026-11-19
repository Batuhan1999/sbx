"""/api/servers (admins): the managed servers this host runs for sandboxes
(hosted_services.py): Postgres, ClickHouse and every server a connection
package declares. Starting one answers once it is recorded and runs it
behind the answer (pulling images takes minutes); the web app and
`sbx servers` follow GET until it is ready or failed. Removing one stops
it and, unless its data is kept, leaves nothing of it on the host."""
import threading

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, SecretStr

from . import auth, catalog, hosted_services
from .catalog import PackageError

router = APIRouter()


class Started(BaseModel):
    name: str
    package: str                        # the connection package whose server to run
    storage_gb: int | None = None       # a new Postgres: its volume's size
    secrets: dict[str, SecretStr] = {}  # a new package server: values for secrets it would otherwise make


def _listing() -> dict:
    return {"servers": hosted_services.records(),
            "packages": [{"name": p.name, "title": p.title} for p in catalog.listing("connection")
                         if hosted_services.runs(p)]}


@router.get("/api/servers")
def api_servers(email: str = Depends(auth.admin_user)):
    return _listing()


def _finish(name: str):
    try:
        hosted_services.finish(name)
    except Exception as error:   # kept with the server (its record's error); here for the log
        print(f"managed server {name}: {error}", flush=True)


@router.post("/api/servers", status_code=202)
async def api_server_start(body: Started, email: str = Depends(auth.admin_user)):
    """Run a server, or start again one that is stopped or failed (the same name and package)."""
    presets = {key: value.get_secret_value() for key, value in body.secrets.items()} or None
    try:
        record = await run_in_threadpool(hosted_services.begin, body.name, package=body.package,
                                         storage_gb=body.storage_gb, presets=presets)
    except (ValueError, PackageError) as error:
        raise HTTPException(400, str(error)) from None
    except RuntimeError as error:   # docker refused before anything ran
        raise HTTPException(503, str(error)) from None
    threading.Thread(target=_finish, args=(body.name,), name=f"server-{body.name}", daemon=True).start()
    return record


@router.delete("/api/servers/{name}")
async def api_server_remove(name: str, keep_data: bool = False, email: str = Depends(auth.admin_user)):
    """Stop the server and remove its connection; its data and its record
    too unless keep_data. 409 while a repository uses it."""
    try:
        return await run_in_threadpool(hosted_services.down, name, delete_data=not keep_data)
    except PackageError as error:
        raise HTTPException(409, str(error)) from None
    except ValueError as error:
        raise HTTPException(404 if "no managed server" in str(error) else 409, str(error)) from None
    except RuntimeError as error:
        raise HTTPException(503, str(error)) from None
