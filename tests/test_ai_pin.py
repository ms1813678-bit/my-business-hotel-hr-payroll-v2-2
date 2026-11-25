"""Pinned loopback transport: rebinding, IPv4/IPv6, HTTPS name check, no second DNS. Local sockets only."""
import http.server, json, os, socket, ssl, threading, unittest, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib import error
from app import ai_assistant as ai

CERTS = os.path.join(os.path.dirname(__file__), "..", "certs")
REAL_GAI = socket.getaddrinfo


def serve(family=socket.AF_INET, addr="127.0.0.1", cert=None):
    seen = []
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            seen.append(self.headers["Host"])
            b = json.dumps({"models": [{"name": ai.OLLAMA_MODEL}]}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            seen.append(self.headers["Host"])
            b = json.dumps({"message": {"content": "ok"}}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    class S(http.server.HTTPServer):
        address_family = family
    srv = S((addr, 0), H)
    if cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(f"{CERTS}/{cert}.crt", f"{CERTS}/{cert}.key")
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


class PinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Runtime-only synthetic certificates, no expiring fixture or private key committed.
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        global CERTS
        cls.certdir = tempfile.TemporaryDirectory(prefix="ai-pin-certs-")
        cls.addClassCleanup(cls.certdir.cleanup)
        CERTS = cls.certdir.name
        for label, host in (("good", "localhost"), ("bad", "other.example")):
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
            now = datetime.now(timezone.utc)
            cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                    .public_key(key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=1))
                    .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
                    .sign(key, hashes.SHA256()))
            Path(CERTS, label + ".crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            Path(CERTS, label + ".key").write_bytes(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    def setUp(self):
        self.old = ai.OLLAMA_BASE_URL
        self.addCleanup(setattr, ai, "OLLAMA_BASE_URL", self.old)

    def _srv(self, *a, **k):
        srv, seen = serve(*a, **k)
        self.addCleanup(srv.server_close); self.addCleanup(srv.shutdown)
        return srv, seen

    def test_rebinding_second_resolution_never_used(self):
        srv, seen = self._srv()
        ai.OLLAMA_BASE_URL = f"http://localhost:{srv.server_port}"
        good = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", srv.server_port))]
        evil = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", srv.server_port))]
        gai = mock.Mock(side_effect=[good, evil, evil, evil])
        with mock.patch.object(socket, "getaddrinfo", gai):
            out = ai._post_json("/api/chat", {"m": 1})
        self.assertEqual(out["message"]["content"], "ok")
        self.assertEqual(gai.call_count, 1)
        self.assertEqual(seen, [f"localhost:{srv.server_port}"])  # original Host header kept

    def test_fallback_uses_verified_list_without_new_dns(self):
        srv, seen = self._srv()
        dead = socket.socket(); dead.bind(("127.0.0.2", 0)); dead_port = dead.getsockname()[1]; dead.close()
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", dead_port)),
                 (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", srv.server_port))]
        ai.OLLAMA_BASE_URL = f"http://localhost:{srv.server_port}"
        gai = mock.Mock(return_value=infos)
        with mock.patch.object(socket, "getaddrinfo", gai):
            self.assertEqual(ai._post_json("/api/chat", {})["message"]["content"], "ok")
        self.assertEqual(gai.call_count, 1)

    def test_all_dial_failure_is_oserror_not_second_dns(self):
        dead = socket.socket(); dead.bind(("127.0.0.1", 0)); p = dead.getsockname()[1]; dead.close()
        ai.OLLAMA_BASE_URL = f"http://127.0.0.1:{p}"
        gai = mock.Mock(side_effect=REAL_GAI)
        with mock.patch.object(socket, "getaddrinfo", gai):
            with self.assertRaises(OSError):
                ai._post_json("/api/chat", {})
        self.assertEqual(gai.call_count, 1)

    def test_any_nonloopback_in_set_refuses_before_socket(self):
        ai.OLLAMA_BASE_URL = "http://localhost:11434"
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 11434)),
                 (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 11434))]
        with mock.patch.object(socket, "getaddrinfo", return_value=infos), \
                mock.patch.object(socket, "socket", side_effect=AssertionError("socket used")):
            with self.assertRaises(RuntimeError):
                ai._post_json("/api/chat", {})

    def test_ipv6_real_dial_full_sockaddr(self):
        try:
            srv, seen = self._srv(socket.AF_INET6, "::1")
        except OSError:
            self.skipTest("no IPv6 loopback here")
        ai.OLLAMA_BASE_URL = f"http://[::1]:{srv.server_port}"
        self.assertEqual(ai._post_json("/api/chat", {})["message"]["content"], "ok")
        self.assertEqual(seen, [f"[::1]:{srv.server_port}"])

    def test_ipv6_sockaddr_tuple_preserved(self):
        pinned = None
        ai.OLLAMA_BASE_URL = "http://[::1]:9"
        infos = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 9, 0, 0))]
        with mock.patch.object(socket, "getaddrinfo", return_value=infos):
            pinned = ai._resolve_verified_loopback()
        self.assertEqual(pinned, [(socket.AF_INET6, ("::1", 9, 0, 0))])

    def _tls(self, cert, url_host="localhost"):
        srv, seen = self._srv(cert=cert)
        ai.OLLAMA_BASE_URL = f"https://{url_host}:{srv.server_port}"
        real = ssl.create_default_context
        def ctx(*a, **k):
            return real(cafile=f"{CERTS}/{cert}.crt")
        return seen, mock.patch.object(ssl, "create_default_context", ctx)

    def test_https_hostname_verified_against_original_host(self):
        seen, p = self._tls("good")
        with p:
            self.assertEqual(ai._post_json("/api/chat", {})["message"]["content"], "ok")
        self.assertEqual(len(seen), 1)

    def test_https_wrong_certificate_name_refused_no_fallback(self):
        seen, p = self._tls("bad")  # trusted CA, but cert is for other.example
        with p:
            with self.assertRaises(error.URLError) as cm:
                ai._post_json("/api/chat", {})
            self.assertIsInstance(cm.exception.reason, ssl.SSLCertVerificationError)
        self.assertEqual(seen, [])

    def test_https_default_context_is_strict(self):
        captured = {}
        orig = ai._PinnedHTTPSConnection.connect
        def spy(self):
            captured["m"] = self._context.verify_mode; captured["h"] = self._context.check_hostname
            raise OSError("stop")
        srv, _ = self._srv(cert="good")
        ai.OLLAMA_BASE_URL = f"https://localhost:{srv.server_port}"
        with mock.patch.object(ai._PinnedHTTPSConnection, "connect", spy):
            with self.assertRaises(OSError):
                ai._post_json("/api/chat", {})
        self.assertEqual(captured, {"m": ssl.CERT_REQUIRED, "h": True})

    def test_default_ports_match_actual_http_https_connection(self):
        for scheme, port in (("http", 80), ("https", 443)):
            ai.OLLAMA_BASE_URL = scheme + "://localhost"
            def resolve(host, actual_port, **kwargs):
                self.assertEqual(actual_port, port)
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", actual_port))]
            with mock.patch.object(socket, "getaddrinfo", side_effect=resolve):
                self.assertEqual(ai._resolve_verified_loopback(), [(socket.AF_INET, ("127.0.0.1", port))])

    def test_dial_closes_socket_when_setup_or_connect_fails(self):
        for method, exc in (("settimeout", ValueError("synthetic")),
                            ("setsockopt", OSError("synthetic")),
                            ("connect", OSError("synthetic"))):
            sock = mock.Mock()
            getattr(sock, method).side_effect = exc
            with mock.patch.object(socket, "socket", return_value=sock):
                with self.assertRaises(type(exc)):
                    ai._dial([(socket.AF_INET, ("127.0.0.1", 9))], .5)
            sock.close.assert_called_once()

    def test_default_timeout_sentinel_not_passed_to_settimeout(self):
        sock = mock.Mock()
        with mock.patch.object(socket, "socket", return_value=sock):
            self.assertIs(ai._dial([(socket.AF_INET, ("127.0.0.1", 9))], socket._GLOBAL_DEFAULT_TIMEOUT), sock)
        sock.settimeout.assert_not_called()

    def test_socket_creation_failure_falls_back_to_next_verified_address(self):
        sock = mock.Mock()
        with mock.patch.object(socket, "socket", side_effect=[OSError("IPv6 disabled"), sock]) as create:
            result = ai._dial([(socket.AF_INET6, ("::1", 9, 0, 0)),
                               (socket.AF_INET, ("127.0.0.1", 9))], .5)
        self.assertIs(result, sock)
        self.assertEqual(create.call_count, 2)
        sock.connect.assert_called_once_with(("127.0.0.1", 9))

    def test_status_path_uses_pin_too(self):
        srv, seen = self._srv()
        ai.OLLAMA_BASE_URL = f"http://localhost:{srv.server_port}"
        good = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", srv.server_port))]
        evil = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", srv.server_port))]
        gai = mock.Mock(side_effect=[good, evil, evil])
        with mock.patch.object(socket, "getaddrinfo", gai):
            st = ai.local_model_status()
        self.assertEqual(gai.call_count, 1)
        self.assertTrue(st["available"])
        self.assertTrue(st["server"])
        self.assertEqual(seen, [f"localhost:{srv.server_port}"])

if __name__ == "__main__":
    unittest.main()
