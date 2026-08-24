#!/usr/bin/env python3
"""Synchronize the vendored UI with a verified MetaCubeXD stable release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.fnos_server import (  # noqa: E402
    FnosServerError,
    download_release_asset,
    fetch_release,
    normalize_version,
    release_asset,
    safe_extract_ui,
    validate_ui_dir,
    write_text_atomic,
)


def tree_manifest(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise FnosServerError(f"unexpected symlink in UI tree: {path}")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(64 * 1024), b""):
                    digest.update(chunk)
            files[relative] = digest.hexdigest()
    return files


def replace_tree(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    incoming = destination.parent / f".{destination.name}-incoming-{uuid.uuid4().hex}"
    backup = destination.parent / f".{destination.name}-backup-{uuid.uuid4().hex}"
    shutil.copytree(source, incoming)
    moved_old = False
    try:
        if destination.exists():
            os.replace(destination, backup)
            moved_old = True
        os.replace(incoming, destination)
    except BaseException:
        if moved_old and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    finally:
        if incoming.exists():
            shutil.rmtree(incoming)
        if backup.exists():
            shutil.rmtree(backup)


def update_manifest(path: Path, version: str) -> bool:
    original = path.read_text(encoding="utf-8")
    current = re.search(r"(?m)^version\s*=\s*(\S+)\s*$", original)
    if current and (current.group(1) == version or current.group(1).startswith(version + "-fnos.")):
        return False
    updated, count = re.subn(
        r"(?m)^version(\s*)=\s*\S+\s*$",
        lambda match: f"version{match.group(1)}= {version}",
        original,
        count=1,
    )
    if count != 1:
        raise FnosServerError("manifest does not contain exactly one version field")
    if updated == original:
        return False
    write_text_atomic(path, updated)
    return True


def sync_repository(
    root: Path,
    *,
    version: str | None = None,
    api_base: str | None = None,
) -> dict[str, str | bool]:
    api_kwargs = {"api_base": api_base} if api_base else {}
    release = fetch_release(version=version, **api_kwargs)
    normalized = normalize_version(str(release["tag_name"]))
    asset = release_asset(release)

    app_dir = root / "app"
    destination = app_dir / "www"
    version_file = app_dir / "upstream-version"
    manifest_file = root / "manifest"

    with tempfile.TemporaryDirectory(prefix="metacubexd-sync-") as temporary:
        temporary_root = Path(temporary)
        archive = temporary_root / "compressed-dist.tgz"
        extracted = temporary_root / "extracted"
        digest = download_release_asset(asset, archive)
        safe_extract_ui(archive, extracted)
        validate_ui_dir(extracted)
        ui_changed = not destination.exists() or tree_manifest(destination) != tree_manifest(extracted)
        if ui_changed:
            replace_tree(extracted, destination)

    previous_version = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else ""
    version_changed = previous_version != normalized
    if version_changed:
        write_text_atomic(version_file, normalized + "\n")
    manifest_changed = update_manifest(manifest_file, normalized)
    changed = ui_changed or version_changed or manifest_changed
    return {
        "changed": changed,
        "ui_changed": ui_changed,
        "version": normalized,
        "tag": f"v{normalized}",
        "digest": digest,
        "release_url": str(release.get("html_url") or ""),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--version", help="stable version/tag; defaults to latest")
    parser.add_argument("--api-base", help=argparse.SUPPRESS)
    parser.add_argument("--github-output", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = sync_repository(
        args.root.resolve(),
        version=args.version,
        api_base=args.api_base,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            for key in ("changed", "version", "tag", "digest", "release_url"):
                output.write(f"{key}={str(result[key]).lower() if isinstance(result[key], bool) else result[key]}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
