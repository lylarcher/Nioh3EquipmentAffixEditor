"""Build the 武器 / 防具 catalogs (P1) from 仁王3词条装备库v2.21.xlsx.

    python tools/build_equipment_db.py [--dry-run] [source.xlsx]

Writes, next to the existing 饰品/魂核 tables (nothing existing is touched):

    data/weapon_affixes.json    近战词条 + 远程词条 + 绿色星号词条的武器行
    data/armor_affixes.json     防具词条 + 绿色星号词条的防具行
    data/equipment_items.json   物品总目录的全部大类（含 大类/中类/小类，供四个筛选轴）

Every row keeps the workbook's own 类别 (which is what the 种类码 table keys on) and, for
★ rows, the workbook's own 数值区间 / 数值集合 — a missing span is never invented.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nioh3_accessory_editor.affixdb import (  # noqa: E402
    AffixEntry, save_catalog,
)
from nioh3_accessory_editor.equipmentdb import (  # noqa: E402
    ARMOR_CATALOG_SCHEMA, EQUIPMENT_ITEM_SCHEMA, WEAPON_CATALOG_SCHEMA,
    DEFAULT_ARMOR_CATALOG, DEFAULT_EQUIPMENT_ITEM_CATALOG, DEFAULT_WEAPON_CATALOG,
)

import build_affix_db as workbook  # noqa: E402  (same directory)

#: Affix sheets per pool: sheet -> (first data row index, 类别 column, 代码 column, 名称 column).
WEAPON_SHEETS = {"近战词条": (0, 1, 2), "远程词条": (0, 1, 2)}
#: 防具词条 多一列 所属部位，所以 类别/代码/名称 都是后移一位。
ARMOR_SHEETS = {"防具词条": (1, 2, 3)}
#: 绿色星号词条 装备种类 tokens that belong to each pool.
WEAPON_GREEN_TOKENS = frozenset(("近战", "远程", "弓", "火枪", "大炮"))
ARMOR_GREEN_TOKENS = frozenset(("头部", "身体", "手臂", "腿部", "足部"))

CODE_PATTERN = re.compile(r"([0-9A-Fa-f]{2} ){11}[0-9A-Fa-f]{2}")


def _collect_sheet(source: Path, sheet: str, category_column: int,
                   code_column: int, name_column: int) -> tuple[list[AffixEntry], int]:
    """Parse one 4/5-column affix sheet into entries (dedup by id, keep the first)."""
    rows = workbook._rows_from_xlsx(source, sheet)
    entries: list[AffixEntry] = []
    seen: set[int] = set()
    skipped = 0
    for row in rows[1:]:
        if len(row) <= max(category_column, code_column, name_column):
            skipped += 1
            continue
        category = row[category_column].strip()
        code = row[code_column].strip()
        name = row[name_column].strip()
        if not category or not name or not CODE_PATTERN.fullmatch(code):
            skipped += 1
            continue
        try:
            effect_id, value, flags = workbook.parse_code(code)
        except ValueError:
            skipped += 1
            continue
        if effect_id == 0xFFFFFFFF or effect_id in seen:
            skipped += 1
            continue
        seen.add(effect_id)
        entries.append(AffixEntry(effect_id=effect_id, value=value, flags=flags,
                                  name=name, category=category))
    return entries, skipped


def _collect_green(source: Path, tokens: frozenset[str]) -> tuple[list[AffixEntry], int]:
    """★ rows of 绿色星号词条 whose 装备种类 mentions one of ``tokens``."""
    rows = workbook._rows_from_xlsx(source, workbook.GREEN_SHEET)
    entries: list[AffixEntry] = []
    seen: set[int] = set()
    skipped = 0
    for row in rows[1:]:
        if len(row) < 6:
            skipped += 1
            continue
        kinds = [part.strip() for part in row[0].strip().strip("[]").split("/")]
        if not any(token in kinds for token in tokens):
            skipped += 1
            continue
        category, code, name = row[1].strip(), row[2].strip(), row[3].strip()
        low, high = row[4].strip(), row[5].strip()
        if not code or not name or not low.isdigit() or not high.isdigit():
            skipped += 1
            continue
        try:
            effect_id, value, flags = workbook.parse_code(code)
        except ValueError:
            skipped += 1
            continue
        if effect_id == 0xFFFFFFFF or effect_id in seen:
            skipped += 1
            continue
        seen.add(effect_id)
        low_i, high_i = int(low), int(high)
        span = workbook.parse_value_set(row[6:], low_i, high_i)
        if span and value not in span:
            value = min(span, key=lambda item: abs(item - value))
        elif not span and not low_i <= value <= high_i:
            value = min(max(value, low_i), high_i)
        entries.append(AffixEntry(effect_id=effect_id, value=value, flags=flags,
                                  name=name, category=category, value_min=low_i,
                                  value_max=high_i, values=span))
    return entries, skipped


def _with_spans(entries: list[AffixEntry], spans: dict[int, tuple[int, int]]) -> tuple[list[AffixEntry], int]:
    """Attach the workbook's value span (全词条数值) where it knows the id."""
    updated: list[AffixEntry] = []
    hit = 0
    for entry in entries:
        span = spans.get(entry.effect_id & 0xFFFF)
        if span is None:
            updated.append(entry)
            continue
        hit += 1
        updated.append(replace(entry, value_min=span[0], value_max=span[1]))
    return updated, hit


