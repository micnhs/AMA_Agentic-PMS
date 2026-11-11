"""The local start falls back to the next free port when the default port is busy (README quick start)."""
from __future__ import annotations

import socket
import unittest

from pms.web import app as web_app


class _NoApp:
    """Stand-in WSGI app: the test only binds the server, it never serves a request."""

    def __call__(self, environ, start_response):  # pragma: no cover - never called
        start_response("204 No Content", [])
        return [b""]


class ServePortFallbackTest(unittest.TestCase):
    def _busy_port(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return sock

    def test_auto_port_uses_next_free_port(self) -> None:
        busy = self._busy_port()
        try:
            port = busy.getsockname()[1]
            server = web_app._bind("127.0.0.1", port, _NoApp(), auto_port=True)
            try:
                self.assertNotEqual(server.server_address[1], port)
                self.assertGreater(server.server_address[1], port)
            finally:
                server.server_close()
        finally:
            busy.close()

    def test_explicit_port_does_not_fall_back(self) -> None:
        busy = self._busy_port()
        try:
            with self.assertRaises(OSError):
                web_app._bind("127.0.0.1", busy.getsockname()[1], _NoApp(), auto_port=False)
        finally:
            busy.close()


if __name__ == "__main__":
    unittest.main()
