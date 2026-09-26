"""统一【应用修改】必须原子化；写入前必须再验一次"新引入的违规"。

用户实测缺陷：在【武器】页签把某槽改成另一个【造成伤害】词条（该记录已有一个
【造成伤害】），点【应用修改】弹出红框说"每个种类只能有一个词条"，但状态行仍然
写着"已应用"，而且还能继续写入存档 —— 属于"部分成功 + 假成功提示"。
"""

from __future__ import annotations

import unittest
from unittest import mock

from nioh3_equipment_affix_editor import editor, equipmentdb, records, ui
from nioh3_equipment_affix_editor.affixdb import GraceDb
from tests import support
from tests.test_ui_weapon_armor import EquipmentTabTestCase


def _two_affixes_of_one_category(db, tags, category_wanted=None):
    """挑出同一个种类的两条可用词条（非固定、标签相容、id 不同）。"""
    by_category: dict[str, list] = {}
    for entry in db.db.all():
        if entry.is_fixed or entry.category == editor.CATEGORY_OTHER:
            continue
        if tags and not set(db.tags_of(entry.effect_id)) & set(tags):
            continue
        if category_wanted and entry.category != category_wanted:
            continue
        by_category.setdefault(entry.category, []).append(entry)
    for category, entries in sorted(by_category.items()):
        if len(entries) >= 2:
            return category, entries[0], entries[1]
    raise unittest.SkipTest("词条表里找不到同种类的两条可用词条")


class NewRecordViolationsTests(EquipmentTabTestCase):
    """写入前的闸门：只报**新引入**的违规。"""

    def _pools(self):
        return {equipmentdb.POOL_MELEE: self.melee,
                equipmentdb.POOL_RANGED: self.ranged,
                equipmentdb.POOL_ARMOR: self.armor}

    def _violations(self, current, baseline):
        return editor.new_record_violations(
            current, baseline, affix_db=self.app.affix_db,
            soul_db=self.app.soul_db, pools=self._pools(),
            grace_db=GraceDb.best_effort(), item_db=self.item_db,
            layout=records.locate_layout(baseline))

    def test_a_brand_new_duplicate_category_is_reported(self) -> None:
        category, first, second = _two_affixes_of_one_category(self.melee, ("近战",))
        codes = editor.load_affix_category_codes()
        base = support.build_plain_save(records_by_slot={
            3: support.build_record(
                record_type=self.katana.item_id, level=170, rarity=4,
                effects=((first.effect_id, first.value,
                          editor.affix_metadata(0, first, codes)),)),
        })
        layout = records.locate_layout(base)
        record = records.read_item_record(base, 3, layout=layout).record
        patched = records.patch_effect_slots(record, [
            {"slot_index": 1, "effect_id": second.effect_id,
             "value": second.value,
             "metadata": editor.affix_metadata(0, second, codes)},
        ])
        offset = records.record_offset(3, layout=layout)
        current = base[:offset] + patched + base[offset + len(patched):]
        found = editor.new_record_violations(
            current, base, affix_db=self.app.affix_db, soul_db=self.app.soul_db,
            pools=self._pools(), grace_db=GraceDb.best_effort(),
            item_db=self.item_db, layout=records.locate_layout(base))
        self.assertTrue(found, f"同种类重复没有被报出来（{category}）")
        self.assertIn("只能有一个词条", found[0])

    def test_a_pre_existing_duplicate_is_not_reported(self) -> None:
        """预先存在的同种类重复不该拦住写入（只拦新引入的）。"""
        category, first, second = _two_affixes_of_one_category(self.melee, ("近战",))
        codes = editor.load_affix_category_codes()
        dup = support.build_plain_save(records_by_slot={
            3: support.build_record(
                record_type=self.katana.item_id, level=170, rarity=4,
                effects=((first.effect_id, first.value,
                          editor.affix_metadata(0, first, codes)),
                         (second.effect_id, second.value,
                          editor.affix_metadata(0, second, codes)))),
        })
        self.assertEqual(self._violations(dup, dup), ())

    def test_an_unrelated_edit_does_not_trip_the_gate(self) -> None:
        _category, first, second = _two_affixes_of_one_category(self.melee, ("近战",))
        codes = editor.load_affix_category_codes()
        base = support.build_plain_save(records_by_slot={
            3: support.build_record(
                record_type=self.katana.item_id, level=170, rarity=4,
                effects=((first.effect_id, first.value,
                          editor.affix_metadata(0, first, codes)),)),
        })
        current = base  # 没有任何改动 → 不该报任何东西
        self.assertEqual(self._violations(current, base), ())


