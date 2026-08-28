from __future__ import annotations

import hashlib
import io
import json
import base64
import socket
import tarfile
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from app.fnos_server import (
    AppState,
    FnosServerError,
    UpdateBusyError,
    create_server,
    download_release_asset,
    dynamic_config_javascript,
    fetch_subscription,
    join_target_url,
    normalize_version,
    safe_extract_ui,
    stable_core_upgrade_query,
    SubscriptionContentError,
    SubscriptionURLRejected,
    validate_core_url,
    validate_subscription_payload,
)


class QuietHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args) -> None:
        pass


class FakeCoreHandler(QuietHandler):
    def _json_response(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _reply(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = {
            "method": self.command,
            "path": self.path,
            "authorization": self.headers.get("Authorization", ""),
            "body": self.rfile.read(length).decode("utf-8"),
        }
        self._json_response(payload)

    @staticmethod
    def _websocket_frame(payload: dict) -> bytes:
        body = json.dumps(payload, separators=(",", ":")).encode()
        if len(body) < 126:
            return bytes((0x81, len(body))) + body
        return bytes((0x81, 126)) + len(body).to_bytes(2, "big") + body

    def _websocket(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Connection", "Upgrade")
        self.send_header("Upgrade", "websocket")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        if "raw_echo=1" in self.path:
            payload = self.connection.recv(4)
            self.connection.sendall(payload)
            self.close_connection = True
            return
        path = self.path.split("?", 1)[0]
        payloads = {
            "/connections": {"connections": [], "downloadTotal": 0, "uploadTotal": 0},
            "/traffic": {"up": 0, "down": 0},
            "/memory": {"inuse": 0, "oslimit": 0},
            "/logs": {"type": "info", "payload": "fake core ready"},
        }
        self.connection.sendall(self._websocket_frame(payloads.get(path, {})))
        self.connection.settimeout(30)
        try:
            while self.connection.recv(4096):
                pass
        except (OSError, TimeoutError):
            pass
        self.close_connection = True

    def do_GET(self) -> None:
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._websocket()
            return
        path = self.path.split("?", 1)[0]
        payloads = {
            "/version": {"version": "v1.19.9"},
            "/configs": {"mode": "rule", "mixed-port": 7890},
            "/proxies": {"proxies": {}},
            "/providers/proxies": {"providers": {}},
            "/rules": {"rules": []},
            "/providers/rules": {"providers": {}},
            "/connections": {"connections": [], "downloadTotal": 0, "uploadTotal": 0},
            "/group": {"groups": {}},
        }
        if path in payloads:
            self._json_response(payloads[path])
            return
        self._reply()

    do_HEAD = _reply
    do_POST = _reply
    do_PUT = _reply
    do_PATCH = _reply
    do_DELETE = _reply
    do_OPTIONS = _reply


class FakeGithubHandler(QuietHandler):
    hits = 0
    archive = b""
    archive_digest = ""
    release_version = "1.1.0"

    def do_GET(self) -> None:
        type(self).hits += 1
        if self.path == "/repos/MetaCubeX/metacubexd/releases/latest" or self.path.startswith(
            "/repos/MetaCubeX/metacubexd/releases/tags/v"
        ):
            port = self.server.server_address[1]
            payload = {
                "tag_name": f"v{self.release_version}",
                "draft": False,
                "prerelease": False,
                "html_url": f"http://127.0.0.1:{port}/release",
                "assets": [
                    {
                        "name": "compressed-dist.tgz",
                        "size": len(self.archive),
                        "digest": f"sha256:{self.archive_digest}",
                        "browser_download_url": f"http://127.0.0.1:{port}/asset.tgz",
                    }
                ],
            }
            body = json.dumps(payload).encode()
        elif self.path == "/asset.tgz":
            body = self.archive
        else:
            body = json.dumps({"path": self.path, "hit": self.hits}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class FakeSubscriptionHandler(QuietHandler):
    last_user_agent = ""

    def do_GET(self) -> None:
        type(self).last_user_agent = self.headers.get("User-Agent", "")
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/config.yaml?from=redirect")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/html":
            body = b"<!doctype html><title>blocked</title>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        elif self.headers.get("User-Agent") != "clash.meta":
            body = b"<html><body>wrong client</body></html>"
            self.send_response(421)
            self.send_header("Content-Type", "text/html")
        else:
            body = b"mode: rule\nproxies:\n  - {name: test, type: direct}\nrules: []\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_ui(path: Path, version_label: str = "bundled") -> None:
    (path / "_nuxt").mkdir(parents=True)
    (path / "index.html").write_text(f"<html><title>{version_label}</title><body>{version_label}</body></html>")
    (path / "config.js").write_text("window.__METACUBEXD_CONFIG__={}")
    (path / "_nuxt/app.js").write_text("console.log('ok')")


def ui_archive(label: str) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as package:
        files = {
            "index.html": f"<html><title>{label}</title><body>{label}</body></html>".encode(),
            "config.js": b"window.__METACUBEXD_CONFIG__={}",
            "_nuxt/app.js": b"console.log('updated')",
        }
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            package.addfile(info, io.BytesIO(content))
    return stream.getvalue()


class RunningServer:
    def __init__(self, server: ThreadingHTTPServer):
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self.server

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class URLTests(unittest.TestCase):
    def test_valid_core_urls_and_paths(self) -> None:
        self.assertEqual(validate_core_url(" http://127.0.0.1:9090/ "), "http://127.0.0.1:9090")
        self.assertEqual(
            join_target_url("https://nas.example:9443/base/", "/version", "x=1"),
            "https://nas.example:9443/base/version?x=1",
        )
        self.assertEqual(normalize_version("v1.273.0"), "1.273.0")

    def test_rejects_unsafe_core_urls(self) -> None:
        for value in ("file:///tmp/core", "http://user:pass@host:9090", "http://host:99999", "http://host:9090/?x=1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_core_url(value)
        with self.assertRaises(ValueError):
            normalize_version("v1.273.0-beta")

    def test_dynamic_config_contains_bridges_and_migration(self) -> None:
        script = dynamic_config_javascript().decode()
        self.assertIn("defaultBackendURL: proxyURL", script)
        self.assertIn("https://api.github.com", script)
        self.assertIn("metacubexd_fnos_proxy_v2", script)
        self.assertIn("serviceWorker.getRegistrations", script)
        self.assertIn("isCoreUpgrade", script)
        self.assertIn("isRemoteConfig", script)
        self.assertIn("subscription/fetch", script)
        self.assertIn("JSON.stringify({ url: url.href })", script)
        self.assertIn("keepalive: true", script)

    def test_core_upgrade_defaults_to_latest_stable(self) -> None:
        self.assertEqual(stable_core_upgrade_query(""), "channel=stable&force=true")
        self.assertEqual(
            stable_core_upgrade_query("channel=alpha&force=false"),
            "channel=alpha&force=false",
        )

    def test_subscription_fetch_uses_mihomo_user_agent_and_follows_redirect(self) -> None:
        subscription = ThreadingHTTPServer(("127.0.0.1", 0), FakeSubscriptionHandler)
        with RunningServer(subscription):
            url = f"http://127.0.0.1:{subscription.server_address[1]}/redirect"
            payload = fetch_subscription(url, allow_private=True)
        self.assertIn(b"proxies:", payload)
        self.assertEqual(FakeSubscriptionHandler.last_user_agent, "clash.meta")

    def test_subscription_fetch_rejects_private_targets_and_html(self) -> None:
        with self.assertRaises(SubscriptionURLRejected):
            fetch_subscription("http://127.0.0.1/config.yaml")
        with self.assertRaises(SubscriptionContentError):
            validate_subscription_payload(b"<!doctype html><title>blocked</title>")
        with self.assertRaises(SubscriptionContentError):
            validate_subscription_payload(b"this is not a clash configuration")


class AdapterServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bundled = self.root / "bundled"
        make_ui(self.bundled)
        self.version_file = self.root / "upstream-version"
        self.version_file.write_text("1.0.0\n")
        self.data = self.root / "data"
        self.core_file = self.data / "mihomo_api"
        self.data.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def state(self, core_port: int, github_port: int | None = None, subscription_fetcher=None) -> AppState:
        self.core_file.write_text(f"http://127.0.0.1:{core_port}\n")
        arguments = dict(
            bundled_ui=self.bundled,
            bundled_version_file=self.version_file,
            data_dir=self.data,
            core_url_file=self.core_file,
            github_api_base=f"http://127.0.0.1:{github_port}" if github_port else "https://api.github.com",
        )
        if subscription_fetcher is not None:
            arguments["subscription_fetcher"] = subscription_fetcher
        state = AppState(**arguments)
        state.seed_bundled_ui()
        return state

    def test_static_health_config_and_http_proxy(self) -> None:
        core = ThreadingHTTPServer(("127.0.0.1", 0), FakeCoreHandler)
        with RunningServer(core):
            adapter = create_server("127.0.0.1", 0, self.state(core.server_address[1]))
            with RunningServer(adapter):
                port = adapter.server_address[1]
                with urlopen(f"http://127.0.0.1:{port}/healthz") as response:
                    self.assertEqual(json.loads(response.read())["ui_version"], "1.0.0")
                with urlopen(f"http://127.0.0.1:{port}/config.js") as response:
                    self.assertIn(b"proxyURL", response.read())
                with urlopen(f"http://127.0.0.1:{port}/missing-route") as response:
                    self.assertIn(b"bundled", response.read())

                connection = HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request(
                    "PATCH",
                    "/mihomo/configs?force=true",
                    body=b'{"mode":"rule"}',
                    headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
                )
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["method"], "PATCH")
                self.assertEqual(payload["path"], "/configs?force=true")
                self.assertEqual(payload["authorization"], "Bearer secret")
                self.assertEqual(payload["body"], '{"mode":"rule"}')

                connection = HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request("POST", "/mihomo/upgrade", headers={"Authorization": "Bearer secret"})
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["path"], "/upgrade?channel=stable&force=true")
                self.assertEqual(payload["authorization"], "Bearer secret")

    def test_proxy_loop_is_rejected(self) -> None:
        core = ThreadingHTTPServer(("127.0.0.1", 0), FakeCoreHandler)
        with RunningServer(core):
            adapter = create_server("127.0.0.1", 0, self.state(core.server_address[1]))
            with RunningServer(adapter):
                connection = HTTPConnection("127.0.0.1", adapter.server_address[1], timeout=5)
                connection.request("GET", "/mihomo/version", headers={"X-MetaCubeXD-FNOS-Proxy": "1"})
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 508)
                connection.close()

    def test_loopback_core_falls_back_to_nas_host_header(self) -> None:
        core = ThreadingHTTPServer(("127.0.0.1", 0), FakeCoreHandler)
        with RunningServer(core):
            # 127.0.0.2 has no listener. Retry the literal local address from
            # Host while retaining the configured core port.
            state = self.state(core.server_address[1])
            self.core_file.write_text(f"http://127.0.0.2:{core.server_address[1]}\n")
            adapter = create_server("127.0.0.1", 0, state)
            with RunningServer(adapter):
                connection = HTTPConnection("127.0.0.1", adapter.server_address[1], timeout=5)
                connection.request(
                    "GET",
                    "/mihomo/version",
                    headers={"Host": f"127.0.0.1:{adapter.server_address[1]}"},
                )
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("X-Mihomo-Target-Fallback"), "lan")
                self.assertEqual(payload["version"], "v1.19.9")
                connection.close()

    def test_remote_subscription_is_fetched_server_side_then_imported_by_core(self) -> None:
        captured: list[str] = []
        yaml = b"mode: rule\nproxies:\n  - {name: test, type: direct}\nrules: []\n"

        def fake_fetch(url: str) -> bytes:
            captured.append(url)
            return yaml

        core = ThreadingHTTPServer(("127.0.0.1", 0), FakeCoreHandler)
        with RunningServer(core):
            state = self.state(core.server_address[1], subscription_fetcher=fake_fetch)
            adapter = create_server("127.0.0.1", 0, state)
            with RunningServer(adapter):
                port = adapter.server_address[1]
                source_url = "https://subscription.example/config?token=secret"
                request_body = json.dumps({"url": source_url}).encode()
                connection = HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request(
                    "POST",
                    "/subscription/fetch",
                    body=request_body,
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                downloaded = response.read()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Cache-Control"), "no-store")
                self.assertEqual(downloaded, yaml)
                connection.close()

                import_body = json.dumps({"path": "", "payload": downloaded.decode()}).encode()
                connection = HTTPConnection("127.0.0.1", port, timeout=5)
                connection.request(
                    "PUT",
                    "/mihomo/configs?force=true",
                    body=import_body,
                    headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
                )
                response = connection.getresponse()
                core_request = json.loads(response.read())
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(core_request["path"], "/configs?force=true")
                self.assertEqual(core_request["authorization"], "Bearer secret")
                self.assertIn("proxies:", json.loads(core_request["body"])["payload"])
        self.assertEqual(captured, [source_url])

    def test_subscription_endpoint_rejects_get(self) -> None:
        core = ThreadingHTTPServer(("127.0.0.1", 0), FakeCoreHandler)
        with RunningServer(core):
            adapter = create_server("127.0.0.1", 0, self.state(core.server_address[1]))
            with RunningServer(adapter):
                connection = HTTPConnection("127.0.0.1", adapter.server_address[1], timeout=5)
                connection.request("GET", "/subscription/fetch")
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 405)
                connection.close()

    def test_websocket_bytes_are_tunneled(self) -> None:
        core = ThreadingHTTPServer(("127.0.0.1", 0), FakeCoreHandler)
        with RunningServer(core):
            adapter = create_server("127.0.0.1", 0, self.state(core.server_address[1]))
            with RunningServer(adapter):
                sock = socket.create_connection(("127.0.0.1", adapter.server_address[1]), timeout=5)
                request = (
                    "GET /mihomo/traffic?raw_echo=1 HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{adapter.server_address[1]}\r\n"
                    "Connection: Upgrade\r\nUpgrade: websocket\r\n"
                    "Sec-WebSocket-Key: dGVzdA==\r\nSec-WebSocket-Version: 13\r\n\r\n"
                )
                sock.sendall(request.encode())
                handshake = b""
                while b"\r\n\r\n" not in handshake:
                    handshake += sock.recv(4096)
                self.assertIn(b" 101 ", handshake.split(b"\r\n", 1)[0])
                sock.sendall(b"PING")
                self.assertEqual(sock.recv(4), b"PING")
                sock.close()

    def test_github_proxy_caches_gets(self) -> None:
        FakeGithubHandler.hits = 0
        github = ThreadingHTTPServer(("127.0.0.1", 0), FakeGithubHandler)
        core = ThreadingHTTPServer(("127.0.0.1", 0), FakeCoreHandler)
        with RunningServer(github), RunningServer(core):
            state = self.state(core.server_address[1], github.server_address[1])
            adapter = create_server("127.0.0.1", 0, state)
            with RunningServer(adapter):
                url = f"http://127.0.0.1:{adapter.server_address[1]}/github/rate_limit"
                with urlopen(url) as first:
                    first.read()
                with urlopen(url) as second:
                    second.read()
                self.assertEqual(FakeGithubHandler.hits, 1)

    def test_github_release_history_uses_cached_latest(self) -> None:
        FakeGithubHandler.hits = 0
        github = ThreadingHTTPServer(("127.0.0.1", 0), FakeGithubHandler)
        core = ThreadingHTTPServer(("127.0.0.1", 0), FakeCoreHandler)
        with RunningServer(github), RunningServer(core):
            state = self.state(core.server_address[1], github.server_address[1])
            adapter = create_server("127.0.0.1", 0, state)
            with RunningServer(adapter):
                prefix = f"http://127.0.0.1:{adapter.server_address[1]}/github"
                with urlopen(prefix + "/repos/MetaCubeX/metacubexd/releases/latest") as latest:
                    latest_payload = json.loads(latest.read())
                with urlopen(prefix + "/repos/MetaCubeX/metacubexd/releases?per_page=10") as history:
                    history_payload = json.loads(history.read())
                self.assertEqual(history_payload, [latest_payload])
                self.assertEqual(FakeGithubHandler.hits, 1)

    def test_runtime_ui_update_switches_atomically(self) -> None:
        archive = ui_archive("updated")
        FakeGithubHandler.archive = archive
        FakeGithubHandler.archive_digest = hashlib.sha256(archive).hexdigest()
        FakeGithubHandler.release_version = "1.1.0"
        github = ThreadingHTTPServer(("127.0.0.1", 0), FakeGithubHandler)
        core = ThreadingHTTPServer(("127.0.0.1", 0), FakeCoreHandler)
        with RunningServer(github), RunningServer(core):
            state = self.state(core.server_address[1], github.server_address[1])
            adapter = create_server("127.0.0.1", 0, state)
            with RunningServer(adapter):
                connection = HTTPConnection("127.0.0.1", adapter.server_address[1], timeout=10)
                connection.request("POST", "/mihomo/upgrade/ui")
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()
                self.assertEqual(response.status, 200)
                self.assertTrue(payload["changed"])
                self.assertEqual(state.active_version, "1.1.0")
                self.assertIn("updated", (state.current_ui / "index.html").read_text())
                self.assertTrue((state.previous_ui / "index.html").exists())

    def test_update_lock_rejects_concurrency(self) -> None:
        core = ThreadingHTTPServer(("127.0.0.1", 0), FakeCoreHandler)
        with RunningServer(core):
            state = self.state(core.server_address[1])
            state.update_lock.acquire()
            try:
                with self.assertRaises(UpdateBusyError):
                    state.update_ui()
            finally:
                state.update_lock.release()

    def test_failed_runtime_update_keeps_current_ui(self) -> None:
        archive = ui_archive("untrusted")
        FakeGithubHandler.archive = archive
        FakeGithubHandler.archive_digest = "0" * 64
        FakeGithubHandler.release_version = "1.1.0"
        github = ThreadingHTTPServer(("127.0.0.1", 0), FakeGithubHandler)
        core = ThreadingHTTPServer(("127.0.0.1", 0), FakeCoreHandler)
        with RunningServer(github), RunningServer(core):
            state = self.state(core.server_address[1], github.server_address[1])
            original = (state.current_ui / "index.html").read_bytes()
            with self.assertRaises(FnosServerError):
                state.update_ui()
            self.assertEqual(state.active_version, "1.0.0")
            self.assertEqual((state.current_ui / "index.html").read_bytes(), original)


class ArchiveSafetyTests(unittest.TestCase):
    def test_rejects_path_traversal_and_links(self) -> None:
        for name, configure in (
            ("../escape", lambda info: None),
            ("link", lambda info: setattr(info, "type", tarfile.SYMTYPE)),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "bad.tgz"
                with tarfile.open(archive, "w:gz") as package:
                    info = tarfile.TarInfo(name)
                    configure(info)
                    if info.isfile():
                        info.size = 1
                        package.addfile(info, io.BytesIO(b"x"))
                    else:
                        package.addfile(info)
                with self.assertRaises(FnosServerError):
                    safe_extract_ui(archive, Path(temporary) / "out")

    def test_digest_mismatch_keeps_download_untrusted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.tgz"
            source.write_bytes(b"not a tar")
            asset = {
                "size": source.stat().st_size,
                "digest": "sha256:" + "0" * 64,
                "browser_download_url": source.as_uri(),
            }
            with self.assertRaises(FnosServerError):
                download_release_asset(asset, Path(temporary) / "download.tgz")


if __name__ == "__main__":
    unittest.main()
