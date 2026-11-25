"""Local-only guarantees for the AI client (canary tests: local sockets only)."""
import http.server
import json
import socket
import threading
import unittest
import urllib.request
from unittest import mock
from urllib import error

from app import ai_assistant as ai


class Canary:
    """Local TCP listener that records any bytes sent to it."""

    def __init__(self):
        self.s = socket.socket()
        self.s.bind(("127.0.0.1", 0))
        self.s.listen(8)
        self.s.settimeout(0.2)
        self.port = self.s.getsockname()[1]
        self.hits = []
        self.run = True
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        while self.run:
            try:
                c, _ = self.s.accept()
                c.settimeout(0.5)
                try:
                    self.hits.append(c.recv(4096))
                except OSError:
                    self.hits.append(b"")
                c.close()
            except socket.timeout:
                continue
            except OSError:
                break

    def close(self):
        self.run = False
        self.t.join(1)
        self.s.close()


def fake_ollama(mode, canary_port=0):
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, obj):
            b = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            seen.append(("GET", b""))
            if mode == "redirect":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{canary_port}/x")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._reply({"models": [{"name": ai.OLLAMA_MODEL}]})

        def do_POST(self):
            seen.append(("POST", self.rfile.read(int(self.headers["Content-Length"]))))
            if mode == "redirect":
                self.send_response(307)
                self.send_header("Location", f"http://127.0.0.1:{canary_port}/x")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._reply({"message": {"content": "ok"}})

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


class LocalOnlyTests(unittest.TestCase):
    def setUp(self):
        # urllib caches its default opener (and the proxies read at that time);
        # reset so each test sees its own environment.
        urllib.request._opener = None
        self.addCleanup(setattr, urllib.request, "_opener", None)
        self.canary = Canary()
        self.addCleanup(self.canary.close)
        self.old_url = ai.OLLAMA_BASE_URL
        self.addCleanup(setattr, ai, "OLLAMA_BASE_URL", self.old_url)

    def _serve(self, mode="ok"):
        srv, seen = fake_ollama(mode, self.canary.port)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        ai.OLLAMA_BASE_URL = f"http://127.0.0.1:{srv.server_port}"
        return seen

    def _proxy_env(self):
        env = {k: f"http://127.0.0.1:{self.canary.port}"
               for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY")}
        env.update({"NO_PROXY": "", "no_proxy": ""})
        p = mock.patch.dict("os.environ", env)
        p.start()
        self.addCleanup(p.stop)

    def test_proxy_env_not_used_for_post(self):
        self._proxy_env()
        seen = self._serve("ok")
        out = ai._post_json("/api/chat", {"model": "m", "messages": [{"role": "user", "content": "SECRET-123"}]})
        self.assertEqual(out["message"]["content"], "ok")
        self.assertTrue(seen)
        self.assertEqual(self.canary.hits, [])

    def test_proxy_env_not_used_for_status(self):
        self._proxy_env()
        self._serve("ok")
        st = ai.local_model_status()
        self.assertTrue(st["server"])
        self.assertEqual(self.canary.hits, [])

    def test_redirect_not_followed_post(self):
        self._serve("redirect")
        with self.assertRaises((error.HTTPError, error.URLError)):
            ai._post_json("/api/chat", {"model": "m", "x": "SECRET-123"})
        self.assertEqual(self.canary.hits, [])

    def test_redirect_not_followed_status(self):
        self._serve("redirect")
        st = ai.local_model_status()
        self.assertFalse(st["server"])
        self.assertEqual(self.canary.hits, [])

    def test_non_loopback_refused_before_any_socket(self):
        urls = ["http://10.255.255.1:11434", "file://localhost/x", "ftp://127.0.0.1/",
                "http://127.0.0.1@evil.example/", "http://127.1:11434", "http://0x7f.1:11434",
                "http://2130706433:11434", "http://[::ffff:127.0.0.1]:11434", "http://localhost.evil.example:11434"]
        boom = mock.Mock(side_effect=AssertionError("socket used"))
        for url in urls:
            with self.subTest(url=url):
                ai.OLLAMA_BASE_URL = url
                with mock.patch.object(socket, "create_connection", boom):
                    with self.assertRaises(RuntimeError):
                        ai._post_json("/api/chat", {})
                    self.assertFalse(ai.local_model_status()["server"])
        boom.assert_not_called()

    def test_localhost_resolving_off_machine_refused(self):
        ai.OLLAMA_BASE_URL = "http://localhost:11434"
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 11434))]
        boom = mock.Mock(side_effect=AssertionError("socket used"))
        with mock.patch.object(socket, "getaddrinfo", return_value=fake), \
                mock.patch.object(socket, "create_connection", boom):
            with self.assertRaises(RuntimeError):
                ai._post_json("/api/chat", {})
        boom.assert_not_called()

    def test_ipv6_loopback_literal_allowed_by_check(self):
        ai.OLLAMA_BASE_URL = "http://[::1]:11434"
        ai._ensure_loopback()


if __name__ == "__main__":
    unittest.main()
