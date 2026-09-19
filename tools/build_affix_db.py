# -*- coding: utf-8 -*-
"""Build data/accessory_affixes.json from the 仁王3词条装备库 xlsx.

Usage:
    python tools/build_affix_db.py [path-to-xlsx]

The workbook's 饰品词条 sheet has columns:
    A=类别  B=词条代码 (12 bytes as space-separated hex)
    C=词条名称  D=备注

The parser is stdlib-only (xlsx = zip + XML).  It accepts either the real
xlsx or the pre-dumped TSV form (A=../B=.. lines) to stay robust.
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nioh3_accessory_editor.affixdb import AffixEntry, save_catalog

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

DEFAULT_SOURCE = Path(r"D:\AIWorkspace\DSHWorkSpcae\Nioh3Trainer\仁王3词条装备库v2.21.xlsx")
TARGET_SHEET = "饰品词条"


def parse_code(code: str) -> tuple[int, int, int]:
    """Decode the 12-byte 词条代码 into (effect_id, value, flags).

    Layout (consistent with the CT equipment metadata byte structure):
    * bytes 0..3  — effect id (u32 LE)
    * bytes 4..7  — value (u32 LE)
    * byte  8     — reserved
    * byte  9     — metadata byte 1: bits0-5 category, bit6 fixed, bit7 改造
    * byte  10    — metadata byte 2: bit2 star, bit3 hammer(髓材)
    * byte  11    — reserved

    ``flags`` packs the fixed bit (0x40) and the star bit (0x04).
    """
    parts = code.replace(",", " ").split()
    if len(parts) != 12:
        raise ValueError(f"词条代码必须是 12 字节，实际 {len(parts)}: {code!r}")
    raw = bytes(int(p, 16) for p in parts)
    effect_id = int.from_bytes(raw[0:4], "little")
    value = int.from_bytes(raw[4:8], "little")
    flags = (raw[9] & 0x40) | (raw[10] & 0x04)
    return effect_id, value, flags


def _rows_from_xlsx(path: Path) -> list[list[str]]:
    z = zipfile.ZipFile(path)
    names = z.namelist()

    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si"):
            shared.append("".join(t.text or "" for t in si.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")))

    wb = ET.fromstring(z.read("xl/workbook.xml"))
    target_file: str | None = None
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rel_map = {
        rel.get("Id"): rel.get("Target")
        for rel in rels.iter(
            "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
        )
    }
    for sheet in wb.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet"):
        if sheet.get("name") == TARGET_SHEET:
            target = rel_map.get(sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"))
            if target:
                if not target.startswith("xl/"):
                    target = "xl/" + target.lstrip("/")
                target_file = target
            break
    if target_file is None or target_file not in names:
        raise FileNotFoundError(f"找不到工作表 {TARGET_SHEET} 或文件 {target_file}")

    root = ET.fromstring(z.read(target_file))
    rows: list[list[str]] = []
    for row in root.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"):
        cells: list[str] = []
        for c in row.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"):
            t = c.get("t") or "n"
            v = c.find("m:v", NS)
            isel = c.find("m:is", NS)
            val = ""
            if t == "s" and v is not None and v.text:
                val = shared[int(v.text)] if int(v.text) < len(shared) else ""
            elif t == "inlineStr" and isel is not None:
                val = "".join(x.text or "" for x in isel.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"))
            elif v is not None and v.text is not None:
                val = v.text
            cells.append(val)
        if cells:
            rows.append(cells)
    return rows


def _rows_from_tsv(path: Path) -> list[list[str]]:
    """Parse the dump format 'A1=类别\tB1=词条代码...' into per-row cells."""
    rows: list[list[str]] = []
    current: dict[int, str] = {}
    current_row = 1
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z]+)(\d+)=(.*)$", line)
        if not m:
            continue
        col, row_num, val = m.group(1), int(m.group(2)), m.group(3)
        if row_num != current_row:
            rows.append([current.get(i, "") for i in range(max(current) + 1)])
            current = {}
            current_row = row_num
        current[ord(col) - ord("A")] = val
    if current:
        rows.append([current.get(i, "") for i in range(max(current) + 1)])
    return rows


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    dry_run = "--dry-run" in sys.argv[1:]
    source = Path(args[0]) if args else DEFAULT_SOURCE
    if not source.is_file():
        print(f"错误：找不到词条来源文件 {source}")
        return 1
    if source.suffix.lower() == ".tsv":
        rows = _rows_from_tsv(source)
    else:
        rows = _rows_from_xlsx(source)

    entries: list[AffixEntry] = []
    by_id: dict[int, AffixEntry] = {}
    conflicts: list[str] = []
    skipped_rows = 0
    for row in rows[1:]:  # skip header
        if len(row) < 3:
            skipped_rows += 1
            continue
        category, code, name = row[0].strip(), row[1].strip(), row[2].strip()
        if not code or not re.fullmatch(r"([0-9A-Fa-f]{2} ){11}[0-9A-Fa-f]{2}", code):
            skipped_rows += 1
            continue
        if not name:
            skipped_rows += 1
            continue
        effect_id, value, flags = parse_code(code)
        if effect_id == 0xFFFFFFFF:
            skipped_rows += 1
            continue
        entry = AffixEntry(
            effect_id=effect_id, value=value, flags=flags,
            name=name, category=category,
        )
        previous = by_id.get(effect_id)
        if previous is not None:
            # The editor resolves edits by id, so an id must map to one entry.
            if (previous.value, previous.flags) != (value, flags):
                conflicts.append(
                    f"{effect_id:#06x}: 保留 {previous.name!r}(数值={previous.value},"
                    f"标识={previous.flags:#04x})，忽略 {name!r}(数值={value},"
                    f"标识={flags:#04x})"
                )
            else:
                skipped_rows += 1
            continue
        by_id[effect_id] = entry
        entries.append(entry)

    if not entries:
        print("错误：未解析到任何词条")
        return 1

    if dry_run:
        print(f"演练：将生成 {len(entries)} 条词条（未写入）")
    else:
        save_catalog(entries, source=f"{source.name} / {TARGET_SHEET}",
                     conflicts=conflicts)
        print(f"OK: 已生成词条库，共 {len(entries)} 条饰品合法词条")
    print("    来源:", source)
    print(f"    跳过行: {skipped_rows}")
    if conflicts:
        print(f"    同一词条 ID 多值冲突: {len(conflicts)} 处（已保留首个）")
        for item in conflicts[:10]:
            print("      -", item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
