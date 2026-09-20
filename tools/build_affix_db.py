# -*- coding: utf-8 -*-
"""Build the shipped JSON catalogs from the 仁王3词条装备库 xlsx.

Usage:
    python tools/build_affix_db.py [path-to-xlsx]

Three files are written:

* ``data/accessory_affixes.json`` from the 饰品词条 sheet — the **legal edit**
  catalog.  Columns: A=类别  B=词条代码 (12 bytes as space-separated hex)
  C=词条名称  D=备注.
* ``data/grace_affixes.json`` from the 词条总目录 sheet — the 恩宠/套装组合
  **name table** (display only; 归属 = [恩宠] / [上位恩宠] / [武士套装] /
  [忍者套装]).  Every real accessory ends with one of these, and they must never
  enter the legal catalog because armour carries 套装 codes as well.
* ``data/accessory_items.json`` from the 物品总目录 sheet — the 饰品 rows, i.e.
  what an accessory **is** (种类), keyed by the per-item id a v2.21 record header
  carries.  Display only as well: those ids are a different space from 词条 ids
  and never authorise a write.

The parser is stdlib-only (xlsx = zip + XML).  It accepts either the real
xlsx or the pre-dumped TSV form (A=../B=.. lines) to stay robust; the TSV form
only has the 饰品词条 sheet, so it writes just the accessory catalog.
"""

from __future__ import annotations

import re
import sys
import zipfile
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nioh3_accessory_editor.affixdb import (
    AffixEntry,
    ItemEntry,
    save_catalog,
    save_grace_catalog,
    save_item_catalog,
    save_soul_catalog,
    save_soul_item_catalog,
)

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: Bundled copy of the source workbook (see third_party/source-data/README.md).
#: The legacy location is kept as a fallback so an older checkout layout still
#: works; the bundled copy wins whenever it exists.  The legacy directory
#: (``Nioh3Trainer``) was removed from the workspace, so this path normally just
#: documents where the data used to live.
DEFAULT_SOURCE = (
    PROJECT_ROOT / "third_party" / "source-data" / "仁王3词条装备库v2.21.xlsx"
)
LEGACY_SOURCES = (
    Path(r"D:\AIWorkspace\DSHWorkSpcae\Nioh3Trainer\仁王3词条装备库v2.21.xlsx"),
)
TARGET_SHEET = "饰品词条"

#: 恩宠 / 套装组合 names live in the 词条总目录 sheet (columns: 词条代码 / 词条名称 /
#: 词条归属).  Every real accessory's last affix is one of these; they are *not*
#: accessory affixes (armour carries 套装 codes too), so they go into a separate,
#: display-only table instead of the legal-edit catalog.
GRACE_SHEET = "词条总目录"
GRACE_OWNERS = ("恩宠", "上位恩宠", "武士套装", "忍者套装")

#: What an accessory *is* lives in the 物品总目录 sheet (大类 / 中类 / 小类 / 代码 /
#: 名称 / 备注).  Only the 饰品 rows are shipped: a v2.21 record header carries the
#: per-item id (mirrored), and of the reporting user's 213 accessories 212 resolve
#: to one of these 88 ids — measured, not assumed.  Display only.
ITEMS_SHEET = "物品总目录"
ITEM_BIG_CLASS = "饰品"
#: 物品代码 in the sheet lists the low byte first ("BB F5" == 0xF5BB).
ITEM_CODE_PATTERN = re.compile(r"^(?:0x)?([0-9A-Fa-f]{2})\s*([0-9A-Fa-f]{2})$")

#: 魂核 (soul cores) have their own affix pool in the 绘卷-魂核词条 sheet, whose
#: 种类 column is 绘卷 or 魂核; measured on the reporting user's save, 132 of the
#: 134 records that look like 魂核 carry exactly the 魂核 sheet's 固定词条代码
#: (0 mismatches), which is why that sheet — not 饰品词条 — gates 魂核 edits.
SOUL_AFFIX_SHEET = "绘卷-魂核词条"
SOUL_AFFIX_KIND = "魂核"
#: The workbook's value table (sheet name is truncated by Excel's 31-char limit).
#: Its 取值集合 column gives each affix's legal value span; a prefix match is used
#: because the sheet name is not stable across workbook revisions.
VALUE_SHEET_PREFIX = "全词条数值*"
#: 魂核 rows of 物品总目录 (大类 = 魂核): the id a 魂核 record's header carries.
SOUL_ITEM_BIG_CLASS = "魂核"


