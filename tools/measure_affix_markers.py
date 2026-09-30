"""Measure the per-affix record markers (``prefix u32@+0x00`` = 条目标记) from a save
and write them to ``data/affix_markers.json``.

    python tools/measure_affix_markers.py <save.bin> [--out data/affix_markers.json]

What was measured (pristine reference save, never written by this tool):

* ``prefix`` low 16 bits are **one constant per affix id** (1328/1328 ids unanimous);
  the high 16 bits are 0 for the large majority and vary per copy for a few ids, so the
  table stores the unanimous low half plus the majority full value.
* the metadata low byte (``roll``) is **not** a function of the affix id and value
  (57.5% of (id, value) pairs still disagree) — it looks like a per-copy roll quality in
  ``0..100``.  It is therefore *not* written back; the table only records the finding.

The save is opened read-only; nothing is written back to it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nioh3_equipment_affix_editor import editor, records  # noqa: E402
from nioh3_equipment_affix_editor.affixdb import AffixDb  # noqa: E402
from nioh3_equipment_affix_editor.editor import SaveDescriptor  # noqa: E402
from nioh3_equipment_affix_editor.savefile import SaveCrypto  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "affix_markers.json"
SCHEMA = "nioh3-affix-markers/v1"
ROLL_MASK = 0xFF
#: 结论（2026-09，见 CHANGELOG）：标记低 16 位**不能**由我们手上的数据推导 ——
#: 已系统否证 id 位段/线性关系、工作簿类别与种类码、12 字节词条代码的逐字节与任意
#: 2 字节窗口、工作簿/排序枚举序号、flags、取值区间、名称散列（前 1000 个 id 拟合、
#: 其余 328 个 id 预测，全部 0 命中）。所以它只能**从存档样本观测**；查不到样本时
#: 保持原值（见 editor 的写入路径），覆盖率受可用样本限制。
PREFIX_LOW_MASK = 0xFFFF


def _key(effect_id: int) -> str:
    return f"{effect_id:#06x}"


def _looks_plain(raw: bytes) -> bool:
    """已解密的存档以 ``RNNUSR`` 开头；本工具写出的 *-plain.bin 备份就是这种。"""
    return raw.startswith(b"RNNUSR")


def main() -> int:
    parser = argparse.ArgumentParser(description="统计词条的条目标记（只读）")
    parser.add_argument("save", help="SAVEDATA.BIN 路径（只读统计）")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    path = Path(args.save)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    # 本工具自己写的备份是"解密后的明文"（*-plain.bin），再解一次会失败；
    # 因此按魔数自动识别：明文直接当已解密数据用（仍然只读）。
    if _looks_plain(raw):
        data = raw
    else:
        data = editor.open_save(SaveDescriptor(path, 0, 0, len(raw)), SaveCrypto())
    affix_db = AffixDb()
    layout = editor.inspect_layout(data, known_ids=editor.accessory_catalog_ids(affix_db))

    low_by_id: dict[int, Counter] = defaultdict(Counter)
    full_by_id: dict[int, Counter] = defaultdict(Counter)
    roll_all: Counter = Counter()
    roll_by_id_value: dict[tuple[int, int], Counter] = defaultdict(Counter)
    slots_seen = records_seen = 0
    for index in range(layout.slot_count):
        record = records.read_item_record(data, index, layout=layout)
        if record is None:
            continue
        records_seen += 1
        for slot in records.read_effect_slots(record.record):
            if slot.is_empty:
                continue
            slots_seen += 1
            low_by_id[slot.effect_id][slot.prefix & PREFIX_LOW_MASK] += 1
            full_by_id[slot.effect_id][slot.prefix] += 1
            roll_all[slot.metadata & ROLL_MASK] += 1
            roll_by_id_value[(slot.effect_id, slot.value)][slot.metadata & ROLL_MASK] += 1

    prefix_low: dict[str, int] = {}
    prefix_majority: dict[str, int] = {}
    samples: dict[str, int] = {}
    low_conflicts: list[str] = []
    high_variants: list[str] = []
    for effect_id, counter in sorted(low_by_id.items()):
        low, count = counter.most_common(1)[0]
        prefix_low[_key(effect_id)] = low
        prefix_majority[_key(effect_id)] = full_by_id[effect_id].most_common(1)[0][0]
        samples[_key(effect_id)] = sum(counter.values())
        if len(counter) > 1:
            low_conflicts.append(
                f"{_key(effect_id)}：低 16 位不唯一 "
                + "、".join(f"{v:#06x}×{n}" for v, n in counter.most_common(4))
            )
        if len(full_by_id[effect_id]) > 1:
            top = full_by_id[effect_id].most_common(2)
            high_variants.append(
                f"{_key(effect_id)}：高 16 位有多个取值 "
                + "、".join(f"{v:#010x}×{n}" for v, n in top)
                + f"（低 16 位恒为 {low:#06x}）"
            )

    unique_roll = sum(1 for c in roll_by_id_value.values() if len(c) == 1)
    payload = {
        "schema": SCHEMA,
        "source": f"{path.name}（只读统计，工具从未写入过该文件）",
        "sha256": digest,
        "records_scanned": records_seen,
        "slots_scanned": slots_seen,
        "prefix_low_by_effect_id": prefix_low,
        "prefix_majority_by_effect_id": prefix_majority,
        "prefix_samples_by_effect_id": samples,
        "prefix_low_mask": PREFIX_LOW_MASK,
        "roll_byte": {
            "derivable": False,
            "mask": ROLL_MASK,
            "range": [min(roll_all), max(roll_all)],
            "distinct_values": len(roll_all),
            "top_values": {str(value): count for value, count in roll_all.most_common(10)},
            "id_value_pairs": len(roll_by_id_value),
            "id_value_pairs_with_one_roll": unique_roll,
            "note": ("元数据低字节看着是每次掉落各自掷出的 0..100 品质百分比，"
                     "既不由词条 id 决定、也不由 (id, 数值) 决定；本工具因此不改写它，"
                     "只保留原值。"),
        },
        "conflicts": low_conflicts + high_variants,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8", newline="\n")

    print(f"写入 {out}")
    print(f"存档 sha256 {digest}")
    print(f"扫描记录 {records_seen}、非空槽 {slots_seen}、不同词条 {len(prefix_low)}")
    print(f"低 16 位同 id 唯一的词条：{len(prefix_low) - len(low_conflicts)}/{len(prefix_low)}")
    print(f"高 16 位有多个取值的词条：{len(high_variants)}")
    print(f"roll 字节范围 {min(roll_all)}..{max(roll_all)}，不同取值 {len(roll_all)}；"
          f"(id,值) 里 roll 唯一的 {unique_roll}/{len(roll_by_id_value)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
