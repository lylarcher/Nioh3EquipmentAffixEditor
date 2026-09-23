"""Measure per-类别 field ranges (等级 / +値 / 词条槽数) from a save — 按类别收集.

    python tools/measure_equipment_ranges.py <save.bin> [--out data/equipment_ranges.json]

Only reads the save (a copy is decrypted in memory).  The output is evidence, not a
guess: every class carries the range actually observed plus how many records it came
from, and the file records the save's SHA-256 so a later build can tell where the
numbers came from.  A class with too few samples is reported as-is (``samples``), and
the editor must not tighten a limit below the shipped 等级 180 unless the numbers say so.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nioh3_accessory_editor import editor, records  # noqa: E402
from nioh3_accessory_editor.affixdb import AffixDb  # noqa: E402
from nioh3_accessory_editor.editor import SaveDescriptor  # noqa: E402
from nioh3_accessory_editor.equipmentdb import (  # noqa: E402
    DEFAULT_EQUIPMENT_ITEM_CATALOG, DEFAULT_EQUIPMENT_RANGES, EQUIPMENT_RANGES_SCHEMA,
    load_equipment_item_db,
)
from nioh3_accessory_editor.savefile import SaveCrypto  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="按类别收集等级/+値/槽数范围（只读）")
    parser.add_argument("save", help="SAVEDATA.BIN 路径（只读）")
    parser.add_argument("--out", default=str(DEFAULT_EQUIPMENT_RANGES))
    args = parser.parse_args()

    path = Path(args.save)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    data = editor.open_save(SaveDescriptor(path, 0, 0, len(raw)), SaveCrypto())
    item_db = load_equipment_item_db()
    affix_db = AffixDb()
    layout = editor.inspect_layout(data, known_ids=editor.accessory_catalog_ids(affix_db))

    classes: dict[str, dict] = {}
    unknown = Counter()
    for index in range(layout.slot_count):
        record = records.read_item_record(data, index, layout=layout)
        if record is None:
            continue
        effects = [e for e in records.read_effect_slots(record.record) if not e.is_empty]
        if not effects:
            continue
        item = item_db.lookup(record.record_type)
        if item is None:
            unknown[record.record_type] += 1
            continue
        key = f"{item.big}/{item.category}/{item.small}" if item.small else \
            f"{item.big}/{item.category}"
        bucket = classes.setdefault(key, {
            "big": item.big, "category": item.category, "small": item.small,
            "samples": 0, "level": [9999, -1], "plus": [9999, -1], "slots": [99, -1],
            "rarity": [99, -1], "star_slots": 0, "fixed_slots": 0, "slot_total": 0,
        })
        bucket["samples"] += 1
        plus = records.read_record_plus(record.record)
        bucket["level"][0] = min(bucket["level"][0], record.level)
        bucket["level"][1] = max(bucket["level"][1], record.level)
        bucket["plus"][0] = min(bucket["plus"][0], plus)
        bucket["plus"][1] = max(bucket["plus"][1], plus)
        bucket["slots"][0] = min(bucket["slots"][0], len(effects))
        bucket["slots"][1] = max(bucket["slots"][1], len(effects))
        bucket["rarity"][0] = min(bucket["rarity"][0], record.rarity)
        bucket["rarity"][1] = max(bucket["rarity"][1], record.rarity)
        bucket["slot_total"] += len(effects)
        bucket["star_slots"] += sum(1 for e in effects if e.metadata & 0x040000)
        bucket["fixed_slots"] += sum(1 for e in effects if e.metadata & 0x4000)

    for key, bucket in classes.items():
        for field in ("level", "plus", "slots", "rarity"):
            if bucket[field][1] < 0:
                bucket[field] = [0, 0]
    payload = {
        "schema": EQUIPMENT_RANGES_SCHEMA,
        "source": f"{path.name}（只读实测）",
        "sha256": digest,
        "item_catalog": DEFAULT_EQUIPMENT_ITEM_CATALOG.name,
        "classes": dict(sorted(classes.items())),
        "unnamed_types": {f"{kind:#06x}": count for kind, count in unknown.most_common()},
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")
    print(f"写入 {out}")
    print(f"存档 sha256 {digest}")
    print(f"类别 {len(classes)} 个，未命名种类 {len(unknown)} 个")
    for key, bucket in sorted(classes.items()):
        print(f"  {key:<28} 记录 {bucket['samples']:<4} 等级 "
              f"{bucket['level'][0]}..{bucket['level'][1]}  +値 "
              f"{bucket['plus'][0]}..{bucket['plus'][1]}  槽 {bucket['slots'][0]}.."
              f"{bucket['slots'][1]}  稀有度 {bucket['rarity'][0]}..{bucket['rarity'][1]}"
              f"  ★槽 {bucket['star_slots']}/{bucket['slot_total']}"
              f"  固定槽 {bucket['fixed_slots']}/{bucket['slot_total']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
