"""HTTP keep-alive: one connection carries many requests, and anything that could confuse request boundaries ends it."""

from __future__ import annotations

import http.client
import socket
import unittest

from games import GameRegistry
from server import MAX_DRAIN, Handler
from test_coop import RECIPES, call, load_recipes, serve


def read_response(sock: socket.socket) -> tuple[str, bytes]:
    """The head and body of one response on a raw socket (Content-Length framed), without closing it."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1])
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return head.decode("latin-1"), body


def is_closed_by_server(sock: socket.socket, wait: float = 2.0) -> bool:
    """True if the server closes this connection within `wait` seconds (a reset counts as closed)."""
    sock.settimeout(wait)
    try:
        return sock.recv(1) == b""
    except ConnectionError:
        return True
    except (TimeoutError, socket.timeout):
        return False


class KeepAliveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.port = serve(GameRegistry(load_recipes(RECIPES), max_games=5))
        cls.accepted = 0
        original = cls.server.get_request

        def counting():  # type: ignore[no-untyped-def]
            cls.accepted += 1
            return original()

        cls.server.get_request = counting  # type: ignore[method-assign]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def raw(self) -> socket.socket:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(sock.close)
        return sock

    def test_many_requests_share_one_connection(self) -> None:
        status, game = call(self.port, "POST", "/games", {"name": "kalive"})
        self.assertEqual(status, 200)
        status, joined = call(self.port, "POST", "/g/kalive/join", {})
        self.assertEqual(status, 200)
        before = self.accepted
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        import json
        for n in range(40):
            conn.request("POST", "/g/kalive/cursor", json.dumps({"player": joined["token"], "x": n / 40, "y": 0.5}),
                         {"Content-Type": "application/json"})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 200)
            self.assertNotEqual((resp.getheader("Connection") or "").lower(), "close")
            conn.request("GET", "/auth-config")
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 200)
        self.assertEqual(self.accepted - before, 1, "all 80 requests should have used a single connection")

    def test_a_small_unread_post_body_does_not_corrupt_the_next_request(self) -> None:
        sneaky = b"GET /laptop HTTP/1.1\r\nHost: x\r\n\r\n"  # a body that looks like a request
        before = self.accepted
        sock = self.raw()
        sock.sendall(b"POST /g/nope/elsewhere HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                     b"Content-Length: %d\r\n\r\n" % len(sneaky) + sneaky)
        head, _ = read_response(sock)
        self.assertTrue(head.startswith("HTTP/1.1 404"), head)
        self.assertNotIn("connection: close", head.lower())  # small unread bodies are discarded, not fatal
        sock.sendall(b"GET /auth-config HTTP/1.1\r\nHost: x\r\n\r\n")
        head, body = read_response(sock)
        self.assertTrue(head.startswith("HTTP/1.1 200"), head)  # one clean answer: the hidden GET was never run
        self.assertIn(b"invite_required", body)
        sock.settimeout(0.3)
        with self.assertRaises(TimeoutError):  # and nothing else is waiting to be sent (no stray second response)
            sock.recv(1)
        self.assertEqual(self.accepted - before, 1)

    def test_an_oversized_body_ends_the_connection(self) -> None:
        sock = self.raw()
        sock.sendall(b"POST /login HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                     b"Content-Length: %d\r\n\r\n" % (MAX_DRAIN + 1) + b"{}")
        head, _ = read_response(sock)
        self.assertTrue(head.startswith("HTTP/1.1 400"), head)
        self.assertIn("connection: close", head.lower())
        self.assertTrue(is_closed_by_server(sock))

    def test_chunked_and_nonsense_framing_end_the_connection(self) -> None:
        for request in (b"POST /login HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
                        b"POST /login HTTP/1.1\r\nHost: x\r\nContent-Length: banana\r\n\r\n{}",
                        b"POST /login HTTP/1.1\r\nHost: x\r\nContent-Length: -4\r\n\r\n{}"):
            sock = self.raw()
            sock.sendall(request)
            head, _ = read_response(sock)
            self.assertIn("connection: close", head.lower(), request)
            self.assertTrue(is_closed_by_server(sock), request)

    def test_a_get_with_a_body_ends_the_connection(self) -> None:
        sock = self.raw()
        sock.sendall(b"GET /auth-config HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello")
        head, _ = read_response(sock)
        self.assertTrue(head.startswith("HTTP/1.1 200"), head)
        self.assertIn("connection: close", head.lower())
        self.assertTrue(is_closed_by_server(sock))

    def test_the_client_can_ask_to_close(self) -> None:
        sock = self.raw()
        sock.sendall(b"GET /auth-config HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        head, _ = read_response(sock)
        self.assertIn("connection: close", head.lower())
        self.assertTrue(is_closed_by_server(sock))

    def test_an_http_1_0_client_still_works(self) -> None:
        sock = self.raw()
        sock.sendall(b"GET /auth-config HTTP/1.0\r\n\r\n")
        head, body = read_response(sock)
        self.assertIn(" 200 ", head.splitlines()[0])
        self.assertIn(b"invite_required", body)
        self.assertTrue(is_closed_by_server(sock))

    def test_the_event_stream_announces_that_it_will_close(self) -> None:
        status, _ = call(self.port, "POST", "/games", {"name": "streamy"})
        status, joined = call(self.port, "POST", "/g/streamy/join", {})
        status, data = call(self.port, "POST", "/g/streamy/ticket", {"player": joined["token"]})
        sock = self.raw()
        sock.sendall(f"GET /g/streamy/events?ticket={data['ticket']} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        sock.settimeout(3)
        head = b""
        while b"\r\n\r\n" not in head:
            head += sock.recv(4096)
        text = head.decode("latin-1").lower()
        self.assertIn("text/event-stream", text)
        self.assertIn("connection: close", text)
        self.assertNotIn("content-length", text)

    def test_an_idle_connection_is_closed_after_the_timeout(self) -> None:
        original = Handler.timeout
        Handler.timeout = 0.4
        self.addCleanup(setattr, Handler, "timeout", original)
        sock = self.raw()
        sock.sendall(b"GET /auth-config HTTP/1.1\r\nHost: x\r\n\r\n")
        read_response(sock)
        self.assertTrue(is_closed_by_server(sock, wait=3.0), "the server should drop a connection nobody is using")

    def test_a_slow_request_is_still_dropped(self) -> None:
        original = Handler.timeout
        Handler.timeout = 0.4
        self.addCleanup(setattr, Handler, "timeout", original)
        sock = self.raw()
        sock.sendall(b"POST /login HTTP/1.1\r\nHost: x\r\nContent-Length: 50\r\n\r\n{")  # promises 50 bytes, sends 1
        self.assertTrue(is_closed_by_server(sock, wait=3.0))


if __name__ == "__main__":
    unittest.main()