def resolve_source(explicit: str | Path | None = None) -> Path:
    """Pick the source workbook: explicit path > bundled copy > legacy path."""
    if explicit is not None:
        return Path(explicit)
    if DEFAULT_SOURCE.is_file():
        return DEFAULT_SOURCE
    for candidate in LEGACY_SOURCES:
        if candidate.is_file():
            return candidate
    return DEFAULT_SOURCE  # missing: the caller reports it with a hint


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


def _rows_from_xlsx(path: Path, sheet_name: str = TARGET_SHEET) -> list[list[str]]:
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
        name = sheet.get("name") or ""
        if name == sheet_name or (sheet_name.endswith("*")
                                  and name.startswith(sheet_name[:-1])):
            target = rel_map.get(sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"))
            if target:
                if not target.startswith("xl/"):
                    target = "xl/" + target.lstrip("/")
                target_file = target
            break
    if target_file is None or target_file not in names:
        raise FileNotFoundError(f"找不到工作表 {sheet_name} 或文件 {target_file}")

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


def parse_grace_owner(owner: str) -> str | None:
    """``[上位恩宠]`` -> ``上位恩宠``; ``None`` when it is not a grace/set row."""
    text = owner.strip().strip("[]").strip()
    return text if text in GRACE_OWNERS else None


def collect_grace_entries(source: Path) -> tuple[list[AffixEntry], int]:
    """Read the 恩宠/套装 rows of 词条总目录 as display-only name entries."""
    rows = _rows_from_xlsx(source, GRACE_SHEET)
    entries: list[AffixEntry] = []
    by_id: dict[int, AffixEntry] = {}
    skipped = 0
    for row in rows[1:]:  # skip header
        if len(row) < 3:
            skipped += 1
            continue
        code, name, owner = row[0].strip(), row[1].strip(), row[2].strip()
        kind = parse_grace_owner(owner)
        if kind is None or not name:
            skipped += 1
            continue
        if not re.fullmatch(r"([0-9A-Fa-f]{2} ){11}[0-9A-Fa-f]{2}", code):
            skipped += 1
            continue
        effect_id, value, flags = parse_code(code)
        if effect_id == 0xFFFFFFFF or effect_id in by_id:
            skipped += 1
            continue
        entry = AffixEntry(
            effect_id=effect_id, value=value, flags=flags, name=name, category=kind,
        )
        by_id[effect_id] = entry
        entries.append(entry)
    return entries, skipped


def parse_item_code(code: str) -> int | None:
    """``BB F5`` -> ``0xF5BB``; ``None`` when the cell is not a 2-byte item code.

    The workbook writes the low byte first, so the value has to be swapped --
    this is what makes a save's ``+0x00`` field resolve to e.g. 凶王耳饰[忍者].
    """
    match = ITEM_CODE_PATTERN.match((code or "").strip())
    if match is None:
        return None
    low, high = match.groups()
    return int(high + low, 16)


def collect_item_entries(source: Path) -> tuple[list[ItemEntry], list[str], int]:
    """Read the 饰品 rows of 物品总目录 as display-only item entries.

    Returns ``(entries, conflicts, skipped)``; a conflict records an id that the
    workbook gives two names (e.g. ``0x1521`` is both 八咫镜[武士] and
    [忍者]), which is kept visible instead of silently picking one.
    """
    rows = _rows_from_xlsx(source, ITEMS_SHEET)
    entries: list[ItemEntry] = []
    conflicts: list[str] = []
    by_id: dict[int, ItemEntry] = {}
    skipped = 0
    for row in rows[1:]:  # skip header: 大类 / 中类 / 小类 / 代码 / 名称 / 备注
        if len(row) < 5:
            skipped += 1
            continue
        big, mid, _small, code, name = (cell.strip() for cell in row[:5])
        if big != ITEM_BIG_CLASS or not name:
            skipped += 1
            continue
        item_id = parse_item_code(code)
        if item_id is None:
            skipped += 1
            continue
        if item_id in by_id:
            existing = by_id[item_id]
            if existing.name != name:
                conflicts.append(
                    f"{item_id:#06x} 同时是「{existing.name}」与「{name}」；"
                    f"表中保留「{existing.name}」"
                )
            skipped += 1
            continue
        entry = ItemEntry(item_id=item_id, name=name, category=mid)
        by_id[item_id] = entry
        entries.append(entry)
    return entries, conflicts, skipped


def collect_soul_entries(source: Path) -> tuple[list[AffixEntry], int]:
    """Read the 魂核 rows of 绘卷-魂核词条 as the legal 魂核 affix table.

    That sheet's 种类 column is 绘卷 or 魂核; a 魂核 core has no 恩宠/套装 affix and
    its own affix pool, so this table — not the 饰品词条 one — decides what may be
    written to a 魂核 record.  ``(entries, skipped)``.
    """
    rows = _rows_from_xlsx(source, SOUL_AFFIX_SHEET)
    entries: list[AffixEntry] = []
    seen: set[int] = set()
    skipped = 0
    for row in rows[1:]:  # skip header: 种类 / 类别 / 词条代码 / 词条名称 / ...
        if len(row) < 4:
            skipped += 1
            continue
        kind, category, code, name = (cell.strip() for cell in row[:4])
        if kind != SOUL_AFFIX_KIND or not name:
            skipped += 1
            continue
        effect_id, value, flags = parse_code(code)
        if effect_id == 0xFFFFFFFF or effect_id in seen:
            skipped += 1
            continue
        seen.add(effect_id)
        entries.append(AffixEntry(effect_id=effect_id, value=value, flags=flags,
                                  name=name, category=category or SOUL_AFFIX_KIND))
    return entries, skipped


def collect_soul_item_entries(source: Path) -> tuple[list[ItemEntry], list[str], int]:
    """Read the 魂核 rows of 物品总目录 as display-only item entries.

    Same shape as :func:`collect_item_entries` (``BB F5`` low byte first), but for
    大类 = 魂核 (83 ids in v2.21).  ``(entries, conflicts, skipped)``.
    """
    rows = _rows_from_xlsx(source, ITEMS_SHEET)
    entries: list[ItemEntry] = []
    conflicts: list[str] = []
    by_id: dict[int, ItemEntry] = {}
    skipped = 0
    for row in rows[1:]:
        if len(row) < 5:
            skipped += 1
            continue
        big, mid, _small, code, name = (cell.strip() for cell in row[:5])
        if big != SOUL_ITEM_BIG_CLASS or not name:
            skipped += 1
            continue
        item_id = parse_item_code(code)
        if item_id is None:
            skipped += 1
            continue
        if item_id in by_id:
            existing = by_id[item_id]
            if existing.name != name:
                conflicts.append(
                    f"{item_id:#06x} 同时是「{existing.name}」与「{name}」；"
                    f"表中保留「{existing.name}」"
                )
            skipped += 1
            continue
        entry = ItemEntry(item_id=item_id, name=name, category=mid)
        by_id[item_id] = entry
        entries.append(entry)
    return entries, conflicts, skipped


def collect_value_ranges(source: Path) -> tuple[dict[int, tuple[int, int]], int]:
    """Read the legal value span of every affix from 全词条数值(3稀有度…).

    That sheet is the workbook's own value table: columns
    ``下标 / 标识 / 规范标识 / 类别 / 代码 / 名称 / 最大值 / 最大值(16进制) /
    取值计数 / 取值集合 / 绘卷环境``.  ``取值集合`` lists every value the affix may
    roll, so its min..max *is* the legal range.  ``代码`` is the affix id's **low
    two bytes** (``BC 53`` → ``0x53BC``), which is why this table is keyed by
    ``effect_id & 0xFFFF``.

    Measured on the reporting user's save: all 807 accessory slots whose affix is
    in this table store a value inside its range — 0 exceptions — which is what
    makes the range a safe write gate.  ``(ranges, skipped)``.
    """
    rows = _rows_from_xlsx(source, VALUE_SHEET_PREFIX)
    ranges: dict[int, tuple[int, int]] = {}
    skipped = 0
    for row in rows[1:]:
        if len(row) < 10:
            skipped += 1
            continue
        code = row[4].strip()
        pooled = row[9].strip()
        parts = [part for part in code.replace("0x", "").split()]
        if len(parts) != 2 or not pooled:
            skipped += 1
            continue
        try:
            key = int(parts[1] + parts[0], 16) if len(parts[1]) == 2 else int(code, 16)
        except ValueError:
            skipped += 1
            continue
        values = [int(chunk) for chunk in pooled.split("|") if chunk.strip().isdigit()]
        if not values:
            skipped += 1
            continue
        ranges[key] = (min(values), max(values))
    return ranges, skipped


def collect_soul_value_ranges(source: Path) -> dict[int, tuple[int, int]]:
    """Legal value span of every 魂核词条, from the sheet's own 数值区间 columns.

    绘卷-魂核词条 carries 词条名称/数值区间 (two cells: min and max) and 数值集合
    (every rollable value, pipe separated); the pooled set is preferred when it is
    present, because it is the more precise statement of the same range.
    """
    rows = _rows_from_xlsx(source, SOUL_AFFIX_SHEET)
    ranges: dict[int, tuple[int, int]] = {}
    for row in rows[1:]:
        if len(row) < 5:
            continue
        kind, code = row[0].strip(), row[2].strip()
        if kind != SOUL_AFFIX_KIND:
            continue
        try:
            effect_id, _value, _flags = parse_code(code)
        except ValueError:
            continue
        pooled = row[6].strip() if len(row) > 6 else ""
        values = [int(chunk) for chunk in pooled.split("|") if chunk.strip().isdigit()]
        if not values:
            low = row[4].strip() if len(row) > 4 else ""
            high = row[5].strip() if len(row) > 5 else ""
            numbers = []
            for text in (low, high):
                try:
                    numbers.append(int(float(text)))
                except ValueError:
                    continue
            values = numbers
        if not values:
            continue
        ranges[effect_id] = (min(values), max(values))
    return ranges


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    dry_run = "--dry-run" in sys.argv[1:]
    source = resolve_source(args[0] if args else None)
    if not source.is_file():
        print(f"错误：找不到词条来源文件 {source}")
        print("      原始数据应位于 third_party/source-data/ "
              "（见该目录的 README.md），或把路径作为参数传入。")
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

    # Attach the workbook's own value spans: 取值集合 min..max per affix, keyed by
    # the low two bytes of the id in 全词条数值 (see collect_value_ranges).
    spans: dict[int, tuple[int, int]] = {}
    spans_skipped = 0
    try:
        spans, spans_skipped = collect_value_ranges(source)
    except (FileNotFoundError, KeyError, zipfile.BadZipFile) as error:
        print(f"    警告：未能读取 {VALUE_SHEET_PREFIX}（{error}）；数值区间未更新")
    ranged = 0
    if spans:
        updated = []
        for entry in entries:
            span = spans.get(entry.effect_id & 0xFFFF)
            if span is None:
                updated.append(entry)
                continue
            ranged += 1
            updated.append(replace(entry, value_min=span[0], value_max=span[1]))
        entries = updated

    if dry_run:
        print(f"演练：将生成 {len(entries)} 条词条（未写入）")
    else:
        save_catalog(entries, source=f"{source.name} / {TARGET_SHEET}",
                     conflicts=conflicts)
        print(f"OK: 已生成词条库，共 {len(entries)} 条饰品合法词条")
    print("    来源:", source)
    print(f"    跳过行: {skipped_rows}")
    if spans:
        print(f"    数值区间: 覆盖 {ranged}/{len(entries)} 条"
              f"（表内 {len(spans)} 条，未识别行 {spans_skipped}）")
    if conflicts:
        print(f"    同一词条 ID 多值冲突: {len(conflicts)} 处（已保留首个）")
        for item in conflicts[:10]:
            print("      -", item)

    # 恩宠/套装组合 name table (display only).  Only an xlsx has the 词条总目录
    # sheet; the TSV fallback carries just the 饰品词条 rows.
    grace: list[AffixEntry] = []
    grace_skipped = 0
    if source.suffix.lower() != ".tsv":
        try:
            grace, grace_skipped = collect_grace_entries(source)
        except (FileNotFoundError, KeyError, zipfile.BadZipFile) as error:
            print(f"    警告：未能读取 {GRACE_SHEET}（{error}）；恩宠/套装名表未更新")
    if not grace:
        return 0
    if dry_run:
        print(f"演练：将生成 {len(grace)} 条恩宠/套装名称（未写入）")
    else:
        save_grace_catalog(grace, source=f"{source.name} / {GRACE_SHEET}（恩宠·套装）")
        print(f"OK: 已生成恩宠/套装名表，共 {len(grace)} 条")
    kinds: dict[str, int] = {}
    for entry in grace:
        kinds[entry.category] = kinds.get(entry.category, 0) + 1
    print("    分类:", "，".join(f"{name} {count}" for name, count in sorted(kinds.items())))
    print(f"    跳过行: {grace_skipped}")

    # 饰品 rows of 物品总目录: what an accessory *is* (display only).
    items: list[ItemEntry] = []
    item_conflicts: list[str] = []
    item_skipped = 0
    try:
        items, item_conflicts, item_skipped = collect_item_entries(source)
    except (FileNotFoundError, KeyError, zipfile.BadZipFile) as error:
        print(f"    警告：未能读取 {ITEMS_SHEET}（{error}）；物品种类表未更新")
    if not items:
        return 0
    if dry_run:
        print(f"演练：将生成 {len(items)} 条饰品物品种类（未写入）")
    else:
        save_item_catalog(items, source=f"{source.name} / {ITEMS_SHEET}（{ITEM_BIG_CLASS}）",
                          conflicts=item_conflicts)
        print(f"OK: 已生成物品种类表，共 {len(items)} 条饰品条目")
    categories: dict[str, int] = {}
    for entry in items:
        categories[entry.category] = categories.get(entry.category, 0) + 1
    print("    分类:", "，".join(f"{name} {count}"
                                for name, count in sorted(categories.items())))
    print(f"    跳过行: {item_skipped}")
    if item_conflicts:
        print(f"    同一物品 ID 多名称: {len(item_conflicts)} 处")
        for line in item_conflicts[:10]:
            print("      -", line)

    # 魂核 affix table + 魂核 item table (own 大类, own affix pool).
    souls: list[AffixEntry] = []
    souls_skipped = 0
    try:
        souls, souls_skipped = collect_soul_entries(source)
    except (FileNotFoundError, KeyError, zipfile.BadZipFile) as error:
        print(f"    警告：未能读取 {SOUL_AFFIX_SHEET}（{error}）；魂核词条库未更新")
    if souls:
        try:
            soul_spans = collect_soul_value_ranges(source)
        except (FileNotFoundError, KeyError, zipfile.BadZipFile):
            soul_spans = {}
        soul_ranged = 0
        if soul_spans:
            updated_souls = []
            for entry in souls:
                span = soul_spans.get(entry.effect_id)
                if span is None:
                    updated_souls.append(entry)
                    continue
                soul_ranged += 1
                updated_souls.append(
                    replace(entry, value_min=span[0], value_max=span[1]))
            souls = updated_souls
        if dry_run:
            print(f"演练：将生成 {len(souls)} 条魂核词条（未写入）")
        else:
            save_soul_catalog(
                souls, source=f"{source.name} / {SOUL_AFFIX_SHEET}（{SOUL_AFFIX_KIND}）")
            print(f"OK: 已生成魂核词条库，共 {len(souls)} 条")
        print(f"    跳过行: {souls_skipped}")
        print(f"    数值区间: 覆盖 {soul_ranged}/{len(souls)} 条")

    soul_items: list[ItemEntry] = []
    soul_item_conflicts: list[str] = []
    soul_items_skipped = 0
    try:
        soul_items, soul_item_conflicts, soul_items_skipped = (
            collect_soul_item_entries(source))
    except (FileNotFoundError, KeyError, zipfile.BadZipFile) as error:
        print(f"    警告：未能读取 {ITEMS_SHEET} 魂核行（{error}）；魂核物品种类表未更新")
    if soul_items:
        if dry_run:
            print(f"演练：将生成 {len(soul_items)} 条魂核物品种类（未写入）")
        else:
            save_soul_item_catalog(
                soul_items,
                source=f"{source.name} / {ITEMS_SHEET}（{SOUL_ITEM_BIG_CLASS}）",
                conflicts=soul_item_conflicts)
            print(f"OK: 已生成魂核物品种类表，共 {len(soul_items)} 条")
        soul_categories: dict[str, int] = {}
        for entry in soul_items:
            soul_categories[entry.category] = soul_categories.get(entry.category, 0) + 1
        print("    分类:", "，".join(f"{name} {count}"
                                   for name, count in sorted(soul_categories.items())))
        print(f"    跳过行: {soul_items_skipped}")
        if soul_item_conflicts:
            print(f"    同一魂核 ID 多名称: {len(soul_item_conflicts)} 处")
            for line in soul_item_conflicts[:10]:
                print("      -", line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
