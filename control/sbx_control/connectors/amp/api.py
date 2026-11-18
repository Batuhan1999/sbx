"""The Amp provider's job endpoint (orbs.handle_job): only from this host."""
import json
import pathlib
import traceback

from fastapi import APIRouter, HTTPException, Request

from ... import auth, state
from . import orbs

router = APIRouter()


@router.post("/internal/amp/jobs")
def amp_job(body: dict, request: Request):
    auth.require_loopback(request)
    try:
        return orbs.handle_job(body)
    except orbs.JobError as error:
        raise HTTPException(400, str(error)) from None
    except Exception as error:
        # Bootstrap tokens and application credentials must not reach the
        # provider log, including through SDK/HTTP exception messages.
        causes = []
        cause = error
        while cause is not None and len(causes) < 4:
            frames = traceback.extract_tb(cause.__traceback__)
            causes.append({"type": type(cause).__name__, "frames": [
                f"{pathlib.Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in frames]})
            cause = cause.__cause__ or cause.__context__
        state.event(orbs.sandbox_name(str(body.get("ORB_ID", "unknown"))), "Amp job failed", json.dumps(causes))
        detail = "Amp job failed; inspect the sandbox lifecycle and app logs in sbx"
        headers = None
        if isinstance(error, orbs.SpawnNotCleanedUp):
            detail += "; " + str(error)
            headers = {orbs.ERROR_CODE: orbs.SpawnNotCleanedUp.code}
        raise HTTPException(503, detail, headers=headers) from None
