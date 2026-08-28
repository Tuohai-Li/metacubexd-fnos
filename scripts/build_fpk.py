#!/usr/bin/env python3
"""Build and audit an installable fnOS FPK with the official fnpack tool."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
FNPACK_VERSION = "1.2.3"
MAX_FNPACK_BYTES = 16 * 1024 * 1024
PROJECT_PATHS = ("app", "cmd", "config", "wizard", "manifest", "ICON.PNG", "ICON_256.PNG", "LICENSE")
IGNORED_NAMES = {"__pycache__", ".DS_Store"}

# Official download URLs. SHA-256 values are pinned so a changed download is
# never executed silently.
FNPACK_DOWNLOADS = {
    ("windows", "amd64"): (
        "https://static2.fnnas.com/fnpack/fnpack-1.2.3-windows-amd64",
        "d7af4bd716b009c58f5bcd931615f39db121e7d4b75dc759e575c4fb2879b6ee",
        "fnpack.exe",
    ),
    ("linux", "amd64"): (
        "https://static2.fnnas.com/fnpack/fnpack-1.2.3-linux-amd64",
        "54b97fa7b70968c4d05c79840f5daeff508957d0bb2062fdb0376d00d9615c93",
        "fnpack",
    ),
    ("darwin", "amd64"): (
        "https://static2.fnnas.com/fnpack/fnpack-1.2.3-darwin-amd64",
        "30a9f50a35e8d8d425b687881761478c3c778e9c0da3a1b59f298b666dd7a268",
        "fnpack",
    ),
    ("darwin", "arm64"): (
        "https://static2.fnnas.com/fnpack/fnpack-1.2.3-darwin-arm64",
        "d40cb00896cb2a5d211357d255750ed0cbe7f2d141df671c2b717afb4e74bf77",
        "fnpack",
    ),
}


class FpkBuildError(RuntimeError):
    """Raised when fnpack or the resulting archive violates the known format."""


def _manifest_value(name: str) -> str:
    content = (ROOT / "manifest").read_text(encoding="utf-8")
    match = re.search(rf"(?m)^{re.escape(name)}\s*=\s*(\S+)\s*$", content)
    if match is None:
        raise FpkBuildError(f"manifest is missing {name}")
    return match.group(1)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _host_key() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        machine = "amd64"
    elif machine in {"arm64", "aarch64"}:
        machine = "arm64"
    return system, machine


def _download_verified_fnpack() -> Path:
    key = _host_key()
    if key not in FNPACK_DOWNLOADS:
        raise FpkBuildError(f"no pinned fnpack {FNPACK_VERSION} download for {key[0]}/{key[1]}")
    url, expected_digest, filename = FNPACK_DOWNLOADS[key]
    cache = Path(tempfile.gettempdir()) / "metacubexd-fnos-fnpack" / FNPACK_VERSION / f"{key[0]}-{key[1]}"
    destination = cache / filename
    if destination.is_file() and _sha256(destination) == expected_digest:
        return destination

    cache.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{filename}.{uuid.uuid4().hex}.tmp")
    digest = hashlib.sha256()
    size = 0
    try:
        request = Request(url, headers={"User-Agent": "metacubexd-fnos-fpk-builder/1"})
        with urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            while True:
                chunk = response.read(128 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_FNPACK_BYTES:
                    raise FpkBuildError("official fnpack download exceeded the safety limit")
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != expected_digest:
            raise FpkBuildError("official fnpack SHA-256 verification failed")
        temporary.chmod(0o755)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def resolve_fnpack(explicit: Path | None, *, allow_download: bool = True) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    configured = os.environ.get("FNPACK_BIN", "").strip()
    if configured:
        candidates.append(Path(configured))
    discovered = shutil.which("fnpack")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_file():
            return resolved
    if allow_download:
        return _download_verified_fnpack()
    raise FpkBuildError("fnpack was not found; pass --fnpack or allow the verified official download")


def _safe_archive_name(name: str) -> str:
    normalized = name.replace("\\", "/").rstrip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise FpkBuildError(f"unsafe archive member: {name!r}")
    return normalized


def _members_by_name(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    for member in archive.getmembers():
        name = _safe_archive_name(member.name)
        if member.issym() or member.islnk():
            raise FpkBuildError(f"archive links are not allowed: {name}")
        if name in members:
            raise FpkBuildError(f"duplicate archive member: {name}")
        members[name] = member
    return members


def audit_fpk(path: Path) -> dict[str, str | int]:
    """Validate observed fnpack 1.2.3 invariants and required app content."""

    try:
        with path.open("rb") as source:
            if source.read(2) != b"\x1f\x8b":
                raise FpkBuildError("FPK outer archive is not gzip-compressed")
        with tarfile.open(path, mode="r:gz") as outer:
            outer_members = _members_by_name(outer)
            required_outer = {
                "app.tgz",
                "manifest",
                "cmd/main",
                "config/privilege",
                "config/resource",
                "wizard/config",
                "ICON.PNG",
                "ICON_256.PNG",
            }
            missing = sorted(required_outer.difference(outer_members))
            if missing:
                raise FpkBuildError("FPK is missing required entries: " + ", ".join(missing))
            manifest_stream = outer.extractfile(outer_members["manifest"])
            app_stream = outer.extractfile(outer_members["app.tgz"])
            if manifest_stream is None or app_stream is None:
                raise FpkBuildError("FPK manifest or app.tgz is not a regular file")
            manifest = manifest_stream.read().decode("utf-8")
            app_archive = app_stream.read()
    except (OSError, tarfile.TarError, UnicodeDecodeError) as exc:
        raise FpkBuildError("FPK archive could not be read") from exc

    checksum_match = re.search(r"(?m)^checksum\s*=\s*([0-9a-fA-F]{32})\s*$", manifest)
    if checksum_match is None:
        raise FpkBuildError("FPK manifest has no valid app.tgz MD5 checksum")
    app_md5 = hashlib.md5(app_archive).hexdigest()  # noqa: S324 - fnpack format requires MD5
    if checksum_match.group(1).lower() != app_md5:
        raise FpkBuildError("FPK manifest checksum does not match app.tgz")

    try:
        with tarfile.open(fileobj=io.BytesIO(app_archive), mode="r:gz") as inner:
            inner_members = _members_by_name(inner)
    except tarfile.TarError as exc:
        raise FpkBuildError("FPK app.tgz could not be read") from exc
    required_inner = {"fnos_server.py", "upstream-version", "ui/config", "www/index.html"}
    missing_inner = sorted(required_inner.difference(inner_members))
    if missing_inner:
        raise FpkBuildError("app.tgz is missing required entries: " + ", ".join(missing_inner))

    return {
        "size": path.stat().st_size,
        "sha256": _sha256(path),
        "app_md5": app_md5,
        "outer_entries": len(outer_members),
        "inner_entries": len(inner_members),
    }


def _copy_project(destination: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "*.fpk")
    for name in PROJECT_PATHS:
        source = ROOT / name
        target = destination / name
        if source.is_dir():
            shutil.copytree(source, target, ignore=ignore)
        elif source.is_file():
            shutil.copy2(source, target)
        elif name != "LICENSE":
            raise FpkBuildError(f"package input is missing: {name}")


def build_fpk(destination: Path, *, fnpack: Path | None = None, allow_download: bool = True) -> Path:
    subprocess.run([sys.executable, str(ROOT / "scripts/validate_package.py")], cwd=ROOT, check=True)
    tool = resolve_fnpack(fnpack, allow_download=allow_download)
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    with tempfile.TemporaryDirectory(prefix="metacubexd-fpk-") as temporary:
        build_root = Path(temporary)
        project = build_root / "project"
        project.mkdir()
        _copy_project(project)
        subprocess.run([str(tool), "build", "--directory", str(project)], cwd=build_root, check=True)
        generated = build_root / f"{_manifest_value('appname')}.fpk"
        if not generated.is_file():
            raise FpkBuildError("fnpack reported success but produced no FPK")
        audit_fpk(generated)
        try:
            shutil.copyfile(generated, temporary_output)
            audit_fpk(temporary_output)
            os.replace(temporary_output, destination)
        finally:
            temporary_output.unlink(missing_ok=True)
    return destination


def main() -> int:
    version = _manifest_value("version")
    arch = _manifest_value("arch")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / f"metacubexd-{version}-{arch}.fpk",
        help="output FPK path",
    )
    parser.add_argument("--fnpack", type=Path, help="path to an official fnpack binary")
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="fail instead of downloading a missing pinned official fnpack",
    )
    args = parser.parse_args()
    output = build_fpk(args.output, fnpack=args.fnpack, allow_download=not args.no_download)
    audit = audit_fpk(output)
    print(output)
    print(f"SHA-256: {audit['sha256']}")
    print(f"app.tgz MD5: {audit['app_md5']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
