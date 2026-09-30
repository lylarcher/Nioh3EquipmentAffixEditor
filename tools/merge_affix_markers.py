"""Merge per-save 条目标记 statistics into the shipped table (dev tool).

    python -X utf8 tools/merge_affix_markers.py            # 只报告
    python -X utf8 tools/merge_affix_markers.py --write    # 写 data/affix_markers.json

Reads the statistics JSON files produced by ``tools/measure_affix_markers.py`` (kept in
``tmp/markers_*.json``, which is git-ignored) and merges them with an anti-pollution rule
(see below).  Everything is read-only unless ``--write`` is given; it never touches a save.

规则（用户/上级给定）：
  * 低 16 位常量：所有样本里最高票占比 >= 90% 且样本数 >= 2 才收录，否则记 conflicts 并剔除
  * 高 16 位（多数派完整值）：同规则单独判定
  * roll 字节：只记录、不参与回填
  * 与"干净金样本"不一致的 id 单独列出
只读输入：tmp/markers_*.json 与 data/affix_markers.json；--write 时写 data/affix_markers.json。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
#: 统计文件放在仓库的 tmp/ 下（git 忽略），工具本身在 tools/ 里。
ROOT = REPO / "tmp"
DATA = REPO / "data"
SHIPPED = DATA / "affix_markers.json"
MIN_SHARE = 0.90
MIN_SAMPLES = 2


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    shipped = load(SHIPPED)
    files = sorted(ROOT.glob("markers_*.json"))
    if not files:
        print("没有找到 tmp/markers_*.json，先跑 tools/measure_affix_markers.py")
        return 1

    samples = []          # (sha256, payload)
    for path in files:
        payload = load(path)
        samples.append((payload.get("sha256", path.stem), payload))
    golden = next((sha for sha, payload in samples if sha.startswith("8aa596b9")), "")

    print(f"样本 {len(samples)} 份（金样本 sha 前 8 位 {golden[:8]}）")
    for sha, payload in samples:
        ids = len(payload.get("prefix_low_by_effect_id", {}))
        print(f"  {sha[:12]}  词条 {ids}  记录 {payload.get('records_scanned')}  "
              f"槽 {payload.get('slots_scanned')}")

    # 逐 id 汇总跨存档投票
    low_votes: dict[int, Counter] = defaultdict(Counter)
    high_votes: dict[int, Counter] = defaultdict(Counter)
    samples_by_id: dict[int, int] = defaultdict(int)
    per_save_low: dict[str, dict[int, int]] = {}
    for sha, payload in samples:
        low = {int(k, 16): v for k, v in payload.get("prefix_low_by_effect_id", {}).items()}
        major = {int(k, 16): v for k, v in payload.get("prefix_majority_by_effect_id", {}).items()}
        per_save_low[sha] = low
        for key, value in low.items():
            low_votes[key][value] += 1
            samples_by_id[key] += 1
        for key, value in major.items():
            high_votes[key][value & 0xFFFF0000] += 1

    merged_low: dict[int, int] = {}
    merged_full: dict[int, int] = {}
    conflicts: list[str] = []
    for key, votes in low_votes.items():
        total = sum(votes.values())
        value, top = votes.most_common(1)[0]
        if total < MIN_SAMPLES or top / total < MIN_SHARE:
            conflicts.append(
                f"{key:#06x}: 低 16 位票数 {dict(votes)}（{top}/{total} 未达 "
                f"{MIN_SHARE:.0%} 或多来源不足）→ 剔除")
            continue
        merged_low[key] = value
        high = high_votes.get(key)
        if high:
            high_value, high_top = high.most_common(1)[0]
            high_total = sum(high.values())
            if high_total >= MIN_SAMPLES and high_top / high_total >= MIN_SHARE:
                merged_full[key] = high_value | value
            else:
                conflicts.append(
                    f"{key:#06x}: 高 16 位票数 {dict(high)}（{high_top}/{high_total}）→ "
                    f"只保留低 16 位常量")
                merged_full[key] = value
        else:
            merged_full[key] = value

    # 金样本 vs 其它样本的不一致（这正是"早先写入标记陈旧"的实证线索）
    mismatches = []
    for key, low in per_save_low.items():
        if key not in per_save_low.get(golden, {}):
            continue
        if low != per_save_low[golden][key]:
            others = {sha[:8]: per_save_low[sha][key] for sha, _ in samples
                      if sha != golden and key in per_save_low.get(sha, {})
                      and per_save_low[sha][key] != per_save_low[golden][key]}
            mismatches.append(f"{key:#06x}: 金样本 {low:#06x} vs {others}")

    shipped_low = {int(k, 16): v for k, v in shipped["prefix_low_by_effect_id"].items()}
    new_ids = sorted(set(merged_low) - set(shipped_low))
    lost_ids = sorted(set(shipped_low) - set(merged_low))
    changed = sorted(key for key in set(merged_low) & set(shipped_low)
                     if merged_low[key] != shipped_low[key])

    print(f"\n合并结果：{len(merged_low)} 条（原表 {len(shipped_low)} 条）")
    print(f"  新增 id {len(new_ids)}，剔除 {len(lost_ids)}，取值改变 {len(changed)}")
    print(f"  冲突/剔除条目 {len(conflicts)}")
    for line in conflicts[:8]:
        print("    " + line)
    print(f"金样本与其它样本不一致 {len(mismatches)} 条")
    for line in mismatches[:8]:
        print("    " + line)

    # 逐池覆盖率
    try:
        import sys
        sys.path.insert(0, str(REPO))
        from nioh3_equipment_affix_editor import equipmentdb  # noqa: E402
        pools = [("饰品", equipmentdb.load_accessory_db() if hasattr(equipmentdb, "load_accessory_db") else None),
                 ("近战", equipmentdb.load_melee_weapon_db()),
                 ("远程", equipmentdb.load_ranged_weapon_db()),
                 ("防具", equipmentdb.load_armor_db())]
    except Exception as error:  # noqa: BLE001
        print(f"  覆盖率统计跳过（{error}）")
        pools = []
    print("\n逐池覆盖率（前 → 后）:")
    for name, db in pools:
        if db is None:
            continue
        ids = [entry.effect_id for entry in db.all()]
        before = sum(1 for i in ids if i in shipped_low)
        after = sum(1 for i in ids if i in merged_low)
        print(f"  {name}: {before}/{len(ids)} ({before / len(ids):.1%}) → "
              f"{after}/{len(ids)} ({after / len(ids):.1%})  +{after - before}")

    if args.write:
        out = {
            "schema": shipped.get("schema", "nioh3-affix-markers/v1"),
            "source": "多份未修改存档（只读统计）",
            "sources": [{"sha256": sha, "records_scanned": payload.get("records_scanned"),
                         "slots_scanned": payload.get("slots_scanned")}
                        for sha, payload in samples],
            "records_scanned": sum(payload.get("records_scanned", 0) or 0 for _s, payload in samples),
            "slots_scanned": sum(payload.get("slots_scanned", 0) or 0 for _s, payload in samples),
            "min_share": MIN_SHARE,
            "min_samples": MIN_SAMPLES,
            "prefix_low_by_effect_id": {f"{k:#06x}": v for k, v in sorted(merged_low.items())},
            "prefix_majority_by_effect_id": {f"{k:#06x}": v for k, v in sorted(merged_full.items())},
            "prefix_samples_by_effect_id": {f"{k:#06x}": samples_by_id[k]
                                            for k in sorted(merged_low)},
            "prefix_low_mask": shipped.get("prefix_low_mask", 0xFFFF),
            "roll_byte": shipped.get("roll_byte", "只记录，不参与回填"),
            "conflicts": conflicts,
        }
        SHIPPED.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8", newline="\n")
        print(f"\n已写入 {SHIPPED}（{len(merged_low)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
