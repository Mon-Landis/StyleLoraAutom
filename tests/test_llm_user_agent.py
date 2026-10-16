import asyncio
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from stylelora.llm.adapters import USER_AGENT, UrllibTransport, with_default_headers


class _Echo(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        _Echo.seen.append(self.headers.get('User-Agent'))
        body = b'{}'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class UserAgentTests(unittest.TestCase):
    def test_default_added_and_caller_value_kept(self):
        self.assertEqual(with_default_headers({})['User-Agent'], USER_AGENT)
        self.assertEqual(with_default_headers(None)['User-Agent'], USER_AGENT)
        kept = with_default_headers({'user-agent': 'custom/1'})
        self.assertEqual(kept, {'user-agent': 'custom/1'})

    def test_wire_request_never_uses_urllib_default(self):
        server = HTTPServer(('127.0.0.1', 0), _Echo)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            _Echo.seen.clear()
            url = f'http://127.0.0.1:{server.server_port}/v1/chat/completions'
            asyncio.run(UrllibTransport()('POST', url, {'Content-Type': 'application/json'},
                                          b'{}', 10))
            self.assertEqual(_Echo.seen, [USER_AGENT])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
