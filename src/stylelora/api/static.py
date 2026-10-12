"""Same-origin SPA hosting; API misses never fall through to HTML."""
from pathlib import Path
from starlette.responses import FileResponse
from fastapi import Request


def mount_frontend(app, directory=None):
    directory = Path(directory) if directory else Path(__file__).resolve().parents[3] / 'frontend' / 'dist'
    if (directory / 'index.html').is_file():
        async def frontend(request: Request, path: str = ''):
            if request.url.path.startswith('/api'):
                from stylelora.runtime.contracts import Failure
                raise Failure(404, 'resource:not_found')
            target = directory / path
            if target.is_file() and target.resolve().is_relative_to(directory.resolve()):
                return FileResponse(target)
            return FileResponse(directory / 'index.html', headers={'Cache-Control': 'no-store'})
        app.add_api_route('/{path:path}', frontend, methods=['GET', 'HEAD'], name='frontend')
