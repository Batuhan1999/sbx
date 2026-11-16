"""/api/runtimes: the sandbox runtimes this installation runs, for the web
app (and its first-run wizard, before anyone can sign in) and `sbx runtimes`."""
from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool

from . import auth, runtimes, setup

router = APIRouter()


def signed_in_or_setup(request: Request) -> str:
    """Anyone signed in, or the holder of the first-run setup's token while it is open."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("setup ") and setup.token_ok(header[6:].strip()):
        return "setup"
    return auth.api_user(request)


@router.get("/api/runtimes")
async def api_runtimes(who: str = Depends(signed_in_or_setup)):
    """{default, runtimes: [{kind, title, default, host, capabilities, limits, status: {state, summary}}]}."""
    return await run_in_threadpool(runtimes.describe)
