from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from stylelora.runtime.contracts import Failure
from .errors import response
from .security import protect


def create_app(runtime, *, frontend_dist=None):
    @asynccontextmanager
    async def lifespan(app):
        await runtime.start_task_monitor()
        yield
        await runtime.close()
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runtime = runtime
    app.middleware('http')(protect)

    @app.exception_handler(Exception)
    async def error(request: Request, exc):
        return response(exc, request.state.request_id)

    @app.exception_handler(Failure)
    async def failure(request: Request, exc):
        return response(exc, request.state.request_id)

    @app.exception_handler(RequestValidationError)
    async def validation(request: Request, exc):
        return response(Failure(422, 'input:invalid'), request.state.request_id)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc):
        return response(Failure(exc.status_code, 'resource:not_found' if exc.status_code == 404 else 'input:invalid'), request.state.request_id)

    from .routes import session, images, resources, exchange, product, prompts, unavailable, storage, catalog_extra, local
    app.include_router(session.router)
    app.include_router(images.router)
    app.include_router(resources.router)
    app.include_router(exchange.router)
    app.include_router(product.router)
    app.include_router(prompts.router)
    from .routes import prompt_groups, tools
    app.include_router(tools.router)
    app.include_router(prompt_groups.router)
    app.include_router(storage.router)
    app.include_router(catalog_extra.router)
    app.include_router(local.router)
    from .routes import tasks, resource_settings
    app.include_router(tasks.router)
    app.include_router(resource_settings.router)
    import re
    normalize = lambda path: re.sub(r'\{[^}]+\}', '{}', path)
    implemented = {(normalize(r.path), m) for r in app.routes if getattr(r, 'path', '').startswith('/api')
                   for m in getattr(r, 'methods', ())}
    for route in unavailable.router.routes:
        if not any((normalize(route.path), m) in implemented for m in route.methods):
            app.router.routes.append(route)
    # Static resources (stats, batches, current, imports) must precede ID routes.
    app.router.routes.sort(key=lambda r: (getattr(r, 'path', '').count('{'), -len(getattr(r, 'path', ''))))
    from .static import mount_frontend
    mount_frontend(app, frontend_dist)
    return app
