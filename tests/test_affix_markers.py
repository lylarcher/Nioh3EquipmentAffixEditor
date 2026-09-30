"""条目标记（``prefix u32@+0x00``）回填：换词条时按实测表同步标记。

实测来源：``tools/measure_affix_markers.py`` 在参考存档（本工具从未写入过它）上统计出
——每个词条 id 的 ``prefix`` 低 16 位是**唯一常量**（1328/1328），高 16 位多数为 0。
所以换词条时必须把这个标记换成**新词条**的标记；表中查不到时保持原值，绝不编造。
"""

from __future__ import annotations

import json
import unittest

from nioh3_equipment_affix_editor import editor, equipmentdb, records
from nioh3_equipment_affix_editor.affixdb import AffixDb
from nioh3_equipment_affix_editor.affixmarkers import (
    DEFAULT_MARKERS_PATH, AffixMarkers,
)
from nioh3_equipment_affix_editor.editor import apply_edits, list_accessories, plan_edits

from tests import support

ITEM_TYPE = 0x4001
#: 一个明显不属于任何真实记录的标记，用来确认"换词条后它确实被改写"。
SENTINEL_PREFIX = 0xDEADBEEF


class AffixMarkerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = AffixDb()
        cls.markers = AffixMarkers.best_effort()
        free = [entry for entry in cls.db.all() if not entry.is_fixed]
        cls.stamped = [entry for entry in free
                       if cls.markers.prefix_for(entry.effect_id) is not None]
        cls.unstamped = [entry for entry in free
                         if cls.markers.prefix_for(entry.effect_id) is None]

    def _save(self, effect_id: int, value: int = 20, metadata: int = 0x40,
              prefix: int = SENTINEL_PREFIX) -> bytes:
        record = support.build_record(
            record_type=ITEM_TYPE, level=150, rarity=4, prefix=prefix,
            effects=((effect_id, value, metadata),),
        )
        return support.build_plain_save(records_by_slot={3: record})

    def _slot(self, save: bytes, slot_index: int = 0):
        record = support.read_record(save, 3) if hasattr(support, "read_record") else None
        if record is None:  # 回退：直接按布局读取
            layout = support.legacy_layout()
            record = save[
                layout.offset(3):layout.offset(3) + records.SCROLL_RECORD_SIZE]
        return records.read_effect_slots(record)[slot_index]


class MarkerTableTests(AffixMarkerTestCase):
    def test_the_shipped_table_loads_and_is_consistent(self) -> None:
        self.assertTrue(self.markers.is_loaded, self.markers.error)
        self.assertGreater(len(self.markers), 1000)
        payload = json.loads(DEFAULT_MARKERS_PATH.read_text(encoding="utf-8"))
        low = payload["prefix_low_by_effect_id"]
        majority = payload["prefix_majority_by_effect_id"]
        self.assertEqual(set(low), set(majority))
        for key, value in majority.items():
            with self.subTest(key):
                self.assertEqual(low[key], value & 0xFFFF)

    def test_the_catalog_covers_some_and_misses_some(self) -> None:
        """表来自一份存档，因此必然有覆盖到的，也（很可能）有没覆盖到的词条。"""
        self.assertTrue(self.stamped)


