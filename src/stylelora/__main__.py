"""Single-process local service entry point."""
import argparse
import asyncio
import os
from pathlib import Path
import sys
import webbrowser
import uvicorn
from stylelora.api import create_app
from stylelora.runtime import Runtime
from stylelora.runtime.instance import InstanceLock, bind_socket
from stylelora.runtime.contracts import Failure
from stylelora.llm.secrets import SecretStore


def main(argv=None):
    parser = argparse.ArgumentParser(prog='stylelora')
    sub = parser.add_subparsers(dest='command', required=True)
    serve = sub.add_parser('serve')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=0)
    serve.add_argument('--workers', type=int, default=1)
    serve.add_argument('--reload', action='store_true')
    serve.add_argument('--project', type=Path)
    serve.add_argument('--no-browser', action='store_true')
    serve.add_argument('--dev-origin')
    args = parser.parse_args(argv)
    lock = sock = None
    try:
        sock = bind_socket(host=args.host, port=args.port, workers=args.workers, reload=args.reload)
        lock = InstanceLock()
        rt = Runtime(Path(os.environ['APPDATA'])/'StyleLoraAutom',
                     authority=f'127.0.0.1:{sock.getsockname()[1]}', dev_origin=args.dev_origin,
                     secret_store=SecretStore())
        app = create_app(rt)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=sock.getsockname()[1],
                                workers=1, proxy_headers=False, access_log=False))
        rt.shutdown_callback = lambda: setattr(server, 'should_exit', True)
        async def run():
            try:
                if args.project:
                    await rt.open(args.project.absolute())
                task = asyncio.create_task(server.serve(sockets=[sock]))
                while not server.started and not task.done():
                    await asyncio.sleep(.01)
                if server.started and not args.no_browser and args.dev_origin:
                    code = rt.bootstrap_code(args.dev_origin)
                    webbrowser.open(args.dev_origin+'/#bootstrap='+code)
                await task
            finally:
                await rt.close()
        asyncio.run(run())
        return 0
    except Failure as exc:
        print(exc.code, file=sys.stderr)
        return 4 if exc.status == 423 else 2
    except (OSError, KeyError):
        print('dependency:unavailable', file=sys.stderr)
        return 5
    finally:
        if sock:
            sock.close()
        if lock:
            lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