class AtomicApplyTests(EquipmentTabTestCase):
    """统一【应用修改】：一项被拒 → 整批不落地、不报假成功。"""

    def _weapon_tab(self):
        return next(tab for tab in self.app.equipment_tabs if tab.big == "武器")

    def test_a_refused_change_leaves_memory_untouched_and_says_so(self) -> None:
        category, first, second = _two_affixes_of_one_category(self.melee, ("近战",))
        codes = editor.load_affix_category_codes()
        # 槽 0 放 first（这个种类），槽 1 先放一条**别的种类**的词条 ——
        # 空槽在本周目不可写，所以目标槽必须本来就有词条。
        other = next(entry for entry in self.melee.db.all()
                     if not entry.is_fixed
                     and entry.category not in (first.category, editor.CATEGORY_OTHER)
                     and set(self.melee.tags_of(entry.effect_id)) & {"近战"})
        data = support.build_plain_save(records_by_slot={
            3: support.build_record(
                record_type=self.katana.item_id, level=170, rarity=4,
                effects=((first.effect_id, first.value,
                          editor.affix_metadata(0, first, codes)),
                         (other.effect_id, other.value,
                          editor.affix_metadata(0, other, codes)))),
        })
        layout = records.locate_layout(data)
        known_ids = ui.accessory_catalog_ids(self.app.affix_db)
        self.app._populate_accessories(
            (data, ui.list_accessories(data, layout=layout, known_ids=known_ids),
             True, layout))
        tab = self._weapon_tab()
        tab.tree.selection_set("3")
        tab._on_selected()

        # 用**该槽自己的候选标签**（引擎只接受候选里的词条）
        label = next((text for text in tab._candidates
                      if text.startswith(f"{second.effect_id:#06x}")), None)
        self.assertIsNotNone(label, "第二条同种类词条不在候选里")

        original = self.app.decrypted
        errors: list[str] = []
        with mock.patch.object(ui.messagebox, "askokcancel", lambda *a, **k: True), \
             mock.patch.object(ui.messagebox, "showerror",
                               lambda *a, **k: errors.append(str(a[1] if len(a) > 1 else a))):
            tab.slot_combos[1].set(label)
            tab.value_vars[1].set(str(second.value))
            tab.apply_all()

        self.assertTrue(errors, "被拒时应该弹出错误说明")
        self.assertIn("只能有一个词条", errors[0])
        self.assertEqual(self.app.decrypted, original,
                         "一批改动里有一项被拒时，内存必须一个字节都不变")
        status = self.app.status_var.get()
        self.assertIn("未应用", status)
        self.assertNotIn("已应用到内存数据", status)

    def test_the_read_summary_counts_what_is_actually_listed(self) -> None:
        """读取后的总结按**真实列举结果**报告，并指向正确页签（不再说"未列出"）。"""
        self._load_standard()
        status = self.app.status_var.get()
        self.assertIn("已读取", status)
        self.assertNotIn("未列出", status)
        self.assertIn("【武器】【防具】页签", status)
        weapon = next(tab for tab in self.app.equipment_tabs if tab.big == "武器")
        armor = next(tab for tab in self.app.equipment_tabs if tab.big == "防具")
        self.assertIn(f"{len(weapon.views)} 条武器", status)
        self.assertIn(f"{len(armor.views)} 条防具", status)


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
