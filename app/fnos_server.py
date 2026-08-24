#!/usr/bin/env python3
"""fnOS static server, Mihomo reverse proxy, and safe UI updater.

The upstream MetaCubeXD release remains an unmodified static artifact.  This
server adds the fnOS-specific behavior at runtime: a same-origin Mihomo proxy,
GitHub API proxying, and atomic updates of the persisted UI copy.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import ipaddress
import json
import logging
import os
import posixpath
import re
import selectors
import shutil
import socket
import ssl
import threading
import time
import uuid
from dataclasses import dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Iterable, Mapping
from urllib.error import HTTPError
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
import tarfile


DEFAULT_CORE_URL = "http://127.0.0.1:9090"
DEFAULT_GITHUB_API = "https://api.github.com"
UPSTREAM_REPOSITORY = "MetaCubeX/metacubexd"
UPSTREAM_ASSET = "compressed-dist.tgz"
PROXY_PREFIX = "/mihomo"
GITHUB_PREFIX = "/github"
SUBSCRIPTION_FETCH_PATH = "/subscription/fetch"
PROXY_GUARD_HEADER = "X-MetaCubeXD-FNOS-Proxy"
USER_AGENT = "metacubexd-fnos/1"
SUBSCRIPTION_USER_AGENT = "clash.meta"

MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_GITHUB_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_EXTRACTED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 20_000
MAX_SUBSCRIPTION_URL_BYTES = 16 * 1024
MAX_SUBSCRIPTION_BYTES = 16 * 1024 * 1024
MAX_SUBSCRIPTION_REDIRECTS = 5
GITHUB_CACHE_SECONDS = 300

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class FnosServerError(RuntimeError):
    """Expected runtime error that can safely be shown to an administrator."""


class UpdateBusyError(FnosServerError):
    """Raised when an update is already running."""


class SubscriptionURLRejected(FnosServerError):
    """Raised when a subscription target is unsafe or malformed."""


class SubscriptionFetchError(FnosServerError):
    """Raised when a safe subscription target cannot be downloaded."""


class SubscriptionContentError(FnosServerError):
    """Raised when a response is not a usable Mihomo/Clash configuration."""


def normalize_version(value: str) -> str:
    """Return an x.y.z version string and reject prerelease/non-numeric tags."""

    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value.strip())
    if not match:
        raise ValueError(f"invalid stable version: {value!r}")
    return ".".join(match.groups())


def version_key(value: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in normalize_version(value).split("."))  # type: ignore[return-value]


def validate_core_url(value: str) -> str:
    """Validate and normalize a configured Mihomo base URL."""

    raw = value.strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Mihomo API must use http:// or https://")
    if not parsed.hostname:
        raise ValueError("Mihomo API must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentials are not allowed in the Mihomo API URL")
    if parsed.query or parsed.fragment:
        raise ValueError("query strings and fragments are not allowed in the Mihomo API URL")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid Mihomo API port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("invalid Mihomo API port")
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def join_target_url(base_url: str, suffix_path: str, query: str = "") -> str:
    parsed = urlsplit(validate_core_url(base_url))
    base_path = parsed.path.rstrip("/")
    suffix = "/" + suffix_path.lstrip("/")
    path = f"{base_path}{suffix}" if suffix != "/" else (base_path or "/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))


def stable_core_upgrade_query(query: str) -> str:
    """Add stable/forced defaults without overriding explicit API choices."""

    pairs = parse_qsl(query, keep_blank_values=True)
    names = {name.lower() for name, _value in pairs}
    if "channel" not in names:
        pairs.append(("channel", "stable"))
    if "force" not in names:
        pairs.append(("force", "true"))
    return urlencode(pairs)


def read_limited(stream: BinaryIO, maximum: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = stream.read(min(64 * 1024, maximum - size + 1))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > maximum:
            raise FnosServerError(f"response exceeded {maximum} bytes")
    return b"".join(chunks)


@dataclass(frozen=True)
class SubscriptionTarget:
    """A validated target with DNS results pinned for the actual connection."""

    scheme: str
    hostname: str
    port: int
    request_target: str
    host_header: str
    addresses: tuple[str, ...]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Verify TLS for the hostname while connecting to a pre-validated IP."""

    def __init__(self, hostname: str, port: int, address: str, timeout: float) -> None:
        super().__init__(hostname, port, timeout=timeout, context=ssl.create_default_context())
        self._validated_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._validated_address, self.port),
            self.timeout,
            self.source_address,
        )
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def validate_subscription_url(value: str, *, allow_private: bool = False) -> SubscriptionTarget:
    """Validate a public HTTP(S) URL and pin its current DNS addresses.

    Runtime callers never enable ``allow_private``. The opt-in exists only so
    protocol tests can use a loopback fixture without weakening the endpoint.
    """

    raw = value.strip()
    if not raw or len(raw.encode("utf-8")) > MAX_SUBSCRIPTION_URL_BYTES:
        raise SubscriptionURLRejected("subscription URL is empty or too long")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise SubscriptionURLRejected("subscription URL has an invalid port") from exc
    if parsed.scheme not in {"http", "https"}:
        raise SubscriptionURLRejected("subscription URL must use http:// or https://")
    if not parsed.hostname:
        raise SubscriptionURLRejected("subscription URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise SubscriptionURLRejected("credentials are not allowed in the subscription URL authority")
    if parsed.fragment:
        raise SubscriptionURLRejected("subscription URL fragments are not allowed")

    hostname = parsed.hostname.rstrip(".")
    if not hostname or "%" in hostname:
        raise SubscriptionURLRejected("subscription host is invalid")
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise SubscriptionURLRejected("subscription host is invalid") from exc
    port = port or (443 if parsed.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise SubscriptionURLRejected("subscription URL has an invalid port")

    request_target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    try:
        request_target.encode("ascii")
    except UnicodeEncodeError as exc:
        raise SubscriptionURLRejected("subscription URL path must be percent-encoded") from exc

    try:
        resolved = socket.getaddrinfo(ascii_hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SubscriptionFetchError("subscription host could not be resolved") from exc
    addresses = tuple(dict.fromkeys(item[4][0].split("%", 1)[0] for item in resolved))
    if not addresses:
        raise SubscriptionFetchError("subscription host returned no network addresses")
    if not allow_private:
        try:
            unsafe = any(not ipaddress.ip_address(address).is_global for address in addresses)
        except ValueError as exc:
            raise SubscriptionURLRejected("subscription host resolved to an invalid address") from exc
        if unsafe:
            raise SubscriptionURLRejected("subscription host resolves to a private or reserved address")

    host_literal = f"[{ascii_hostname}]" if ":" in ascii_hostname else ascii_hostname
    default_port = 443 if parsed.scheme == "https" else 80
    host_header = host_literal if port == default_port else f"{host_literal}:{port}"
    return SubscriptionTarget(
        scheme=parsed.scheme,
        hostname=ascii_hostname,
        port=port,
        request_target=request_target,
        host_header=host_header,
        addresses=addresses,
    )


def validate_subscription_payload(payload: bytes) -> None:
    """Reject HTML/error pages and accept common Mihomo/Clash YAML or JSON."""

    if not payload:
        raise SubscriptionContentError("subscription source returned an empty response")
    if len(payload) > MAX_SUBSCRIPTION_BYTES:
        raise SubscriptionContentError("subscription response exceeded the size limit")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SubscriptionContentError("subscription response is not UTF-8 text") from exc
    if "\x00" in text:
        raise SubscriptionContentError("subscription response contains binary data")
    stripped = text.lstrip()
    leading = stripped[:256].lower()
    if leading.startswith(("<!doctype html", "<html", "<head", "<body")):
        raise SubscriptionContentError("subscription source returned an HTML page instead of YAML")

    known_keys = {
        "dns",
        "mode",
        "mixed-port",
        "port",
        "proxies",
        "proxy-groups",
        "proxy-providers",
        "rule-providers",
        "rules",
        "socks-port",
        "tun",
    }
    if stripped.startswith("{"):
        try:
            parsed_json = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SubscriptionContentError("subscription response is not valid JSON") from exc
        if isinstance(parsed_json, dict) and known_keys.intersection(parsed_json):
            return
    top_level_keys = {
        key
        for match in re.finditer(
            r"(?m)^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(?:#.*)?$|^([A-Za-z][A-Za-z0-9_-]*)\s*:",
            text,
        )
        for key in match.groups()
        if key is not None
    }
    if not known_keys.intersection(top_level_keys):
        raise SubscriptionContentError("response is not a recognizable Mihomo/Clash configuration")


def _subscription_request(target: SubscriptionTarget, timeout: float) -> tuple[int, list[tuple[str, str]], bytes]:
    headers = {
        "Host": target.host_header,
        "User-Agent": SUBSCRIPTION_USER_AGENT,
        "Accept": "application/yaml, text/yaml, text/plain, application/json, */*",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    last_error: BaseException | None = None
    for address in target.addresses:
        if target.scheme == "https":
            connection: http.client.HTTPConnection = _PinnedHTTPSConnection(
                target.hostname, target.port, address, timeout
            )
        else:
            connection = http.client.HTTPConnection(address, target.port, timeout=timeout)
        try:
            connection.request("GET", target.request_target, headers=headers)
            response = connection.getresponse()
            content_length = response.getheader("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > MAX_SUBSCRIPTION_BYTES:
                        raise SubscriptionContentError("subscription response exceeded the size limit")
                except ValueError as exc:
                    raise SubscriptionFetchError("subscription source returned an invalid Content-Length") from exc
            body = read_limited(response, MAX_SUBSCRIPTION_BYTES)
            return response.status, response.getheaders(), body
        except SubscriptionContentError:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            last_error = exc
        finally:
            connection.close()
    raise SubscriptionFetchError("subscription source is unreachable") from last_error


def fetch_subscription(
    url: str,
    *,
    timeout: float = 30,
    allow_private: bool = False,
) -> bytes:
    """Download and validate a subscription without exposing an SSRF primitive."""

    current = url.strip()
    redirect_statuses = {
        HTTPStatus.MOVED_PERMANENTLY,
        HTTPStatus.FOUND,
        HTTPStatus.SEE_OTHER,
        HTTPStatus.TEMPORARY_REDIRECT,
        HTTPStatus.PERMANENT_REDIRECT,
    }
    for redirect_count in range(MAX_SUBSCRIPTION_REDIRECTS + 1):
        target = validate_subscription_url(current, allow_private=allow_private)
        status, headers, body = _subscription_request(target, timeout)
        if status in redirect_statuses:
            if redirect_count == MAX_SUBSCRIPTION_REDIRECTS:
                raise SubscriptionFetchError("subscription source redirected too many times")
            location = next((value for name, value in headers if name.lower() == "location"), "")
            if not location:
                raise SubscriptionFetchError("subscription source returned a redirect without Location")
            current = urljoin(current, location)
            continue
        if status != HTTPStatus.OK:
            raise SubscriptionFetchError(f"subscription source returned HTTP {status}")
        content_encoding = next(
            (value for name, value in headers if name.lower() == "content-encoding"), ""
        ).strip().lower()
        if content_encoding not in {"", "identity"}:
            raise SubscriptionContentError("subscription source used an unsupported content encoding")
        validate_subscription_payload(body)
        return body
    raise SubscriptionFetchError("subscription source redirected too many times")


def github_request(
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, str] | None = None,
    timeout: float = 30,
    maximum: int = MAX_GITHUB_RESPONSE_BYTES,
) -> tuple[int, list[tuple[str, str]], bytes]:
    request_headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            body = b"" if method == "HEAD" else read_limited(response, maximum)
            return response.status, list(response.headers.items()), body
    except HTTPError as exc:
        body = b"" if method == "HEAD" else read_limited(exc, maximum)
        return exc.code, list(exc.headers.items()), body


def fetch_release(
    *,
    version: str | None = None,
    api_base: str = DEFAULT_GITHUB_API,
    timeout: float = 30,
) -> dict:
    endpoint = "releases/latest" if version is None else f"releases/tags/v{normalize_version(version)}"
    url = f"{api_base.rstrip('/')}/repos/{UPSTREAM_REPOSITORY}/{endpoint}"
    status, _headers, body = github_request(url, timeout=timeout)
    if status != HTTPStatus.OK:
        raise FnosServerError(f"GitHub release request failed with HTTP {status}")
    try:
        release = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FnosServerError("GitHub returned invalid release metadata") from exc
    if release.get("draft") or release.get("prerelease"):
        raise FnosServerError("refusing non-stable GitHub release")
    normalize_version(str(release.get("tag_name", "")))
    return release


def release_asset(release: Mapping, name: str = UPSTREAM_ASSET) -> Mapping:
    for asset in release.get("assets", []):
        if asset.get("name") == name:
            digest = str(asset.get("digest") or "")
            if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
                raise FnosServerError(f"GitHub asset {name} has no valid SHA-256 digest")
            if not asset.get("browser_download_url"):
                raise FnosServerError(f"GitHub asset {name} has no download URL")
            return asset
    raise FnosServerError(f"GitHub release does not contain {name}")


def download_release_asset(asset: Mapping, destination: Path, timeout: float = 120) -> str:
    expected_size = int(asset.get("size") or 0)
    if expected_size <= 0 or expected_size > MAX_ARCHIVE_BYTES:
        raise FnosServerError("GitHub asset size is missing or unsafe")
    expected_digest = str(asset["digest"]).split(":", 1)[1].lower()
    request = Request(str(asset["browser_download_url"]), headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    written = 0
    with urlopen(request, timeout=timeout) as response, destination.open("wb") as output:
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_ARCHIVE_BYTES:
                raise FnosServerError("GitHub asset exceeded the download limit")
            digest.update(chunk)
            output.write(chunk)
    if written != expected_size:
        raise FnosServerError(f"GitHub asset size mismatch: expected {expected_size}, got {written}")
    actual_digest = digest.hexdigest()
    if actual_digest != expected_digest:
        raise FnosServerError("GitHub asset SHA-256 digest mismatch")
    return actual_digest


def _safe_member_path(name: str) -> PurePosixPath:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    parts = tuple(part for part in path.parts if part not in {"", "."})
    if path.is_absolute() or not parts or any(part == ".." for part in parts):
        raise FnosServerError(f"unsafe archive path: {name!r}")
    return PurePosixPath(*parts)


def safe_extract_ui(archive: Path, destination: Path) -> None:
    """Extract only regular files/directories from the upstream tar archive."""

    destination.mkdir(parents=True, exist_ok=False)
    total_size = 0
    with tarfile.open(archive, mode="r:gz") as package:
        members = package.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise FnosServerError("UI archive contains too many entries")
        for member in members:
            if member.name.replace("\\", "/").rstrip("/") in {"", "."}:
                if member.isdir():
                    continue
                raise FnosServerError("UI archive root entry is not a directory")
            relative = _safe_member_path(member.name)
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise FnosServerError(f"UI archive contains unsupported entry: {member.name!r}")
            total_size += member.size
            if total_size > MAX_EXTRACTED_BYTES:
                raise FnosServerError("UI archive expands beyond the safety limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            source = package.extractfile(member)
            if source is None:
                raise FnosServerError(f"cannot read archive entry: {member.name!r}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output, 64 * 1024)
            target.chmod(0o644)


def validate_ui_dir(path: Path) -> None:
    required = (path / "index.html", path / "config.js", path / "_nuxt")
    if not required[0].is_file() or not required[1].is_file() or not required[2].is_dir():
        raise FnosServerError("UI is missing index.html, config.js, or _nuxt")
    if not any(item.is_file() and item.suffix == ".js" for item in required[2].iterdir()):
        raise FnosServerError("UI _nuxt directory contains no JavaScript bundle")


def read_version_file(path: Path) -> str | None:
    try:
        return normalize_version(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def write_text_atomic(path: Path, value: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


@dataclass(frozen=True)
class CachedResponse:
    expires_at: float
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class AppState:
    def __init__(
        self,
        *,
        bundled_ui: Path,
        bundled_version_file: Path,
        data_dir: Path,
        core_url_file: Path,
        github_api_base: str = DEFAULT_GITHUB_API,
        subscription_fetcher: Callable[[str], bytes] = fetch_subscription,
    ) -> None:
        self.bundled_ui = bundled_ui.resolve()
        self.bundled_version_file = bundled_version_file.resolve()
        self.data_dir = data_dir.resolve()
        self.core_url_file = core_url_file.resolve()
        self.ui_root = self.data_dir / "ui"
        self.current_ui = self.ui_root / "current"
        self.previous_ui = self.ui_root / "previous"
        self.github_api_base = github_api_base.rstrip("/")
        self.subscription_fetcher = subscription_fetcher
        self.update_lock = threading.Lock()
        self.cache_lock = threading.Lock()
        self.github_cache: dict[str, CachedResponse] = {}

    @property
    def bundled_version(self) -> str:
        version = read_version_file(self.bundled_version_file)
        if version is None:
            raise FnosServerError("bundled upstream-version is missing or invalid")
        return version

    @property
    def active_version(self) -> str:
        return read_version_file(self.current_ui / ".upstream-version") or self.bundled_version

    def static_root(self) -> Path:
        try:
            validate_ui_dir(self.current_ui)
            return self.current_ui
        except FnosServerError:
            return self.bundled_ui

    def core_url(self) -> str:
        try:
            configured = self.core_url_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            configured = DEFAULT_CORE_URL
        return validate_core_url(configured)

    def _staging_dir(self) -> Path:
        self.ui_root.mkdir(parents=True, exist_ok=True)
        return self.ui_root / f".staging-{uuid.uuid4().hex}"

    def _activate(self, staging: Path) -> None:
        validate_ui_dir(staging)
        if self.previous_ui.exists():
            shutil.rmtree(self.previous_ui)
        moved_current = False
        if self.current_ui.exists():
            os.replace(self.current_ui, self.previous_ui)
            moved_current = True
        try:
            os.replace(staging, self.current_ui)
        except BaseException:
            if moved_current and self.previous_ui.exists() and not self.current_ui.exists():
                os.replace(self.previous_ui, self.current_ui)
            raise

    def seed_bundled_ui(self) -> bool:
        validate_ui_dir(self.bundled_ui)
        bundled_version = self.bundled_version
        active_version = read_version_file(self.current_ui / ".upstream-version")
        try:
            validate_ui_dir(self.current_ui)
            active_valid = active_version is not None
        except FnosServerError:
            active_valid = False
        if active_valid and version_key(active_version) >= version_key(bundled_version):
            return False
        staging = self._staging_dir()
        try:
            shutil.copytree(self.bundled_ui, staging)
            write_text_atomic(staging / ".upstream-version", bundled_version + "\n")
            self._activate(staging)
            return True
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def cached_github_response(self, path_and_query: str, method: str) -> CachedResponse:
        cache_key = path_and_query
        now = time.monotonic()
        release_latest_key: str | None = None
        if method == "GET":
            with self.cache_lock:
                cached = self.github_cache.get(cache_key)
                if cached and cached.expires_at > now:
                    return cached

                # MetaCubeXD asks for both /releases/latest and a ten-item
                # release history when opening its version dialog.  On slower
                # NAS connections the latter can exceed the UI's fixed
                # ten-second timeout.  The stable latest release is sufficient
                # for update detection, so serve it as a one-item history when
                # it is already in the short-lived cache.  A later request can
                # still fetch the complete history after this cache expires.
                parsed = urlsplit(path_and_query)
                if parsed.path.endswith("/releases"):
                    latest_key = parsed.path + "/latest"
                    latest = self.github_cache.get(latest_key)
                    if latest and latest.expires_at > now and latest.status == HTTPStatus.OK:
                        synthesized = self._release_history(latest)
                        if synthesized is not None:
                            self.github_cache[cache_key] = synthesized
                            return synthesized
                    release_latest_key = latest_key

        # The history and latest calls are started concurrently by the UI.  If
        # the cache was not populated yet, deliberately fetch the much smaller
        # latest endpoint instead of waiting on GitHub's full release history.
        if release_latest_key is not None:
            latest_url = f"{self.github_api_base}{release_latest_key}"
            status, headers, body = github_request(latest_url, method="GET")
            latest = CachedResponse(now + GITHUB_CACHE_SECONDS, status, tuple(headers), body)
            if status == HTTPStatus.OK:
                synthesized = self._release_history(latest)
                if synthesized is not None:
                    with self.cache_lock:
                        self.github_cache[release_latest_key] = latest
                        self.github_cache[cache_key] = synthesized
                    return synthesized
        url = f"{self.github_api_base}{path_and_query}"
        status, headers, body = github_request(url, method=method)
        response = CachedResponse(now + GITHUB_CACHE_SECONDS, status, tuple(headers), body)
        if method == "GET" and status == HTTPStatus.OK:
            with self.cache_lock:
                self.github_cache[cache_key] = response
        return response

    @staticmethod
    def _release_history(latest: CachedResponse) -> CachedResponse | None:
        try:
            release = json.loads(latest.body)
            body = json.dumps([release], separators=(",", ":")).encode()
        except (TypeError, ValueError, UnicodeDecodeError):
            return None
        headers = tuple(
            (name, value)
            for name, value in latest.headers
            if name.lower() not in {"content-length", "content-encoding"}
        )
        return CachedResponse(latest.expires_at, HTTPStatus.OK, headers, body)

    def update_ui(self) -> dict[str, str | bool]:
        if not self.update_lock.acquire(blocking=False):
            raise UpdateBusyError("a UI update is already running")
        try:
            release = fetch_release(api_base=self.github_api_base)
            new_version = normalize_version(str(release["tag_name"]))
            old_version = self.active_version
            if version_key(new_version) <= version_key(old_version):
                return {"changed": False, "old_version": old_version, "new_version": old_version}
            asset = release_asset(release)
            self.ui_root.mkdir(parents=True, exist_ok=True)
            staging = self._staging_dir()
            archive = self.ui_root / f".download-{uuid.uuid4().hex}.tgz"
            try:
                download_release_asset(asset, archive)
                safe_extract_ui(archive, staging)
                validate_ui_dir(staging)
                write_text_atomic(staging / ".upstream-version", new_version + "\n")
                self._activate(staging)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    archive.unlink()
                if staging.exists():
                    shutil.rmtree(staging)
            return {"changed": True, "old_version": old_version, "new_version": new_version}
        finally:
            self.update_lock.release()


def dynamic_config_javascript() -> bytes:
    """Bootstrap the same-origin endpoint and bridge upstream GitHub calls."""

    script = r"""(() => {
  const base = new URL('.', document.baseURI);
  const proxyURL = new URL('mihomo', base).href.replace(/\/$/, '');
  window.__METACUBEXD_CONFIG__ = { defaultBackendURL: proxyURL, githubToken: '' };

  try {
    const marker = 'metacubexd_fnos_proxy_v1';
    if (!localStorage.getItem(marker)) {
      const list = JSON.parse(localStorage.getItem('endpointList') || '[]');
      const selected = localStorage.getItem('selectedEndpoint') || '';
      const current = Array.isArray(list) ? list.find((item) => item && item.id === selected) : null;
      if (current) {
        current.url = proxyURL;
        localStorage.setItem('endpointList', JSON.stringify([current]));
      } else {
        localStorage.removeItem('endpointList');
        localStorage.removeItem('selectedEndpoint');
      }
      localStorage.setItem(marker, '1');
    }
  } catch (_) {}

  if (!window.__METACUBEXD_FNOS_FETCH__) {
    const nativeFetch = window.fetch.bind(window);
    window.__METACUBEXD_FNOS_FETCH__ = nativeFetch;
    window.fetch = async (input, init) => {
      let request = input instanceof Request ? input : new Request(input, init);
      let url = new URL(request.url, window.location.href);
      const isGithub = url.origin === 'https://api.github.com';
      const isRemoteConfig = request.method === 'GET' &&
        (url.protocol === 'http:' || url.protocol === 'https:') &&
        url.origin !== window.location.origin && !isGithub;
      const isCoreUpgrade = request.method === 'POST' &&
        url.origin === window.location.origin &&
        url.pathname.replace(/\/+$/, '').endsWith('/mihomo/upgrade');
      const isUiUpgrade = request.method === 'POST' &&
        url.origin === window.location.origin &&
        url.pathname.replace(/\/+$/, '').endsWith('/mihomo/upgrade/ui');

      if (isGithub) {
        const target = new URL(`github${url.pathname}${url.search}`, base);
        request = new Request(target, {
          method: request.method,
          headers: request.headers,
          credentials: 'same-origin',
          cache: 'no-store',
          signal: new AbortController().signal,
        });
        url = target;
      } else if (isRemoteConfig) {
        const target = new URL('subscription/fetch', base);
        request = new Request(target, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: url.href }),
          credentials: 'same-origin',
          cache: 'no-store',
          signal: request.signal,
        });
        url = target;
      } else if (isCoreUpgrade || isUiUpgrade) {
        if (isCoreUpgrade) {
          url.searchParams.set('channel', url.searchParams.get('channel') || 'stable');
          url.searchParams.set('force', url.searchParams.get('force') || 'true');
        }
        const body = await request.clone().arrayBuffer();
        request = new Request(url, {
          method: request.method,
          headers: request.headers,
          body,
          credentials: 'same-origin',
          cache: 'no-store',
          keepalive: true,
          signal: new AbortController().signal,
        });
      }

      const response = await nativeFetch(request);
      if (isUiUpgrade && response.ok) {
        try {
          if ('serviceWorker' in navigator) {
            const registrations = await navigator.serviceWorker.getRegistrations();
            await Promise.all(registrations.map((registration) => registration.unregister()));
          }
          if ('caches' in window) {
            const names = await caches.keys();
            await Promise.all(names.map((name) => caches.delete(name)));
          }
        } catch (_) {}
      }
      return response;
    };
  }
})();
"""
    return script.encode("utf-8")


class FnosHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: AppState):
        self.state = state
        super().__init__(address, FnosRequestHandler)


class FnosRequestHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: FnosHTTPServer

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(kwargs.pop("directory", ".")), **kwargs)

    def copyfile(self, source: BinaryIO, outputfile: BinaryIO) -> None:
        try:
            super().copyfile(source, outputfile)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Browsers routinely cancel superseded asset and navigation
            # requests.  This is not a server failure and should not fill the
            # fnOS application log with socket tracebacks.
            return

    def log_message(self, fmt: str, *args) -> None:
        logging.info("%s - %s", self.client_address[0], fmt % args)

    def translate_path(self, path: str) -> str:
        parsed_path = urlsplit(path).path
        decoded = unquote(parsed_path, errors="surrogatepass")
        normalized = posixpath.normpath(decoded)
        words = [word for word in normalized.split("/") if word]
        result = self.server.state.static_root()
        for word in words:
            if os.path.dirname(word) or word in {".", ".."}:
                continue
            result = result / word
        return str(result)

    def end_headers(self) -> None:
        path = urlsplit(self.path).path
        if path in {"/", "/index.html", "/200.html", "/404.html", "/sw.js"}:
            self.send_header("Cache-Control", "no-store")
        elif path.startswith("/_nuxt/") or path.startswith("/_fonts/"):
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:
        self._dispatch("OPTIONS")

    def _dispatch(self, method: str) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/healthz":
            self._send_json(HTTPStatus.OK, {"status": "ok", "ui_version": self.server.state.active_version}, method)
            return
        if path == "/config.js":
            body = dynamic_config_javascript()
            self._send_bytes(HTTPStatus.OK, body, "text/javascript; charset=utf-8", method, {"Cache-Control": "no-store"})
            return
        if path == SUBSCRIPTION_FETCH_PATH:
            self._handle_subscription_fetch(method)
            return
        if path == GITHUB_PREFIX or path.startswith(GITHUB_PREFIX + "/"):
            self._handle_github(method, parsed)
            return
        if path == PROXY_PREFIX or path.startswith(PROXY_PREFIX + "/"):
            if self.headers.get(PROXY_GUARD_HEADER):
                self._send_json(508, {"error": "proxy loop detected"}, method)
                return
            if self._is_websocket_upgrade():
                self._proxy_websocket(parsed)
                return
            if path.rstrip("/") == f"{PROXY_PREFIX}/upgrade/ui" and method == "POST":
                self._handle_ui_update(method)
                return
            if path.rstrip("/") == f"{PROXY_PREFIX}/upgrade" and method == "POST":
                parsed = parsed._replace(query=stable_core_upgrade_query(parsed.query))
            self._proxy_http(method, parsed)
            return
        if method not in {"GET", "HEAD"}:
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"}, method)
            return
        self._serve_static(method)

    def _serve_static(self, method: str) -> None:
        path = urlsplit(self.path).path
        translated = Path(self.translate_path(self.path))
        if not translated.exists() and "." not in Path(path).name:
            self.path = "/index.html"
        if method == "HEAD":
            super().do_HEAD()
        else:
            super().do_GET()

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        method: str,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for name, value in headers.items():
                self.send_header(name, value)
        self.end_headers()
        if method != "HEAD" and body:
            self.wfile.write(body)

    def _send_json(self, status: int, payload: Mapping, method: str) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8", method)

    def _read_request_body(self, maximum: int = MAX_REQUEST_BYTES) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b""
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise FnosServerError("invalid Content-Length") from exc
        if length < 0 or length > maximum:
            raise FnosServerError("request body is too large")
        return self.rfile.read(length)

    def _handle_subscription_fetch(self, method: str) -> None:
        if method != "POST":
            self._send_json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": "subscription fetch requires POST"},
                method,
            )
            return
        try:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise SubscriptionURLRejected("subscription request must use application/json")
            body = self._read_request_body(MAX_SUBSCRIPTION_URL_BYTES + 1024)
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SubscriptionURLRejected("subscription request contains invalid JSON") from exc
            url = payload.get("url") if isinstance(payload, dict) else None
            if not isinstance(url, str):
                raise SubscriptionURLRejected("subscription request must include a URL")
            subscription = self.server.state.subscription_fetcher(url)
            self._send_bytes(
                HTTPStatus.OK,
                subscription,
                "text/yaml; charset=utf-8",
                method,
                {"Cache-Control": "no-store", "X-Subscription-Proxy": "metacubexd-fnos"},
            )
        except SubscriptionURLRejected as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)}, method)
        except SubscriptionContentError as exc:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)}, method)
        except (SubscriptionFetchError, FnosServerError, OSError, ssl.SSLError) as exc:
            logging.warning("subscription fetch failed: %s", type(exc).__name__)
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)}, method)

    def _core_target(self, parsed) -> tuple[str, str, int, str]:
        suffix = parsed.path[len(PROXY_PREFIX) :] or "/"
        target_url = join_target_url(self.server.state.core_url(), suffix, parsed.query)
        target = urlsplit(target_url)
        port = target.port or (443 if target.scheme == "https" else 80)
        request_target = target.path or "/"
        if target.query:
            request_target += "?" + target.query
        return target.scheme, target.hostname or "", port, request_target

    def _forward_headers(self, target_host: str, target_port: int, scheme: str) -> dict[str, str]:
        headers: dict[str, str] = {}
        connection_tokens = {
            token.strip().lower()
            for token in self.headers.get("Connection", "").split(",")
            if token.strip()
        }
        for name, value in self.headers.items():
            lower = name.lower()
            if lower in HOP_BY_HOP_HEADERS or lower in connection_tokens or lower in {"host", "origin"}:
                continue
            headers[name] = value
        default_port = 443 if scheme == "https" else 80
        host_literal = f"[{target_host}]" if ":" in target_host else target_host
        headers["Host"] = host_literal if target_port == default_port else f"{host_literal}:{target_port}"
        headers[PROXY_GUARD_HEADER] = "1"
        headers["X-Forwarded-For"] = self.client_address[0]
        return headers

    def _proxy_http(self, method: str, parsed) -> None:
        try:
            scheme, host, port, target = self._core_target(parsed)
            body = self._read_request_body()
            headers = self._forward_headers(host, port, scheme)
            connection_type = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            is_core_upgrade = urlsplit(target).path.rstrip("/").endswith("/upgrade")
            timeout = 300 if is_core_upgrade else 75
            if is_core_upgrade:
                logging.info("forwarding Mihomo core upgrade request to %s", target)
            connection = connection_type(host, port, timeout=timeout)
            try:
                connection.request(method, target, body=body if body else None, headers=headers)
                response = connection.getresponse()
                payload = response.read()
                self.send_response(response.status, response.reason)
                response_headers = response.getheaders()
                for name, value in response_headers:
                    if name.lower() not in HOP_BY_HOP_HEADERS and name.lower() != "content-length":
                        self.send_header(name, value)
                if method == "HEAD":
                    upstream_length = next(
                        (value for name, value in response_headers if name.lower() == "content-length"),
                        "0",
                    )
                    self.send_header("Content-Length", upstream_length)
                else:
                    self.send_header("Content-Length", str(len(payload)))
                self.send_header("Via", "metacubexd-fnos")
                self.end_headers()
                if method != "HEAD" and payload:
                    self.wfile.write(payload)
            finally:
                connection.close()
        except (FnosServerError, OSError, http.client.HTTPException, ssl.SSLError) as exc:
            logging.warning("Mihomo proxy failed: %s", exc)
            with contextlib.suppress(OSError):
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "Mihomo core is unavailable"}, method)

    def _handle_github(self, method: str, parsed) -> None:
        if method not in {"GET", "HEAD"}:
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "GitHub proxy is read-only"}, method)
            return
        suffix = parsed.path[len(GITHUB_PREFIX) :] or "/"
        if not suffix.startswith("/") or ".." in PurePosixPath(suffix).parts:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid GitHub API path"}, method)
            return
        path_and_query = suffix + (("?" + parsed.query) if parsed.query else "")
        try:
            response = self.server.state.cached_github_response(path_and_query, method)
            self.send_response(response.status)
            allowed = {"content-type", "etag", "last-modified", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"}
            for name, value in response.headers:
                if name.lower() in allowed:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Cache-Control", "private, max-age=300")
            self.end_headers()
            if method != "HEAD" and response.body:
                self.wfile.write(response.body)
        except (FnosServerError, OSError, ssl.SSLError) as exc:
            logging.warning("GitHub proxy failed: %s", exc)
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "GitHub API is unavailable"}, method)

    def _handle_ui_update(self, method: str) -> None:
        try:
            result = self.server.state.update_ui()
            self._send_bytes(
                HTTPStatus.OK,
                json.dumps(result, separators=(",", ":")).encode("utf-8"),
                "application/json; charset=utf-8",
                method,
                {"Cache-Control": "no-store", "Clear-Site-Data": '"cache"'},
            )
        except UpdateBusyError as exc:
            self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)}, method)
        except (FnosServerError, OSError, tarfile.TarError, ValueError) as exc:
            logging.exception("UI update failed")
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)}, method)

    def _is_websocket_upgrade(self) -> bool:
        return self.headers.get("Upgrade", "").lower() == "websocket" and "upgrade" in self.headers.get("Connection", "").lower()

    def _proxy_websocket(self, parsed) -> None:
        upstream: socket.socket | None = None
        try:
            scheme, host, port, target = self._core_target(parsed)
            upstream = socket.create_connection((host, port), timeout=15)
            if scheme == "https":
                upstream = ssl.create_default_context().wrap_socket(upstream, server_hostname=host)
            headers = self._forward_headers(host, port, scheme)
            headers["Connection"] = "Upgrade"
            headers["Upgrade"] = "websocket"
            request = [f"GET {target} HTTP/1.1\r\n"]
            request.extend(f"{name}: {value}\r\n" for name, value in headers.items())
            request.append("\r\n")
            upstream.sendall("".join(request).encode("iso-8859-1"))

            handshake = bytearray()
            while b"\r\n\r\n" not in handshake:
                chunk = upstream.recv(4096)
                if not chunk:
                    raise FnosServerError("Mihomo closed the WebSocket handshake")
                handshake.extend(chunk)
                if len(handshake) > 64 * 1024:
                    raise FnosServerError("Mihomo WebSocket handshake is too large")
            status_line = bytes(handshake).split(b"\r\n", 1)[0]
            if b" 101 " not in status_line:
                raise FnosServerError(f"Mihomo rejected WebSocket: {status_line.decode('iso-8859-1', 'replace')}")
            self.connection.sendall(handshake)
            self.close_connection = True
            self._relay_sockets(self.connection, upstream)
        except (FnosServerError, OSError, ssl.SSLError) as exc:
            logging.warning("Mihomo WebSocket proxy failed: %s", exc)
            if not self.wfile.closed:
                with contextlib.suppress(OSError):
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "Mihomo WebSocket is unavailable"}, "GET")
        finally:
            if upstream is not None:
                with contextlib.suppress(OSError):
                    upstream.close()

    @staticmethod
    def _relay_sockets(client: socket.socket, upstream: socket.socket) -> None:
        selector = selectors.DefaultSelector()
        selector.register(client, selectors.EVENT_READ, upstream)
        selector.register(upstream, selectors.EVENT_READ, client)
        try:
            while selector.get_map():
                events = selector.select(timeout=60)
                if not events:
                    continue
                for key, _mask in events:
                    source: socket.socket = key.fileobj
                    target: socket.socket = key.data
                    data = source.recv(64 * 1024)
                    if not data:
                        selector.unregister(source)
                        with contextlib.suppress(OSError):
                            target.shutdown(socket.SHUT_WR)
                        continue
                    target.sendall(data)
        finally:
            selector.close()


