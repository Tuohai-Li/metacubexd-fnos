#!/usr/bin/env python3
"""Validate the fnOS package layout without requiring fnpack."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.fnos_server import normalize_version, validate_core_url, validate_ui_dir  # noqa: E402


REQUIRED = (
    "manifest",
    "app/fnos_server.py",
    "app/upstream-version",
    "app/ui/config",
    "app/www/index.html",
    "cmd/main",
    "cmd/config_callback",
    "config/resource",
    "config/privilege",
    "wizard/config",
)


def manifest_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^(\S+)\s*=\s*(.*?)\s*$", line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def git_modes(paths: list[str]) -> dict[str, str]:
    result = subprocess.run(
        ["git", "ls-files", "--stage", "--", *paths],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    modes: dict[str, str] = {}
    for line in result.stdout.splitlines():
        metadata, path = line.split("\t", 1)
        modes[path] = metadata.split()[0]
    return modes


def main() -> int:
    errors: list[str] = []
    for relative in REQUIRED:
        if not (ROOT / relative).exists():
            errors.append(f"missing required path: {relative}")

    manifest = manifest_values(ROOT / "manifest")
    expected_fields = {
        "appname": "metacubexd",
        "arch": "x86_64",
        "platform": "x86",
        "service_port": "9091",
        "desktop_applaunchname": "metacubexd.Application",
    }
    for name, expected in expected_fields.items():
        if manifest.get(name) != expected:
            errors.append(f"manifest {name} must be {expected!r}")
    try:
        upstream_version = normalize_version((ROOT / "app/upstream-version").read_text(encoding="utf-8"))
        manifest_version = manifest.get("version", "")
        version_match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:-fnos\.\d+)?", manifest_version)
        if version_match is None or normalize_version(version_match.group(1)) != upstream_version:
            errors.append("manifest base version and app/upstream-version differ")
    except ValueError as exc:
        errors.append(str(exc))

    try:
        validate_ui_dir(ROOT / "app/www")
    except Exception as exc:  # validation should report every package problem together
        errors.append(str(exc))

    for relative in ("app/ui/config", "config/resource", "config/privilege", "wizard/config"):
        try:
            json.loads((ROOT / relative).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid JSON in {relative}: {exc}")

    try:
        wizard = json.loads((ROOT / "wizard/config").read_text(encoding="utf-8"))
        default_url = wizard[0]["items"][0]["initValue"]
        if validate_core_url(default_url) != "http://127.0.0.1:9090":
            errors.append("wizard default must target the same-NAS core")
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        errors.append(f"invalid wizard Mihomo setting: {exc}")

    command_paths = [path.relative_to(ROOT).as_posix() for path in sorted((ROOT / "cmd").iterdir()) if path.is_file()]
    try:
        modes = git_modes(command_paths)
        for path in command_paths:
            if modes.get(path) != "100755":
                errors.append(f"{path} must be executable in Git (mode 100755)")
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        errors.append(f"cannot validate command modes: {exc}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"fnOS package structure is valid (MetaCubeXD {manifest['version']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
