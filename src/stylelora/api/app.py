from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from stylelora.runtime.contracts import Failure
from .errors import response
from .security import protect


def create_app(runtime):
    @asynccontextmanager
    async def lifespan(app):
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

    from .routes import session, images, resources, exchange, product, prompts, unavailable
    app.include_router(session.router)
    app.include_router(images.router)
    app.include_router(resources.router)
    app.include_router(exchange.router)
    app.include_router(product.router)
    app.include_router(prompts.router)
    import re
    normalize = lambda path: re.sub(r'\{[^}]+\}', '{}', path)
    implemented = {(normalize(r.path), m) for r in app.routes for m in getattr(r, 'methods', ())}
    for route in unavailable.router.routes:
        if not any((normalize(route.path), m) in implemented for m in route.methods):
            app.router.routes.append(route)
    # Static resources (stats, batches, current, imports) must precede ID routes.
    app.router.routes.sort(key=lambda r: (getattr(r, 'path', '').count('{'), -len(getattr(r, 'path', ''))))
    return app
