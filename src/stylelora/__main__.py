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
from stylelora.runtime.instance import InstanceLock, bind_socket, publish_instance, discovery_path, attach_url
from stylelora.runtime.contracts import Failure
from stylelora.llm.secrets import SecretStore


def _quiet_client_resets(loop):
    """Suppress the Windows Proactor traceback logged when a browser drops a
    keep-alive/SSE connection (WinError 10054). Other loop errors still log."""
    default = loop.get_exception_handler()

    def handler(lp, context):
        exc = context.get('exception')
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
            return
        if default is not None:
            default(lp, context)
        else:
            lp.default_exception_handler(context)
    loop.set_exception_handler(handler)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='stylelora')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('ui')
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
        if args.command == 'ui':
            webbrowser.open(attach_url())
            return 0
        sock = bind_socket(host=args.host, port=args.port, workers=args.workers, reload=args.reload)
        lock = InstanceLock()
        rt = Runtime(Path(os.environ['APPDATA'])/'StyleLoraAutom',
                     authority=f'127.0.0.1:{sock.getsockname()[1]}', dev_origin=args.dev_origin,
                     secret_store=SecretStore())
        app = create_app(rt)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=sock.getsockname()[1],
                                workers=1, proxy_headers=False, access_log=False,
                                use_colors=False))
        rt.shutdown_callback = lambda: setattr(server, 'should_exit', True)
        async def run():
            _quiet_client_resets(asyncio.get_running_loop())
            try:
                if args.project:
                    await rt.open(args.project.absolute())
                task = asyncio.create_task(server.serve(sockets=[sock]))
                while not server.started and not task.done():
                    await asyncio.sleep(.01)
                if server.started:
                    publish_instance(rt)
                    if not args.no_browser:
                        origin = args.dev_origin or 'http://' + rt.authority
                        webbrowser.open(origin+'/#bootstrap='+rt.bootstrap_code(origin))
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
    except ValueError:
        print('instance:invalid', file=sys.stderr)
        return 2
    finally:
        if sock:
            sock.close()
        if lock:
            discovery_path().unlink(missing_ok=True)
            lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
