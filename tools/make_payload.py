# -*- coding: utf-8 -*-
"""Build ``app-payload.zip``: the files a frozen build unpacks next to itself.

The executable is a **single file**, so everything the user is meant to see and
edit -- the parameter configuration, the affix catalogue, the bundled crypto
helper and the original source data -- travels inside it as one zip.  On first
run :mod:`nioh3_accessory_editor.bootstrap` writes those files into the same
relative directories they occupy in a checkout:

    data/accessory_affixes.json     词条库（合法词条，可自行查看）
    data/grace_affixes.json         恩宠/套装组合名表（仅用于显示槽位名称）
    config/editor.json              参数配置（改完后重启生效）
    assets/app.ico, logo*.png       程序图标与 logo（GUI 使用，可替换）
    bin/Nioh_Savefile_decrypt.exe   随附的加解密组件（可替换）
    third_party/source-data/...     原始数据（xlsx / CT，供重新生成词条库）
    readme.txt                      使用说明（给使用者看，中文）
README.md, README.zh-CN.md      开发者文档（英文 / 简体中文）
    CHANGELOG.md                    变更日志

Usage:
    python tools/make_payload.py [--output build/app-payload.zip] [--verify]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nioh3_accessory_editor import bootstrap
from nioh3_accessory_editor.config import default_config_document

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "build" / "app-payload.zip"

#: Directories copied verbatim (relative to the project root).
PAYLOAD_DIRECTORIES = ("assets", "bin", "data", "third_party")

#: Individual files copied verbatim.
PAYLOAD_FILES = ("readme.txt", "README.md", "README.zh-CN.md", "CHANGELOG.md")

EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".tmp", ".zip")
EXCLUDED_DIRECTORIES = ("__pycache__", ".git")

#: Fixed timestamp for reproducible archives (the zip epoch).
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _iter_directory(root: Path, name: str) -> list[Path]:
    base = root / name
    if not base.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        if any(part in EXCLUDED_DIRECTORIES for part in path.parts):
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        found.append(path)
    return found


def collect_payload_files(root: Path | None = None,
                          version: str = "", commit: str = "") -> dict[str, bytes]:
    """Return ``{relative name: contents}`` for everything shipped side by side."""
    root = Path(root) if root is not None else PROJECT_ROOT
    payload: dict[str, bytes] = {}

    for name in PAYLOAD_DIRECTORIES:
        for path in _iter_directory(root, name):
            payload[path.relative_to(root).as_posix()] = path.read_bytes()
    for name in PAYLOAD_FILES:
        path = root / name
        if path.is_file():
            payload[name] = path.read_bytes()

    document = default_config_document(version=version, commit=commit)
    payload["config/editor.json"] = (
        json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    return payload


def build_index(payload: dict[str, bytes], version: str, commit: str,
                created: str) -> dict:
    """Index written into the archive so extraction can verify its work."""
    return {
        "schema": bootstrap.PAYLOAD_SCHEMA,
        "component": "Nioh3AccessoryEditor",
        "version": version,
        "commit": commit,
        "created": created,
        "files": {
            name: {"size": len(data), "sha256": _sha256(data)}
            for name, data in sorted(payload.items())
        },
    }


def write_payload(output: Path, payload: dict[str, bytes], index: dict) -> Path:
    """Write a deterministic zip: sorted entries, fixed timestamps."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        entry = zipfile.ZipInfo(bootstrap.PAYLOAD_INDEX_NAME, _ZIP_TIMESTAMP)
        entry.compress_type = zipfile.ZIP_DEFLATED
        entry.external_attr = 0o644 << 16
        archive.writestr(entry, json.dumps(index, ensure_ascii=False,
                                           indent=2).encode("utf-8"))
        for name in sorted(payload):
            entry = zipfile.ZipInfo(name, _ZIP_TIMESTAMP)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, payload[name])
    return output


def verify_payload(path: Path) -> list[str]:
    """Re-read an archive and report every inconsistency found."""
    problems: list[str] = []
    with zipfile.ZipFile(path) as archive:
        try:
            index = json.loads(archive.read(bootstrap.PAYLOAD_INDEX_NAME))
        except KeyError:
            return [f"缺少 {bootstrap.PAYLOAD_INDEX_NAME}"]
        files = index.get("files", {})
        names = {n for n in archive.namelist()
                 if n != bootstrap.PAYLOAD_INDEX_NAME}
        for name, meta in sorted(files.items()):
            if name not in names:
                problems.append(f"索引中的文件在压缩包里不存在: {name}")
                continue
            data = archive.read(name)
            if len(data) != meta.get("size"):
                problems.append(f"大小不一致: {name}")
            if _sha256(data) != meta.get("sha256"):
                problems.append(f"SHA-256 不一致: {name}")
        for extra in sorted(names - set(files)):
            problems.append(f"压缩包里有索引未记录的文件: {extra}")
    return problems


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成随单文件 exe 内嵌的载荷 zip")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT),
                        help=f"输出路径（默认 {DEFAULT_OUTPUT}）")
    parser.add_argument("--root", default=str(PROJECT_ROOT), help="项目根目录")
    parser.add_argument("--version", default="", help="写入配置文件的版本号")
    parser.add_argument("--commit", default="", help="写入配置文件的 commit（后 8 位）")
    parser.add_argument("--created", default="", help="构建时间戳（ISO-8601）")
    parser.add_argument("--verify", action="store_true",
                        help="写完后重新读取并校验（含未记录的额外文件）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出摘要")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = collect_payload_files(Path(args.root), args.version, args.commit)
    if not payload:
        print("错误：没有可打包的文件", file=sys.stderr)
        return 1
    index = build_index(payload, args.version, args.commit, args.created)
    output = write_payload(Path(args.output), payload, index)

    problems = verify_payload(output) if args.verify else []
    total = sum(len(data) for data in payload.values())
    if args.json:
        print(json.dumps({
            "output": str(output),
            "files": len(payload),
            "bytes": output.stat().st_size,
            "raw_bytes": total,
            "entries": sorted(payload),
            "problems": problems,
        }, ensure_ascii=False, indent=2))
    elif not args.quiet:
        print(f"OK: 载荷已生成 {output}")
        print(f"    文件 {len(payload)} 个，原始 {total / 1024:.0f} KB，"
              f"压缩后 {output.stat().st_size / 1024:.0f} KB")
        for name in sorted(payload):
            print(f"    - {name}")
        for problem in problems:
            print(f"    校验问题: {problem}", file=sys.stderr)
    if problems:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