def create_server(bind: str, port: int, state: AppState) -> FnosHTTPServer:
    return FnosHTTPServer((bind, port), state)


def configure_logging(log_file: Path | None) -> None:
    if log_file is None:
        handlers: list[logging.Handler] = [logging.StreamHandler()]
    else:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers = [logging.FileHandler(log_file, encoding="utf-8")]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9091)
    parser.add_argument("--bundled-ui", type=Path)
    parser.add_argument("--bundled-version-file", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--core-url-file", type=Path)
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--validate-core-url")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    if args.validate_core_url is not None:
        print(validate_core_url(args.validate_core_url))
        return 0
    missing = [
        name
        for name in ("bundled_ui", "bundled_version_file", "data_dir", "core_url_file")
        if getattr(args, name) is None
    ]
    if missing:
        build_argument_parser().error("missing required server arguments: " + ", ".join(missing))
    configure_logging(args.log_file)
    state = AppState(
        bundled_ui=args.bundled_ui,
        bundled_version_file=args.bundled_version_file,
        data_dir=args.data_dir,
        core_url_file=args.core_url_file,
    )
    seeded = state.seed_bundled_ui()
    logging.info(
        "starting fnOS adapter on %s:%s (UI %s%s, core %s)",
        args.bind,
        args.port,
        state.active_version,
        ", seeded" if seeded else "",
        state.core_url(),
    )
    server = create_server(args.bind, args.port, state)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
