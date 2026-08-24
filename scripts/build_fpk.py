#!/usr/bin/env python3
"""Build a deterministic fnOS FPK on systems without the fnpack CLI."""

from __future__ import annotations

import argparse
import gzip
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTER_PATHS = ("LICENSE", "cmd", "config", "ICON.PNG", "ICON_256.PNG", "manifest", "wizard")
IGNORED_NAMES = {"__pycache__", ".DS_Store"}


def _manifest_value(name: str) -> str:
    content = (ROOT / "manifest").read_text(encoding="utf-8")
    match = re.search(rf"(?m)^{re.escape(name)}\s*=\s*(\S+)\s*$", content)
    if match is None:
        raise RuntimeError(f"manifest is missing {name}")
    return match.group(1)


def _archive_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in IGNORED_NAMES for part in relative.parts) or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise RuntimeError(f"symbolic links are not allowed in FPK input: {path}")
        paths.append(path)
    return sorted(paths, key=lambda item: item.relative_to(root).as_posix())


def _tar_filter(info: tarfile.TarInfo, *, executable: bool, epoch: int) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = epoch
    info.mode = 0o755 if info.isdir() or executable else 0o644
    return info


def _add_tree(archive: tarfile.TarFile, source: Path, archive_root: str, epoch: int) -> None:
    if source.is_dir():
        archive.add(
            source,
            arcname=archive_root,
            recursive=False,
            filter=lambda info: _tar_filter(info, executable=False, epoch=epoch),
        )
        for path in _archive_paths(source):
            relative = path.relative_to(source).as_posix()
            arcname = f"{archive_root}/{relative}" if archive_root else relative
            executable = arcname.startswith("cmd/") and path.is_file()
            archive.add(
                path,
                arcname=arcname,
                recursive=False,
                filter=lambda info, executable=executable: _tar_filter(
                    info, executable=executable, epoch=epoch
                ),
            )
    else:
        executable = archive_root.startswith("cmd/")
        archive.add(
            source,
            arcname=archive_root,
            recursive=False,
            filter=lambda info: _tar_filter(info, executable=executable, epoch=epoch),
        )


def _write_inner_app(destination: Path, epoch: int) -> None:
    with destination.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for path in _archive_paths(ROOT / "app"):
                    relative = path.relative_to(ROOT / "app").as_posix()
                    executable = relative == "fnos_server.py"
                    archive.add(
                        path,
                        arcname=relative,
                        recursive=False,
                        filter=lambda info, executable=executable: _tar_filter(
                            info, executable=executable, epoch=epoch
                        ),
                    )


def build_fpk(destination: Path) -> Path:
    subprocess.run([sys.executable, str(ROOT / "scripts/validate_package.py")], cwd=ROOT, check=True)
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = destination.with_name(f".{destination.name}.tmp")
    with tempfile.TemporaryDirectory(prefix="metacubexd-fpk-") as temporary:
        app_archive = Path(temporary) / "app.tgz"
        _write_inner_app(app_archive, epoch)
        try:
            with tarfile.open(temporary_output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                archive.add(
                    app_archive,
                    arcname="app.tgz",
                    recursive=False,
                    filter=lambda info: _tar_filter(info, executable=False, epoch=epoch),
                )
                for relative in OUTER_PATHS:
                    _add_tree(archive, ROOT / relative, relative, epoch)
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
    args = parser.parse_args()
    print(build_fpk(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