def collect_items(source: Path) -> tuple[list[dict], list[str]]:
    """Every row of 物品总目录 (all 大类) as a plain dict (id/名称/大类/中类/小类)."""
    rows = workbook._rows_from_xlsx(source, workbook.ITEMS_SHEET)
    items: dict[int, dict] = {}
    conflicts: list[str] = []
    for row in rows[1:]:
        if len(row) < 5:
            continue
        big, mid, small, code, name = (cell.strip() for cell in row[:5])
        if not big or not name:
            continue
        item_id = workbook.parse_item_code(code)
        if item_id is None:
            continue
        previous = items.get(item_id)
        if previous is not None:
            if previous["name"] != name:
                conflicts.append(f"{item_id:#06x} 表中同时是「{previous['name']}」与"
                                 f"「{name}」，保留「{previous['name']}」")
            continue
        items[item_id] = {"item_id": item_id, "name": name, "category": mid or big,
                          "small": small, "big": big, "source": ""}
    # 八咫镜 fix-up（与饰品表同一处实测结论）：代码列相同，靠实测区分。
    for item_id, (name, category, evidence) in workbook.ITEM_ID_OVERRIDES.items():
        big = next((suffix for suffix in ("饰品", "魂核", "防具", "武器")
                    if category.endswith(suffix)), category)
        items[item_id] = {"item_id": item_id, "name": name, "category": category,
                          "small": "", "big": big, "source": evidence}
    return [items[key] for key in sorted(items)], conflicts


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    dry_run = "--dry-run" in sys.argv[1:]
    source = workbook.resolve_source(args[0] if args else None)
    if not source.is_file():
        print(f"错误：找不到词条来源文件 {source}")
        return 1

    spans: dict[int, tuple[int, int]] = {}
    try:
        spans, _skipped = workbook.collect_value_ranges(source)
    except Exception as error:  # noqa: BLE001 - 没有数值表也要能生成主表
        print(f"    警告：未能读取全词条数值表（{error}）；数值区间留空")

    report = []
    pools = {
        "武器": (dict(WEAPON_SHEETS), WEAPON_GREEN_TOKENS, DEFAULT_WEAPON_CATALOG,
                 WEAPON_CATALOG_SCHEMA,
                 "仁王3词条装备库v2.21.xlsx / 近战词条 + 远程词条 + 绿色星号词条（武器）"),
        "防具": (dict(ARMOR_SHEETS), ARMOR_GREEN_TOKENS, DEFAULT_ARMOR_CATALOG,
                 ARMOR_CATALOG_SCHEMA,
                 "仁王3词条装备库v2.21.xlsx / 防具词条 + 绿色星号词条（防具）"),
    }
    written: list[str] = []
    for pool, (sheets, tokens, path, schema, provenance) in pools.items():
        entries: list[AffixEntry] = []
        seen: set[int] = set()
        skipped = 0
        for sheet, (category_column, code_column, name_column) in sheets.items():
            batch, batch_skipped = _collect_sheet(source, sheet, category_column,
                                                  code_column, name_column)
            skipped += batch_skipped
            for entry in batch:
                if entry.effect_id in seen:
                    continue
                seen.add(entry.effect_id)
                entries.append(entry)
        green, green_skipped = _collect_green(source, tokens)
        skipped += green_skipped
        added_green = 0
        for entry in green:
            if entry.effect_id in seen:
                continue
            seen.add(entry.effect_id)
            entries.append(entry)
            added_green += 1
        before = len(entries)
        entries, span_hits = _with_spans(entries, spans)
        stars = sum(1 for entry in entries if entry.is_star)
        fixed = sum(1 for entry in entries if entry.is_fixed)
        line = (f"{pool}: {len(entries)} 条（★ {stars}、同名固定 {fixed}，"
                f"★ 新增 {added_green}，数值区间命中 {span_hits}/{before}，跳过 {skipped}）")
        report.append(line)
        print("  " + line)
        if not dry_run:
            save_catalog(entries, path, schema=schema, source="", conflicts=[],
                         default_source=provenance)
            written.append(str(path))

    items, conflicts = collect_items(source)
    from collections import Counter

    per_big = Counter(entry["big"] for entry in items)
    print(f"  物品总目录: {len(items)} 条 -> " +
          "、".join(f"{big} {count}" for big, count in per_big.most_common()))
    if not dry_run:
        payload = {
            "schema": EQUIPMENT_ITEM_SCHEMA,
            "count": len(items),
            "source": "仁王3词条装备库v2.21.xlsx / 物品总目录（全部大类）",
            "conflicts": conflicts,
            "items": items,
        }
        DEFAULT_EQUIPMENT_ITEM_CATALOG.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8", newline="\n")
        written.append(str(DEFAULT_EQUIPMENT_ITEM_CATALOG))
    print("\n".join(["", "生成完毕："] + ["  " + path for path in written])
          if written else "（dry-run：未写文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
