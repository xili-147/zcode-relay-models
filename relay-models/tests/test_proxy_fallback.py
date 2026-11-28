import json
import socket
import sys
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import relay_cli


class CatalogHandler(BaseHTTPRequestHandler):
    requests = []
    status = 200

    def do_GET(self):
        type(self).requests.append((self.path, self.headers.get("Authorization")))
        payload = json.dumps({"data": [{"id": "available-model"}]}).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


class ProxyFallbackTests(unittest.TestCase):
    def setUp(self):
        CatalogHandler.requests = []
        CatalogHandler.status = 200
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CatalogHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/models"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_failed_proxy_retries_same_catalog_directly_with_credentials(self):
        with socket.socket() as unavailable_proxy:
            unavailable_proxy.bind(("127.0.0.1", 0))
            proxy_url = f"http://127.0.0.1:{unavailable_proxy.getsockname()[1]}"
            with patch.object(relay_cli.urllib.request, "getproxies",
                              return_value={"http": proxy_url}), patch.object(
                    relay_cli.urllib.request, "proxy_bypass", return_value=False), patch.object(
                    relay_cli.urllib.request, "_opener", None):
                result = relay_cli.http_json(
                    self.url, {"Authorization": "Bearer fixture-key"}, 1)
        self.assertEqual(result, {"data": [{"id": "available-model"}]})
        self.assertEqual(CatalogHandler.requests, [("/v1/models", "Bearer fixture-key")])

    def test_proxy_handshake_timeout_retries_catalog_directly(self):
        with patch.object(relay_cli.urllib.request, "urlopen", side_effect=
                          urllib.error.URLError(TimeoutError("handshake timed out"))), patch.object(
                relay_cli.urllib.request, "getproxies", return_value={"http": "http://proxy.invalid"}), patch.object(
                relay_cli.urllib.request, "proxy_bypass", return_value=False):
            result = relay_cli.http_json(self.url, {"Authorization": "Bearer fixture-key"}, 1)
        self.assertEqual(result, {"data": [{"id": "available-model"}]})
        self.assertEqual(CatalogHandler.requests, [("/v1/models", "Bearer fixture-key")])

    def test_http_authentication_error_does_not_retry_directly(self):
        CatalogHandler.status = 401
        proxy_url = f"http://127.0.0.1:{self.server.server_port}"
        with patch.object(relay_cli.urllib.request, "getproxies",
                          return_value={"http": proxy_url}), patch.object(
                relay_cli.urllib.request, "proxy_bypass", return_value=False), patch.object(
                relay_cli.urllib.request, "_opener", None):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                relay_cli.http_json(self.url, {"Authorization": "Bearer fixture-key"}, 1)
            self.assertEqual(raised.exception.code, 401)
            raised.exception.close()
        self.assertEqual(CatalogHandler.requests, [(self.url, "Bearer fixture-key")])

    def test_connection_failure_without_proxy_is_not_retried(self):
        with patch.object(relay_cli.urllib.request, "urlopen", side_effect=
                          urllib.error.URLError(TimeoutError("connection timed out"))), patch.object(
                relay_cli.urllib.request, "getproxies", return_value={}):
            with self.assertRaises(urllib.error.URLError):
                relay_cli.http_json(self.url, {}, 1)
        self.assertEqual(CatalogHandler.requests, [])


if __name__ == "__main__":
    unittest.main()
