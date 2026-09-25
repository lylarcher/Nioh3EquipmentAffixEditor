# -*- coding: utf-8 -*-
"""Verify the icon really ended up inside a built executable (stdlib only).

``pyinstaller ... icon=`` can fail silently (a missing file, an unsupported ICO
entry) and still produce a working exe with the default icon.  This script parses
the PE resource directory instead of trusting the build:

* finds ``RT_GROUP_ICON`` (14) and decodes ``GRPICONDIR`` -> the sizes Windows
  will offer (16/20/24/32/40/48/64/128/256, where 0 means 256);
* lists the ``RT_ICON`` (3) image blobs;
* optionally compares every blob byte-for-byte with the entries of a source
  ``.ico``, so "the shipped exe carries exactly the committed icon" is a fact and
  not an assumption.

Usage:
    python tools/check_exe_icon.py dist/Nioh3EquipmentAffixEditor.exe
    python tools/check_exe_icon.py --exe dist/x.exe --against assets/app.ico \\
        --expect 16,20,24,32,40,48,64,128,256 [--json]
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

RT_ICON = 3
RT_GROUP_ICON = 14


class PeError(ValueError):
    """Raised when the file is not a PE image or has no resource directory."""


def _read_u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _read_u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _sections(data: bytes) -> tuple[list[tuple[int, int, int]], int]:
    """Return ``([(virtual address, virtual size, raw offset)], resource_rva)``."""
    if data[:2] != b"MZ":
        raise PeError("不是 PE 文件（缺少 MZ 头）")
    pe_offset = _read_u32(data, 0x3C)
    if data[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise PeError("不是 PE 文件（缺少 PE 签名）")
    coff = pe_offset + 4
    number_of_sections = _read_u16(data, coff + 2)
    optional_size = _read_u16(data, coff + 16)
    optional = coff + 20
    magic = _read_u16(data, optional)
    if magic == 0x20B:  # PE32+
        directories_offset = optional + 112
    elif magic == 0x10B:  # PE32
        directories_offset = optional + 96
    else:
        raise PeError(f"未知的可选头 magic: 0x{magic:04X}")
    if optional_size < (directories_offset - optional) + 8 * 3:
        raise PeError("可选头中没有数据目录")
    resource_rva = _read_u32(data, directories_offset + 8 * 2)

    section_table = optional + optional_size
    sections = []
    for index in range(number_of_sections):
        base = section_table + index * 40
        virtual_size = _read_u32(data, base + 8)
        virtual_address = _read_u32(data, base + 12)
        raw_size = _read_u32(data, base + 16)
        raw_offset = _read_u32(data, base + 20)
        sections.append((virtual_address, max(virtual_size, raw_size), raw_offset))
    if resource_rva == 0:
        raise PeError("该 exe 没有资源目录（因此没有图标）")
    return sections, resource_rva


def _rva_to_offset(sections: list[tuple[int, int, int]], rva: int) -> int:
    for virtual_address, size, raw_offset in sections:
        if virtual_address <= rva < virtual_address + size:
            return raw_offset + (rva - virtual_address)
    raise PeError(f"RVA 0x{rva:X} 不属于任何节（无法定位图标数据）")


def _walk_resources(data: bytes, sections: list[tuple[int, int, int]],
                    resource_rva: int) -> dict[int, list[tuple[int, int, int]]]:
    """Return ``{type id: [(name id, data offset, data size), ...]}``."""
    base = _rva_to_offset(sections, resource_rva)

    def directory_entries(offset: int) -> list[tuple[int, int]]:
        named = _read_u16(data, offset + 12)
        ids = _read_u16(data, offset + 14)
        entries = []
        for index in range(named + ids):
            entry = offset + 16 + index * 8
            name = _read_u32(data, entry)
            child = _read_u32(data, entry + 4)
            entries.append((name, child))
        return entries

    found: dict[int, list[tuple[int, int, int]]] = {}
    for type_name, type_child in directory_entries(base):
        if type_name & 0x80000000:  # named type: not an icon
            continue
        type_offset = base + (type_child & 0x7FFFFFFF)
        for name, name_child in directory_entries(type_offset):
            if name & 0x80000000:
                continue
            language_offset = base + (name_child & 0x7FFFFFFF)
            for _language, entry_child in directory_entries(language_offset):
                entry_offset = base + (entry_child & 0x7FFFFFFF)
                data_rva = _read_u32(data, entry_offset)
                size = _read_u32(data, entry_offset + 4)
                found.setdefault(type_name, []).append(
                    (name, _rva_to_offset(sections, data_rva), size))
    return found


def icon_sizes_from_group(data: bytes) -> list[int]:
    """Decode a GRPICONDIR blob into the list of sizes it declares."""
    reserved, resource_type, count = struct.unpack_from("<HHH", data, 0)
    if resource_type != 1:
        raise PeError(f"GROUP_ICON 的类型不是 1（得到 {resource_type}）")
    sizes = []
    for index in range(count):
        width, height = struct.unpack_from("<BB", data, 6 + index * 14)
        sizes.append((width or 256, height or 256))
    return [width for width, _height in sizes]


def read_ico_entries(path: Path) -> list[tuple[int, int, bytes]]:
    """Parse a source ``.ico`` into ``[(size, image size, blob), ...]``."""
    data = path.read_bytes()
    reserved, image_type, count = struct.unpack_from("<HHH", data, 0)
    if (reserved, image_type) != (0, 1):
        raise PeError(f"{path.name} 不是图标文件")
    entries = []
    for index in range(count):
        width, height, _colors, _reserved, _planes, _bits, size, offset = \
            struct.unpack_from("<BBBBHHII", data, 6 + index * 16)
        entries.append((width or 256, size, data[offset:offset + size]))
    return entries


def inspect(exe: Path, against: Path | None = None) -> dict:
    data = exe.read_bytes()
    sections, resource_rva = _sections(data)
    resources = _walk_resources(data, sections, resource_rva)

    groups = resources.get(RT_GROUP_ICON, [])
    icons = resources.get(RT_ICON, [])
    if not groups:
        raise PeError("exe 里没有 RT_GROUP_ICON 资源（没有图标）")

    sizes: list[int] = []
    for _name, offset, size in groups:
        sizes.extend(icon_sizes_from_group(data[offset:offset + size]))
    # Several RT_GROUP_ICON resources (one per language) repeat the same sizes.
    unique_sizes = sorted(set(sizes))

    report = {
        "exe": str(exe),
        "group_icons": len(groups),
        "group_icon_entries": len(sizes),
        "icon_images": len(icons),
        "sizes": unique_sizes,
        "size_entries": sizes,
        "icon_bytes": sum(size for _name, _offset, size in icons),
    }

    if against is not None:
        expected = read_ico_entries(against)
        shipped = {name: data[offset:offset + size] for name, offset, size in icons}
        identical = 0
        mismatched: list[int] = []
        for index, (entry_size, blob_size, blob) in enumerate(expected, start=1):
            candidate = shipped.get(index)
            # PyInstaller numbers RT_ICON entries 1..N in ICO order.
            if candidate is not None and candidate == blob:
                identical += 1
            elif blob in shipped.values():
                identical += 1  # same pixels, different resource id
            else:
                mismatched.append(entry_size)
        report.update({
            "against": str(against),
            "expected_images": len(expected),
            "identical_images": identical,
            "mismatched_sizes": mismatched,
        })
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="校验 exe 内嵌图标")
    parser.add_argument("exe", nargs="?", help="要检查的 exe")
    parser.add_argument("--exe", dest="exe_option", help="同上（供 build.ps1 使用）")
    parser.add_argument("--against", default=None,
                        help="源 .ico：逐字节比对图标数据")
    parser.add_argument("--expect", default=None,
                        help="期望的尺寸列表，逗号分隔（如 16,32,256）")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = args.exe_option or args.exe
    if not target:
        print("错误: 需要指定 exe", file=sys.stderr)
        return 2

    exe = Path(target)
    if not exe.is_file():
        print(f"错误: 找不到 {exe}", file=sys.stderr)
        return 2

    problems: list[str] = []
    try:
        report = inspect(exe, Path(args.against) if args.against else None)
    except PeError as error:
        print(f"错误: {error}", file=sys.stderr)
        return 1

    if args.expect:
        wanted = sorted({int(value) for value in args.expect.split(",") if value.strip()})
        missing = [size for size in wanted if size not in report["sizes"]]
        unexpected = [size for size in report["sizes"] if size not in wanted]
        if missing:
            problems.append(f"exe 图标缺少尺寸: {missing}")
        if unexpected:
            problems.append(f"exe 图标多出尺寸: {unexpected}")
    if "mismatched_sizes" in report and report["mismatched_sizes"]:
        problems.append(f"图标数据与源 .ico 不一致的尺寸: {report['mismatched_sizes']}")

    if args.json:
        print(json.dumps({"report": report, "problems": problems},
                         ensure_ascii=False, indent=2))
    else:
        print(f"exe 图标: {len(report['sizes'])} 个尺寸 "
              f"{sorted(report['sizes'])}，RT_ICON 数据 {report['icon_bytes']} B")
        if "identical_images" in report:
            print(f"与 {Path(report['against']).name} 逐字节一致: "
                  f"{report['identical_images']}/{report['expected_images']}")
        for problem in problems:
            print(f"错误: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
