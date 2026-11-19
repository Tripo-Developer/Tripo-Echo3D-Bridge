"""Validate Echo3D release packages without building or uploading."""

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "Tripo3d_echo3d_Bridge"
MAC_APP = "Tripo-Echo3D-Bridge"
MAC_DMGS = (
    f"{MAC_APP}-macOS-arm64.dmg",
    f"{MAC_APP}-macOS-x86_64.dmg",
)
MAC_README = "README.md"
sys.path.insert(0, str(ROOT))

from echo3d_bridge import auto_update as update  # noqa: E402


def source_record():
    path = ROOT / "echo3d_bridge" / "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
    ]
    if len(values) != 1 or not isinstance(values[0], str):
        raise ValueError(f"expected one __version__ in {path}")
    update.version(values[0])
    return values[0]


def source_tag():
    return "v" + source_record()


def check_source(tag):
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag or ""):
        raise ValueError("Expected a stable tag such as v1.1.0")
    expected = source_tag()
    if update.version(tag) != update.version(source_record()):
        raise ValueError(f"Release tag {tag} does not match __version__ {expected}")
    if tag != expected:
        raise ValueError(f"Release tag {tag} does not match __version__ {expected}")


def validate(archive, tag):
    check_source(tag)
    update.archive_info(archive, tag)
    with zipfile.ZipFile(archive) as package:
        if package.testzip() is not None:
            raise ValueError("Package CRC check failed")


def pack(exe, tag, destination):
    check_source(tag)
    exe = Path(exe)
    if not exe.is_file():
        raise ValueError(f"missing executable: {exe}")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / f"{PACKAGE}-{tag}.zip"
    payload = f'__version__ = "{update.version_text(tag)}"\n'
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.write(exe, update.EXE_NAME)
        package.writestr(update.VERSION_NAME, payload)
    validate(archive, tag)
    return archive


def mac_archive_name(tag):
    check_source(tag)
    return f"{PACKAGE}-mac-{tag}.zip"


def _require_mac_package(archive):
    archive = Path(archive)
    if not archive.is_file():
        raise ValueError(f"missing mac package: {archive}")
    with zipfile.ZipFile(archive) as package:
        if package.testzip() is not None:
            raise ValueError("Package CRC check failed")
        names = {name.rstrip("/") for name in package.namelist() if name.rstrip("/")}
        for name in (*MAC_DMGS, MAC_README):
            if name not in names:
                raise ValueError(f"mac package is missing {name}")
            if package.getinfo(name).file_size <= 0:
                raise ValueError(f"mac package entry is empty: {name}")
    return archive


def stage_mac(archive, tag, destination):
    archive = _require_mac_package(archive)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / mac_archive_name(tag)
    if archive.resolve() != target.resolve():
        shutil.copyfile(archive, target)
    return target


def manifest_for(archive, tag, url):
    validate(archive, tag)
    digest = hashlib.sha256()
    with Path(archive).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    manifest = {
        "schema_version": 1,
        "id": update.PLUGIN_ID,
        "version": update.version_text(tag),
        "channel": update.CHANNEL,
        "packages": {"default": {"url": update.safe_url(url), "sha256": digest.hexdigest()}},
    }
    resolved = update.resolve(manifest, "0.0.0")
    if resolved["latest"] != manifest["version"] or resolved["sha256"] != digest.hexdigest() or resolved["url"] != url:
        raise ValueError("manifest did not resolve to the packaged release")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("version", "tag", "check", "pack", "stage-mac", "manifest"))
    parser.add_argument("--exe", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--url")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "version":
        print(source_record())
        return
    if args.command == "tag":
        print(source_tag())
        return
    tag = os.environ["RELEASE_TAG"]
    if args.command == "check":
        check_source(tag)
        print(tag)
        return
    if args.command == "pack":
        if args.exe is None:
            parser.error("pack requires --exe")
        print(pack(args.exe, tag, ROOT / "release-assets"))
        return
    if args.command == "stage-mac":
        if args.archive is None:
            parser.error("stage-mac requires --archive")
        print(stage_mac(args.archive, tag, ROOT / "release-assets"))
        return
    if not all([args.archive, args.url, args.output]):
        parser.error("manifest requires --archive, --url and --output")
    result = manifest_for(args.archive, tag, args.url)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
