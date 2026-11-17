"""sbx-control's composition root: `uvicorn sbx_control.app:app`. Nothing
imports it."""
import threading
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import (auth, catalog_api, connections_api, core, dist_api, endpoints_api, env, extensions, gate, infisical_api, keeper,
               lifecycle, platform, recipes, repo_onboarding, repos_api, runtime, runtimes_api, sandboxes_api, secret_store,
               settings_api, setup_api, setup_pr, spa, state, template_builds, updates, users_api)

# In the order they match: a path two routers could answer goes to the first;
# the extensions' come after the platform's API and before the web app's.
ROUTERS = (auth, sandboxes_api, env, infisical_api, gate, dist_api,
           connections_api, catalog_api, setup_api, settings_api, users_api, repos_api, runtimes_api, endpoints_api,
           updates)
LAST = (spa, repo_onboarding, setup_pr)
# Outermost last (Starlette wraps each added middleware around the ones before).
MIDDLEWARES = (setup_api.setup_gate, dist_api.version_header, spa.same_origin_writes)


@asynccontextmanager
async def lifespan(_app):
    state.migrate()
    platform.connect()
    lifecycle.recover_interrupted()
    stop = threading.Event()
    for name, work in (("keeper", keeper.run), ("templates", template_builds.schedule),
                       ("infisical-poll", infisical_api.poll), ("updates", updates.watch)):
        threading.Thread(target=work, args=(stop,), name=name, daemon=True).start()
    extensions.start(stop)
    try:
        yield
    finally:
        stop.set()


async def unexpected(request: Request, error: Exception):
    """Say what failed instead of a bare 500: most often it is the sandbox
    runtime not answering, or answering with an error."""
    if isinstance(error, runtime.NoRuntime):
        return no_runtime(request, error)
    traceback.print_exception(error)
    if core.runtime_failure(error):
        try:
            title = runtime.title()
        except Exception:
            title = "none set up"
        return PlainTextResponse(f"the sandbox runtime ({title}) failed: {type(error).__name__}: {error}",
                                 status_code=503)
    return PlainTextResponse(f"sbx-control failed: {type(error).__name__}: {error}", status_code=500)


def no_runtime(_request, error):
    """The installation enables no runtime yet: what to do, not a 500."""
    return JSONResponse({"detail": str(error)}, status_code=503)


async def lifecycle_conflict(_request, error):
    return JSONResponse({"detail": str(error)}, status_code=409)


async def not_built(_request, error):
    return JSONResponse({"detail": str(error)}, status_code=503, headers={"Retry-After": "60"})


async def no_room(_request, error):
    return JSONResponse({"detail": str(error)}, status_code=503)


async def refused(_request, error):
    if isinstance(error, core.PauseRefused):
        return JSONResponse({"detail": str(error)}, status_code=503, headers={"Retry-After": "60"})
    return JSONResponse({"detail": str(error)}, status_code=503 if isinstance(error, core.NoCapacity) else 400)


def create_app() -> FastAPI:
    app = FastAPI(title="sbx-control", lifespan=lifespan)
    app.add_exception_handler(Exception, unexpected)
    app.add_exception_handler(runtime.NoRuntime, no_runtime)
    app.add_exception_handler(lifecycle.Conflict, lifecycle_conflict)
    app.add_exception_handler(core.SandboxError, refused)
    app.add_exception_handler(template_builds.NotBuilt, not_built)
    app.add_exception_handler(template_builds.NoRoom, no_room)
    app.add_exception_handler(recipes.RecipeError, refused)
    app.add_exception_handler(secret_store.StoreError, infisical_api.store_failed)
    app.add_middleware(auth.Sessions)
    for middleware in MIDDLEWARES:
        app.middleware("http")(middleware)
    for router in (*(m.router for m in ROUTERS), *extensions.routers(), *(m.router for m in LAST)):
        app.include_router(router)
    return app


app = create_app()
