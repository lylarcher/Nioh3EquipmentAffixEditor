"""Deep read-only inspection of one save: what the tool can prove from bytes.

``scan`` answers "where is the record table?".  This tool answers the next
questions, which matter when no in-game reference is available:

1. **Which effect-slot base is real?**  The record's 7 effect slots are 0x18
   bytes apart starting at ``0x34`` per the reference capture.  For every
   candidate base the tool counts how many of the u32 values sitting at
   ``base + k*0x18 + 4`` are *known 饰品词条 ids* from the shipped catalog.  A
   real base shows a hit rate far above the ~``len(catalog)/2**32`` you get from
   arbitrary bytes, so the offset is confirmed by statistics instead of by
   assumption.
2. **Which records can be accessories?**  The catalog only lists 饰品词条, so a
   record whose occupied slots hit the catalog is an accessory; records whose
   values never hit it (weapons, armour, 绘卷) are listed separately.  Affix ids
   that appear in the save but *not* in the catalog are collected too, because
   they are the catalog's own gaps.
3. **What do the records look like?**  A per-type summary (count, level range,
   rarity, occupied-slot histogram) plus a full record listing.

Nothing is written: the save is opened, decrypted in memory and reported.

Usage:
    python tools/inspect_save.py [--config PATH] [--json PATH]
                                 [--records N] [--all]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nioh3_accessory_editor import records, savefile  # noqa: E402
from nioh3_accessory_editor.affixdb import AffixDb, GraceDb, ItemDb  # noqa: E402
from nioh3_accessory_editor.config import load_config  # noqa: E402
from nioh3_accessory_editor.editor import (  # noqa: E402
    SaveDescriptor,
    discover_saves,
    open_save,
    save_checksum_is_valid,
)
from nioh3_accessory_editor.savefile import (  # noqa: E402
    account_id_from_save_path,
    save_slot_index_from_path,
)

#: Candidate effect-slot bases, as offsets from the start of a record.  The
#: captured layout says 0x34; the sweep exists so the tool can *show* whether
#: this save agrees.
BASE_CANDIDATES = tuple(range(0x00, 0xE1, 4))


@dataclass(frozen=True, slots=True)
class BaseScore:
    """How well one candidate effect-slot base matches the affix catalog."""

    base: int
    hits: int
    occupied: int
    distinct_ids: tuple[int, ...]
    slot_indices: tuple[int, ...]

    @property
    def rate(self) -> float:
        return self.hits / self.occupied if self.occupied else 0.0

    @property
    def is_captured_base(self) -> bool:
        return self.base == records.EFFECT_START

    @property
    def aliases_captured_base(self) -> bool:
        """Whether this base reads the captured lattice, shifted by whole slots.

        Bases that differ by a multiple of the stride address the same fields
        under different labels: shifted down they miss the first slot, shifted up
        the last one, so they score *almost* as well without being independent
        evidence.  The report labels them instead of hiding the ambiguity.
        """
        return (self.base != records.EFFECT_START
                and self.base % records.EFFECT_STRIDE
                == records.EFFECT_START % records.EFFECT_STRIDE)


def effect_slot_base_scores(
    decrypted: bytes,
    layout: records.InventoryLayout,
    known_ids: frozenset[int],
) -> tuple[BaseScore, ...]:
    """Score every candidate base by how many catalog ids it finds.

    Hits alone can tie between bases that share the stride lattice, so the
    captured base (``0x34``) is ranked first among equals: the sweep's job is to
    *confirm* the captured offset or show a strictly better one, not to pick a
    relabelled alias of it.
    """
    item_records = [record for record in records.iter_item_records(decrypted,
                                                                  layout=layout)
                    if not record.is_scroll]
    scores: list[BaseScore] = []
    for base in BASE_CANDIDATES:
        hits = 0
        occupied = 0
        found: list[int] = []
        slots: set[int] = set()
        for record in item_records:
            for index in range(records.EFFECT_COUNT):
                start = base + index * records.EFFECT_STRIDE
                if start + 8 > len(record.record):
                    continue
                value = int.from_bytes(record.record[start + 4:start + 8], "little")
                if value in records.EMPTY_EFFECT_IDS:
                    continue
                occupied += 1
                if value in known_ids:
                    hits += 1
                    found.append(value)
                    slots.add(index)
        scores.append(BaseScore(base=base, hits=hits, occupied=occupied,
                                distinct_ids=tuple(sorted(set(found))),
                                slot_indices=tuple(sorted(slots))))
    scores.sort(key=lambda score: (-score.hits, not score.is_captured_base,
                                   score.base))
    return tuple(scores)


def _record_payload(record: records.AccessoryRecord, db: AffixDb) -> dict:
    """One record as JSON-friendly data, with catalog lookups per slot."""
    slots = []
    known = 0
    occupied = 0
    for slot in record.effects:
        entry = None if slot.is_empty else db.lookup(slot.effect_id)
        if not slot.is_empty:
            occupied += 1
            if entry is not None:
                known += 1
        slots.append({
            "index": slot.slot_index,
            "effect_id": slot.effect_id,
            "value": slot.value,
            "metadata": slot.metadata,
            "empty": slot.is_empty,
            "in_catalog": entry is not None,
            "name": entry.name if entry else None,
            "catalog_value": entry.value if entry else None,
        })
    return {
        "slot_index": record.slot_index,
        "offset": record.offset,
        "type": record.record_type,
        "kind": record.kind_name,
        "level": record.level,
        "rarity": record.rarity,
        "rarity_name": record.rarity_name,
        "account_id": record.account_id,
        "item_count": record.item_count,
        "occupied_slots": occupied,
        "catalog_slots": known,
        "slots": slots,
    }


def inspect(decrypted: bytes, db: AffixDb, *, preview_records: int = 4) -> dict:
    """Collect every read-only finding about one decrypted save."""
    known_ids = frozenset(entry.effect_id for entry in db.all())
    diagnosis = records.layout_diagnosis(decrypted, preview_records=preview_records)
    payload: dict[str, object] = {
        "save_size": len(decrypted),
        "diagnosis": diagnosis,
        "layout": diagnosis.get("layout"),
        "base_scores": [],
        "types": [],
        "records": [],
        "accessory_candidates": [],
        "catalog_size": len(db),
    }
    layout = None
    if diagnosis.get("layout"):
        layout = records.locate_layout(decrypted, known_ids=known_ids)
    if layout is None:
        return payload

    scores = effect_slot_base_scores(decrypted, layout, known_ids)
    payload["base_scores"] = [
        {
            "base": score.base,
            "hits": score.hits,
            "occupied": score.occupied,
            "rate": round(score.rate, 4),
            "captured_base": score.is_captured_base,
            "stride_alias": score.aliases_captured_base,
            "slot_indices": list(score.slot_indices),
            "distinct_ids": list(score.distinct_ids),
        }
        for score in scores[:8]
    ]

    all_records = records.iter_item_records(decrypted, layout=layout,
                                            known_ids=known_ids)
    payload["records"] = [_record_payload(record, db) for record in all_records]

    by_type: dict[int, list[dict]] = defaultdict(list)
    for entry in payload["records"]:  # type: ignore[union-attr]
        by_type[entry["type"]].append(entry)
    types = []
    for record_type, entries in sorted(by_type.items(),
                                       key=lambda item: (-len(item[1]), item[0])):
        occupied = Counter(entry["occupied_slots"] for entry in entries)
        levels = [entry["level"] for entry in entries]
        types.append({
            "type": record_type,
            "count": len(entries),
            "levels": [min(levels), max(levels)],
            "rarities": sorted({entry["rarity"] for entry in entries}),
            "occupied_histogram": dict(sorted(occupied.items())),
            "catalog_slots": sum(entry["catalog_slots"] for entry in entries),
            "occupied_total": sum(entry["occupied_slots"] for entry in entries),
            "slot_indices": sorted({slot["index"]
                                    for entry in entries
                                    for slot in entry["slots"]
                                    if not slot["empty"]}),
        })
    payload["types"] = types

    # An accessory is a record whose occupied effect slots name 饰品词条: the
    # catalog holds nothing else, so a weapon/armour/绘卷 record never hits it.
    payload["accessory_candidates"] = [
        entry for entry in payload["records"]  # type: ignore[union-attr]
        if entry["catalog_slots"] > 0
    ]
    missing = Counter()
    for entry in payload["records"]:  # type: ignore[union-attr]
        for slot in entry["slots"]:
            if not slot["empty"] and not slot["in_catalog"]:
                missing[slot["effect_id"]] += 1
    payload["unknown_affix_ids"] = [{"effect_id": effect_id, "count": count}
                                    for effect_id, count in missing.most_common(40)]
    payload["unknown_affix_total"] = sum(missing.values())

    # The trailing occupied slot of an accessory holds its 恩宠/套装组合 effect,
    # which the shipped 饰品词条 table does not list; collect them separately so
    # the table can be extended instead of guessed at.
    grace = Counter()
    for entry in payload["accessory_candidates"]:
        slots = [slot for slot in entry["slots"] if not slot["empty"]]
        for slot in reversed(slots):
            if slot["in_catalog"]:
                break
            grace[slot["effect_id"]] += 1
    payload["grace_affix_ids"] = [{"effect_id": effect_id, "count": count}
                                  for effect_id, count in grace.most_common(40)]
    payload["grace_affix_total"] = sum(grace.values())
    # len(...) of the *truncated* list above would understate the variety; keep
    # the true distinct counts so the report cannot mislead.
    payload["grace_affix_kinds"] = len(grace)

    # What the accessories *are*: the record header's per-item id, resolved
    # against the 饰品 rows of 物品总目录 (display only).
    items = Counter(entry["type"] for entry in payload["accessory_candidates"])
    payload["accessory_item_ids"] = [{"item_id": item_id, "count": count}
                                     for item_id, count in items.most_common(40)]
    payload["accessory_item_total"] = sum(items.values())
    payload["accessory_item_kinds"] = len(items)
    return payload


def _format_report(payload: dict, db: AffixDb, *, listing: int,
                   grace_db: GraceDb | None = None,
                   item_db: ItemDb | None = None) -> str:
    lines: list[str] = []
    lines.append(f"存档大小      : {payload['save_size']:#x} 字节")
    lines.append(f"词条库条目    : {payload['catalog_size']}（仅饰品词条）")
    layout = payload.get("layout")
    if not layout:
        lines.append("记录表        : 未定位到")
        for note in payload["diagnosis"]["notes"]:  # type: ignore[index]
            lines.append(f"  ! {note}")
        return "\n".join(lines)

    lines.append(f"记录表        : {layout['anchor']:#08x} 起 {layout['slot_count']} 槽"
                 f"（步长 {layout['stride']:#x}，占用 {layout['record_count']}）")
    lines.append(f"  · {layout['anchor_note']}")
    lines.append(f"  · {payload['diagnosis']['confidence']}")  # type: ignore[index]

    lines.append("")
    lines.append("效果槽基准搜索（命中 = 该位置的 u32 是词条库内的饰品词条 id）")
    for score in payload["base_scores"]:
        markers = []
        if score["captured_base"]:
            markers.append("参考捕获的 0x34")
        if score["stride_alias"]:
            markers.append("与 0x34 同格（整体位移整数个槽位）")
        note = f" ← {'；'.join(markers)}" if markers else ""
        lines.append(
            f"  基准 {score['base']:#04x}: 命中 {score['hits']}/{score['occupied']}"
            f"（{score['rate']:.1%}）命中槽位 {score['slot_indices']}"
            f" 词条种类 {len(score['distinct_ids'])}{note}"
        )

    lines.append("")
    lines.append("按记录类型汇总")
    for entry in payload["types"]:
        lines.append(
            f"  type {entry['type']:#06x}: {entry['count']} 条"
            f" 等级 {entry['levels'][0]}..{entry['levels'][1]}"
            f" 品质 {entry['rarities']}"
            f" 效果槽占用 {entry['occupied_histogram']}"
            f" 占用槽位 {entry['slot_indices']}"
            f" 命中词条库 {entry['catalog_slots']}/{entry['occupied_total']}"
        )

    candidates = payload["accessory_candidates"]
    lines.append("")
    lines.append(f"饰品（占用槽命中词条库）: {len(candidates)} 条"
                 f" / 记录表内物品记录 {len(payload['records'])} 条")
    for entry in candidates[:listing]:
        lines.append(
            f"  槽 {entry['slot_index']} @ {entry['offset']:#08x}"
            f" type {entry['type']:#06x} Lv{entry['level']} {entry['rarity_name']}"
        )
        for slot in entry["slots"]:
            if slot["empty"]:
                continue
            name = slot["name"] or "（不在词条库）"
            lines.append(f"      [{slot['index']}] {name} id={slot['effect_id']:#010x}"
                         f" 数值={slot['value']} 标识={slot['metadata']:#010x}")
    if len(candidates) > listing:
        lines.append(f"  …还有 {len(candidates) - listing} 条，--all 可全部列出")

    others = [entry for entry in payload["records"]
              if entry["offset"] not in {c["offset"] for c in candidates}]
    if others:
        lines.append("")
        lines.append(f"其余物品记录（占用槽未命中词条库 = 武器/防具/绘卷）: "
                     f"{len(others)} 条")
        for entry in others[:listing]:
            ids = [f"{slot['effect_id']:#010x}"
                   for slot in entry["slots"] if not slot["empty"]]
            lines.append(
                f"  槽 {entry['slot_index']} @ {entry['offset']:#08x}"
                f" type {entry['type']:#06x} Lv{entry['level']}"
                f" 占用 {entry['occupied_slots']} 槽 id={ids or '无'}"
            )
        if len(others) > listing:
            lines.append(f"  …还有 {len(others) - listing} 条，--all 可全部列出")

    unknown = payload.get("unknown_affix_ids") or []
    if unknown:
        lines.append("")
        lines.append(f"存档里出现但词条库没有的 id: {payload['unknown_affix_total']} 处"
                     f"（{len(unknown)} 种，前 12 种）")
        lines.append("  " + " ".join(f"{item['effect_id']:#08x}×{item['count']}"
                                     for item in unknown[:12]))

    grace = payload.get("grace_affix_ids") or []
    if grace:
        lines.append("")
        lines.append("饰品末位槽（游戏内显示为恩宠/套装组合效果）的 id: "
                     f"{payload['grace_affix_total']} 件"
                     f"（{payload.get('grace_affix_kinds', len(grace))} 种，"
                     "列出前 12 种）")
        for item in grace[:12]:
            effect_id = item["effect_id"]
            named = grace_db.describe(effect_id) if grace_db else None
            suffix = named if named else "名表里没有这个 id"
            lines.append(f"  {effect_id:#08x} × {item['count']:<3} {suffix}")
        unknown = [item for item in grace
                   if not (grace_db and grace_db.describe(item["effect_id"]))]
        if unknown:
            lines.append(f"  其中名表未收录: {len(unknown)} 种 "
                         f"（共 {sum(item['count'] for item in unknown)} 件），"
                         "可反馈给作者补全")
    items = payload.get("accessory_item_ids") or []
    if items:
        # Count the named/short names over *all* accessories, not the listed slice.
        all_items = Counter(entry["type"]
                            for entry in payload["accessory_candidates"])
        named = sum(count for item_id, count in all_items.items()
                    if item_db and item_db.describe(item_id))
        lines.append("")
        lines.append(f"饰品是什么（种类，只读）: {payload['accessory_item_total']} 件"
                     f"（{payload.get('accessory_item_kinds', len(items))} 种，"
                     f"列出前 12 种；能对上物品总目录 {named} 件）")
        for item in items[:12]:
            item_id = item["item_id"]
            name = item_db.describe(item_id) if item_db else None
            suffix = name if name else "物品总目录里没有这个 id"
            lines.append(f"  {item_id:#08x} × {item['count']:<3} {suffix}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读深度检查一份仁王3存档（不改写、不联网）")
    parser.add_argument("--config", default=None,
                        help="参数配置文件（用它指定 save_root 等）")
    parser.add_argument("--save", default=None,
                        help="直接指定 SAVEDATA.BIN 路径（优先于自动发现）")
    parser.add_argument("--json", default=None, help="把完整结果写成 JSON 文件")
    parser.add_argument("--records", type=int, default=6,
                        help="报告里每类列出几条（默认 6）")
    parser.add_argument("--all", action="store_true", help="列出全部记录")
    parser.add_argument("--python-crypto", action="store_true",
                        help="使用内置纯 Python 加解密（较慢）")
    args = parser.parse_args(argv)

    config = load_config(Path(args.config) if args.config else None)
    if args.save:
        path = Path(args.save)
        descriptor = _descriptor_for(path)
    else:
        saves = discover_saves(config.resolved_save_root())
        if not saves:
            print("未发现存档：用 --save 指定 SAVEDATA.BIN，"
                  "或在配置文件里设置 save_root。", file=sys.stderr)
            return 1
        descriptor = saves[0]
        if len(saves) > 1:
            print(f"提示：发现 {len(saves)} 个存档，检查第一个 "
                  f"（{descriptor.display}）", file=sys.stderr)

    crypto = savefile.SaveCrypto(prefer_python=args.python_crypto)
    decrypted = open_save(descriptor, crypto)
    db = AffixDb()
    grace_db = GraceDb.best_effort()
    item_db = ItemDb.best_effort()
    payload = inspect(decrypted, db, preview_records=4)
    payload["path"] = str(descriptor.path)
    payload["account_id"] = descriptor.account_id
    payload["slot_index"] = descriptor.slot_index
    payload["checksum_consistent"] = save_checksum_is_valid(decrypted)

    print(f"存档          : {descriptor.display}")
    print(f"路径          : {descriptor.path}")
    print(f"校验和一致    : {'是' if payload['checksum_consistent'] else '否'}")
    print()
    print(_format_report(payload, db,
                         listing=len(payload["records"]) if args.all
                         else max(0, args.records),
                         grace_db=grace_db, item_db=item_db))

    if args.json:
        target = Path(args.json)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print(f"\nJSON 已写入: {target}")
    print("\n只读检查：未修改存档，游戏无需运行。仅供测试学习用。")
    return 0


def _descriptor_for(path: Path) -> SaveDescriptor:
    """Build the descriptor for an explicitly given SAVEDATA.BIN."""
    return SaveDescriptor(path, account_id_from_save_path(path),
                          save_slot_index_from_path(path), path.stat().st_size)


if __name__ == "__main__":
    raise SystemExit(main())