class ReplaceStampsTheMarkerTests(AffixMarkerTestCase):
    def test_replacing_an_affix_writes_its_measured_marker(self) -> None:
        if not self.stamped:
            self.skipTest("随包标记表没有覆盖到任何可写词条")
        target = self.stamped[0]
        save = self._save(self.stamped[-1].effect_id)
        self.assertEqual(self._slot(save).prefix, SENTINEL_PREFIX)
        edits = [{"record_index": 3, "slot_index": 0,
                  "effect_id": target.effect_id, "value": target.value}]
        plan_edits(save, edits, affix_db=self.db)
        patched = apply_edits(save, edits, affix_db=self.db)
        slot = self._slot(patched)
        self.assertEqual(slot.effect_id, target.effect_id)
        self.assertEqual(slot.prefix, self.markers.prefix_for(target.effect_id))

    def test_only_the_edited_slot_and_the_checksum_change(self) -> None:
        if not self.stamped:
            self.skipTest("随包标记表没有覆盖到任何可写词条")
        target = self.stamped[0]
        save = self._save(self.stamped[-1].effect_id)
        edits = [{"record_index": 3, "slot_index": 0,
                  "effect_id": target.effect_id, "value": target.value}]
        patched = apply_edits(save, edits, affix_db=self.db)
        layout = support.legacy_layout()
        base = layout.offset(3) + records.EFFECT_START
        allowed = set(range(base, base + records.EFFECT_STRIDE))
        allowed |= set(range(support.SAVE_CHECKSUM_SEED_OFFSET, len(save)))
        changed = {index for index, (before, after) in enumerate(zip(save, patched))
                   if before != after}
        self.assertTrue(changed)
        self.assertLessEqual(changed, allowed,
                             "只有目标槽与该存档的校验区允许变化")

    def test_a_value_only_edit_keeps_the_marker(self) -> None:
        entry = self.stamped[0] if self.stamped else None
        if entry is None or not entry.has_value_range:
            self.skipTest("需要一条有区间的可写词条")
        other = next((value for value in range(entry.value_min, entry.value_max + 1)
                      if value != entry.value), entry.value)
        save = self._save(entry.effect_id, value=entry.value, prefix=SENTINEL_PREFIX)
        edits = [{"record_index": 3, "slot_index": 0, "value": other}]
        patched = apply_edits(save, edits, affix_db=self.db)
        self.assertEqual(self._slot(patched).prefix, SENTINEL_PREFIX)
        self.assertEqual(self._slot(patched).value, other)

    def test_the_roll_byte_is_left_alone(self) -> None:
        """元数据低字节是每次掉落各自掷出的品质百分比，实测不可由 (id, 值) 推出。"""
        if not self.stamped:
            self.skipTest("随包标记表没有覆盖到任何可写词条")
        target = self.stamped[0]
        save = self._save(self.stamped[-1].effect_id, metadata=0x37)
        edits = [{"record_index": 3, "slot_index": 0,
                  "effect_id": target.effect_id, "value": target.value}]
        patched = apply_edits(save, edits, affix_db=self.db)
        self.assertEqual(self._slot(patched).metadata & 0xFF, 0x37)

    def test_an_affix_without_a_sample_keeps_the_old_marker(self) -> None:
        if not self.unstamped:
            self.skipTest("随包标记表覆盖了目录里的全部可写词条")
        target = self.unstamped[0]
        save = self._save(self.stamped[0].effect_id if self.stamped
                          else self.unstamped[-1].effect_id)
        edits = [{"record_index": 3, "slot_index": 0,
                  "effect_id": target.effect_id, "value": target.value}]
        patched = apply_edits(save, edits, affix_db=self.db)
        slot = self._slot(patched)
        self.assertEqual(slot.effect_id, target.effect_id)
        self.assertEqual(slot.prefix, SENTINEL_PREFIX,
                         "查不到标记时必须保持原值，不能猜")

    def test_an_unknown_affix_is_still_refused(self) -> None:
        save = self._save(self.stamped[0].effect_id if self.stamped else 0x0042)
        with self.assertRaises(Exception):
            plan_edits(save, [{"record_index": 3, "slot_index": 0,
                               "effect_id": 0x7FFFFFFF, "value": 1}],
                       affix_db=self.db)


class CreationStampsTheMarkerTests(AffixMarkerTestCase):
    def test_a_created_affix_carries_its_measured_marker(self) -> None:
        """新建（无中生有）走同一套回填：这里用 武器 入口（该入口只做武器/防具）。"""
        melee = equipmentdb.load_melee_weapon_db()
        candidates = [entry for entry in melee.all() if not entry.is_fixed
                      and self.markers.prefix_for(entry.effect_id) is not None]
        if not candidates:
            self.skipTest("近战词条表与随包标记表没有交集")
        target = candidates[0]
        item = next(entry for entry in equipmentdb.load_equipment_item_db().all()
                    if entry.big == "武器")
        donor = support.build_record(
            record_type=item.item_id, level=150, rarity=4,
            effects=((candidates[-1].effect_id, candidates[-1].value, 0x40),),
        )
        save = support.build_plain_save(records_by_slot={3: donor})
        pools = {equipmentdb.POOL_MELEE: equipmentdb.load_pool(equipmentdb.POOL_MELEE)}
        plan = editor.plan_create_equipment(
            save, record_type=item.item_id, level=150, rarity=4,
            effects=[{"slot_index": 0, "effect_id": target.effect_id,
                      "value": target.value}],
            slot_index=11, item_db=equipmentdb.load_equipment_item_db(),
            pools=pools, known_ids=editor.accessory_catalog_ids(self.db),
            layout=support.legacy_layout(),
        )
        created = editor.apply_equipment_creations(save, [plan])
        layout = support.legacy_layout()
        record = created[layout.offset(11):layout.offset(11) + records.SCROLL_RECORD_SIZE]
        slot = records.read_effect_slots(record)[0]
        self.assertEqual(slot.effect_id, target.effect_id)
        self.assertEqual(slot.prefix, self.markers.prefix_for(target.effect_id))


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
