"""UI tests for the 武器 / 防具 tabs (P3/P4).

Same conventions as ``tests/test_ui.py``: the real window is built (no main loop)
and every dialog is stubbed, so a mistake in the widgets fails here instead of
hanging the suite.  The rules themselves are pinned by the engine tests
(``tests/test_weapon_armor_edits.py``); what is checked here is that the tab
**uses** those rules: which candidates it offers, which slots it greys out, and
that what it applies is byte-for-byte what the engine plans.
"""

from __future__ import annotations

import unittest
from unittest import mock

from nioh3_accessory_editor import editor, equipmentdb, records, ui
from nioh3_accessory_editor.affixdb import GraceDb
from nioh3_accessory_editor.editor import EditorError
from tests import support
from tests.test_ui import TK_AVAILABLE, TK_ERROR, UiTestCase

#: 存档里的两个标识位（与引擎测试同一口径）。
STAR_BIT = 0x040000
FIXED_BIT = 0x4000
PLAIN = 0x40


def _first(items, predicate):
    return next(item for item in items if predicate(item))


@unittest.skipUnless(TK_AVAILABLE, f"Tk unavailable ({TK_ERROR})")
class EquipmentTabTestCase(UiTestCase):
    """Shared fixtures: one 刀, one 弓 and one 胸甲 record in a synthetic save."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.item_db = equipmentdb.load_equipment_item_db()
        cls.melee = equipmentdb.load_pool(equipmentdb.POOL_MELEE)
        cls.ranged = equipmentdb.load_pool(equipmentdb.POOL_RANGED)
        cls.armor = equipmentdb.load_pool(equipmentdb.POOL_ARMOR)
        cls.grace_db = GraceDb.best_effort()
        cls.katana = _first(cls.item_db.all(), lambda e: e.small == "刀")
        cls.bow = _first(cls.item_db.all(), lambda e: e.small == "弓")
        cls.gun = _first(cls.item_db.all(), lambda e: e.small == "火枪")
        cls.body = _first(cls.item_db.all(), lambda e: e.small == "身体")
        cls.melee_only = _first(
            cls.melee.db.all(), lambda e: cls.melee.tags_of(e.effect_id) == ("近战",))
        cls.melee_free = _first(
            cls.melee.db.all(),
            lambda e: not e.is_fixed and e.effect_id != cls.melee_only.effect_id)
        cls.bow_only = _first(
            cls.ranged.db.all(), lambda e: cls.ranged.tags_of(e.effect_id) == ("弓",))
        cls.armor_free = _first(cls.armor.db.all(), lambda e: not e.is_fixed)
        cls.grace = cls.grace_db.all()[0]

    # ------------------------------------------------------------- 夹具
    def _load(self, records_by_slot):
        data = support.build_plain_save(records_by_slot=records_by_slot)
        layout = records.locate_layout(data)
        # 与真实读取路径同一个口径：带上饰品目录证据，武器/防具才不会被当成饰品。
        known_ids = ui.accessory_catalog_ids(self.app.affix_db)
        self.app._populate_accessories(
            (data, ui.list_accessories(data, layout=layout, known_ids=known_ids),
             True, layout))
        return data, layout

    def _load_standard(self):
        """#3 太刀（普通 + 固定 + 恩宠）、#4 弓、#5 胸甲。"""
        return self._load({
            3: support.build_record(
                record_type=self.katana.item_id, level=150, rarity=4, plus=3,
                effects=((self.melee_free.effect_id, self.melee_free.value, PLAIN),
                         (self.melee_only.effect_id, self.melee_only.value, FIXED_BIT),
                         (self.grace.effect_id, 0, PLAIN))),
            4: support.build_record(
                record_type=self.bow.item_id, level=120, rarity=5,
                effects=((self.bow_only.effect_id, self.bow_only.value, PLAIN),)),
            5: support.build_record(
                record_type=self.body.item_id, level=180, rarity=4, plus=30,
                effects=((self.armor_free.effect_id, self.armor_free.value, PLAIN),)),
        })

    def tab(self, big: str) -> ui.EquipmentTab:
        return next(tab for tab in self.app.equipment_tabs if tab.big == big)

    def select(self, big: str, slot_index: int) -> ui.EquipmentTab:
        tab = self.tab(big)
        tab.tree.selection_set(str(slot_index))
        tab._on_selected()
        return tab

    # --------------------------------------------------------- 页 签 本 身
    def test_the_two_tabs_exist_next_to_the_others(self) -> None:
        labels = [self.app.notebook.tab(tab, "text")
                  for tab in self.app.notebook.tabs()]
        self.assertEqual(labels[:2], ["饰品", "魂核（魂之核）"])
        self.assertEqual(labels[2:], ["武器", "防具"])
        self.assertEqual([tab.big for tab in self.app.equipment_tabs], ["武器", "防具"])

    def test_each_tab_lists_only_its_own_big_class(self) -> None:
        self._load_standard()
        weapons = self.tab("武器")
        armor = self.tab("防具")
        self.assertEqual(sorted(view.slot_index for view in weapons.views), [3, 4])
        self.assertEqual([view.slot_index for view in armor.views], [5])

    def test_a_row_shows_type_level_plus_rarity_and_grace(self) -> None:
        self._load_standard()
        tab = self.tab("武器")
        values = tab.tree.item("3", "values")
        self.assertEqual(values[0], "刀")
        self.assertEqual(values[1], self.grace_db.describe(self.grace.effect_id))
        self.assertEqual(values[2], "150")
        self.assertEqual(values[3], "3")
        self.assertEqual(values[4], records.RARITY_NAMES[4])
        self.assertIn(self.katana.name, tab.tree.item("3", "text"))

    def test_reading_the_save_fills_both_tabs_without_touching_the_others(self) -> None:
        data, _layout = self._load_standard()
        self.assertIs(self.app.decrypted, data)
        # 饰品页签仍然按老口径工作（这条存档里没有饰品词条记录）。
        self.assertEqual(self.app.accessory_views, [])

    # ------------------------------------------------------------ 筛 选
    def test_the_axes_are_named_from_the_item_table(self) -> None:
        self._load_standard()
        tab = self.tab("武器")
        self.assertEqual(list(tab.filter_combos), list(ui.EQUIPMENT_FILTER_AXES))
        self.assertEqual(tab._axis_label("school"),
                         "/".join(self.item_db.schools("武器")))
        self.assertIn("刀", tab.filter_combos["small"]["values"])
        self.assertIn(self.katana.label, tab.filter_combos["kind"]["values"])

    def test_every_axis_narrows_the_other_three(self) -> None:
        self._load_standard()
        tab = self.tab("武器")
        graces = tab.filter_combos["grace"]["values"]
        self.assertIn(self.grace_db.describe(self.grace.effect_id), graces)
        tab.filter_vars["small"].set("弓")
        tab.refresh_tree()
        # 选了「弓」以后：种类只剩弓，恩宠轴清空（弓没有恩宠），记录只剩 #4。
        kinds = tab.filter_combos["kind"]["values"]
        self.assertNotIn(self.katana.label, kinds)
        self.assertIn(self.bow.label, kinds)
        self.assertEqual(list(tab.filter_combos["grace"]["values"]), [ui.ALL_FILTER])
        self.assertEqual([tab.tree.item(i, "text") for i in tab.tree.get_children()],
                         [tab.tree.item("4", "text")])
        tab.filter_vars["small"].set(ui.ALL_FILTER)
        tab.refresh_tree()
        self.assertIn(self.grace_db.describe(self.grace.effect_id),
                      tab.filter_combos["grace"]["values"])

    def test_a_choice_that_no_longer_matches_is_kept_and_explained(self) -> None:
        self._load_standard()
        tab = self.tab("武器")
        tab.filter_vars["small"].set("弓")
        tab.refresh_tree()
        # 「种类」轴上强制选一个弓页签不会提供的取值：应当被保留并标注，而不是静默重置。
        tab.filter_vars["kind"].set(self.katana.label)
        tab.refresh_tree()
        self.assertIn(f"{self.katana.label}（当前无记录）",
                      tab.filter_combos["kind"]["values"])
        self.assertEqual(tab.tree.get_children(), ())
        self.assertIn("筛选后 0 / 2", tab.filter_status_var.get())

    def test_clear_filters_brings_everything_back(self) -> None:
        self._load_standard()
        tab = self.tab("武器")
        tab.filter_vars["small"].set("弓")
        tab.refresh_tree()
        self.assertEqual(len(tab.tree.get_children()), 1)
        tab.clear_filters()
        self.assertEqual(len(tab.tree.get_children()), 2)

    # -------------------------------------------------- 逐 槽 搜 索 / 候 选
    def test_the_candidates_come_from_this_items_own_pool(self) -> None:
        self._load_standard()
        tab = self.select("武器", 3)
        self.assertIs(tab._pool, self.melee)
        katana_count = len(tab._candidates) - 1
        self.select("武器", 4)
        self.assertIs(tab._pool, self.ranged)
        self.assertLess(len(tab._candidates) - 1, katana_count)
        self.assertEqual(katana_count,
                         len([e for e in self.melee.db.all() if not e.is_fixed]))

    def test_a_fixed_affix_is_not_offered_for_manual_writing(self) -> None:
        """同名固定词条不在候选里（引擎拒绝手写，选出来只会白点一次）。"""
        self._load_standard()
        tab = self.select("武器", 3)
        fixed = _first(self.melee.db.all(), lambda e: e.is_fixed)
        self.assertNotIn(fixed.label, tab.slot_combos[0]["values"])
        self.assertEqual(tab._search_candidates(fixed.label), [])
        self.assertNotIn(fixed.effect_id, tab._candidate_ids)

    def test_a_ranged_weapon_never_offers_a_melee_only_affix(self) -> None:
        self._load_standard()
        tab = self.select("武器", 4)
        labels = tab.slot_combos[1]["values"]
        self.assertNotIn(self.melee_only.label, labels)
        self.assertIn(self.bow_only.label, labels)
        # 近战武器相反：只标「近战」的词条在列表里，弓专属的不在。
        self.select("武器", 3)
        labels = tab.slot_combos[0]["values"]
        self.assertIn(self.melee_only.label, labels)
        self.assertNotIn(self.bow_only.label, labels)

    def test_the_two_ranged_classes_share_pool_but_not_every_tag(self) -> None:
        """火枪与弓同表，但只标「弓」的词条不该出现在火枪上（标签相交判定）。"""
        self._load({
            6: support.build_record(record_type=self.gun.item_id, level=100, rarity=4),
        })
        gun = self.select("武器", 6)
        self.assertIs(gun._pool, self.ranged)
        labels = gun.slot_combos[0]["values"]
        self.assertNotIn(self.bow_only.label, labels)
        self.assertNotEqual(self.gun.item_id, self.bow.item_id)

    def test_searching_one_slot_leaves_the_others_alone(self) -> None:
        self._load_standard()
        tab = self.select("武器", 3)
        full = len(tab._candidates)
        self.assertGreater(full, 1)
        tab.slot_combos[0].set("星")
        tab._on_slot_typed(0)
        narrowed = tab.slot_combos[0]["values"]
        self.assertLess(len(narrowed), full)
        # 别的**可编辑**槽（3）候选不变；恩宠槽本来就只有一个只读标签。
        self.assertEqual(len(tab.slot_combos[3]["values"]), full)
        self.assertIn("槽1", tab.search_status_var.get())

    def test_two_keywords_must_both_match(self) -> None:
        self._load_standard()
        tab = self.select("武器", 3)
        sample = next(entry for entry in tab._candidate_entries.values()
                      if len(entry.label) > 12)
        letters = [char for char in sample.label[6:] if char.strip()]
        first, second = letters[0], letters[-1]
        single = len(tab._search_candidates(first))
        both = tab._search_candidates(f"{first} {second}")
        self.assertGreater(single, 0, f"关键词 {first!r} 应当有匹配")
        self.assertLessEqual(len(both), single)
        for entry in both:
            self.assertIn(first, entry.label)
            self.assertIn(second, entry.label)
        self.assertIn(sample.label, [entry.label for entry in both])
        tab.slot_combos[0].set(f"{first} {second}")
        tab._on_slot_typed(0)
        self.assertIn("槽1", tab.search_status_var.get())

    def test_enter_takes_a_keyword_only_when_it_is_unambiguous(self) -> None:
        self._load_standard()
        tab = self.select("武器", 3)
        table = self.melee.db
        unique = _first(
            (entry for entry in table.all() if entry.label in tab._candidate_entries),
            lambda entry: any(len(table.search(entry.label[:size])) == 1
                              for size in range(4, 9)))
        keyword = ""
        for size in range(4, 9):
            candidate = unique.label[:size]
            if len(table.search(candidate)) == 1:
                keyword = candidate
                break
        self.assertTrue(keyword, "应当能找到一个只命中一条的关键词前缀")
        tab.slot_combos[0].set(keyword)
        tab._on_slot_return(0)
        self.assertEqual(tab.slot_combos[0].get(), unique.label)
        self.assertIn("已选中", tab.search_status_var.get())

    def test_an_ambiguous_keyword_asks_the_user_to_pick_from_the_list(self) -> None:
        self._load_standard()
        tab = self.select("武器", 3)
        words = {char for entry in list(tab._candidate_entries.values())[:300]
                 for char in entry.label[6:] if char.strip()}
        ambiguous = sorted(word for word in words
                           if len(tab._search_candidates(word)) > 1)
        self.assertTrue(ambiguous, "前置条件：候选里应当存在多命中的关键词")
        tab.slot_combos[0].set(ambiguous[0])
        tab._on_slot_return(0)
        self.assertIn("请从该槽的下拉列表里选一条", tab.search_status_var.get())
        self.assertGreaterEqual(len(tab.slot_combos[0]["values"]), 2)

    def test_a_keyword_with_no_match_says_so(self) -> None:
        self._load_standard()
        tab = self.select("武器", 3)
        tab.slot_combos[0].set("这个词条不存在")
        tab._on_slot_typed(0)
        self.assertIn("没有匹配", tab.search_status_var.get())
        tab._on_slot_return(0)
        self.assertIn("没有匹配", tab.search_status_var.get())

    # ------------------------------------------------ 固 定 / ★ / 恩 宠
    def test_a_fixed_slot_is_shown_but_greyed_out(self) -> None:
        self._load_standard()
        tab = self.select("武器", 3)
        self.assertIn("disabled", tab.slot_combos[1].state())
        self.assertIn("固定，不可修改", tab.slot_combos[1].get())
        self.assertIn("disabled", tab.value_entries[1].state())
        self.assertIn("不能改", tab.slot_labels[1].get())
        # 固定槽不参与改动收集。
        self.assertNotIn(1, [edit["slot_index"] for edit in tab.current_edits()])

    def test_a_star_slot_stays_editable(self) -> None:
        self._load({
            3: support.build_record(
                record_type=self.katana.item_id, level=150, rarity=4,
                effects=((self.melee_free.effect_id, self.melee_free.value, STAR_BIT),)),
        })
        tab = self.select("武器", 3)
        self.assertNotIn("disabled", tab.slot_combos[0].state())
        self.assertTrue(editor.EquipmentView.slot_is_star(
            next(v for v in tab.views if v.slot_index == 3), 0))
        self.assertIn("★ 词条（可改）", tab.slot_labels[0].get())

    def test_the_grace_slot_is_read_only_and_explained(self) -> None:
        self._load_standard()
        tab = self.select("武器", 3)
        index = tab.grace_slot(next(v for v in tab.views if v.slot_index == 3))
        self.assertEqual(index, 2)
        self.assertIn("disabled", tab.slot_combos[index].state())
        self.assertIn("只读", tab.slot_combos[index].get())
        self.assertIn("只显示", tab.grace_status_var.get())
        self.assertIn("只能有一个恩宠/套装", tab.grace_status_var.get())
        self.assertNotIn(index, [edit["slot_index"] for edit in tab.current_edits()])

    def test_an_out_of_pool_affix_is_reported_and_can_be_replaced(self) -> None:
        """占着槽位但不在本池词条表里的词条：如实说明，换掉它仍然可以。"""
        outside = self.affix.effect_id
        self.assertIsNone(self.melee.db.lookup(outside),
                          "这个 id 应当不在近战词条表里（测试前提）")
        self._load({
            3: support.build_record(
                record_type=self.katana.item_id, level=150, rarity=4,
                effects=((outside, self.affix.value, PLAIN),)),
        })
        tab = self.select("武器", 3)
        self.assertIn("非本池词条", tab.slot_combos[0].get())
        tab.slot_combos[0].set(self.melee_free.label)
        edits = tab.current_edits()
        self.assertEqual([edit["effect_id"] for edit in edits], [self.melee_free.effect_id])

    def _valued_affix(self):
        """一条真能取多个数值的近战词条（固定值的词条谈不上「只改数值」）。"""
        return _first(self.melee.db.all(),
                      lambda e: not e.is_fixed and e.value_min < e.value_max
                      and "近战" in self.melee.tags_of(e.effect_id))

    def _record_with(self, affix, value: int):
        return {3: support.build_record(
            record_type=self.katana.item_id, level=150, rarity=4,
            effects=((affix.effect_id, value, PLAIN),))}

    def test_a_value_only_change_is_collected_and_applied(self) -> None:
        """只改数值（词条不动）也要能收集、能写进字节。"""
        affix = self._valued_affix()
        self._load(self._record_with(affix, affix.value_min))
        tab = self.select("武器", 3)
        tab.value_vars[0].set(str(affix.value_max))
        edits = tab.current_edits()
        self.assertEqual(edits, ({"slot_index": 0, "effect_id": affix.effect_id,
                                  "value": affix.value_max, "record_index": 3},))
        tab.apply_edits()
        written = _first(editor.list_equipment(self.app.decrypted, big="武器",
                                               layout=self.app.layout,
                                               item_db=self.item_db),
                         lambda v: v.slot_index == 3)
        self.assertEqual(written.effects[0].effect_id, affix.effect_id)
        self.assertEqual(written.effects[0].value, affix.value_max)

    def test_an_out_of_range_value_is_refused_without_touching_the_bytes(self) -> None:
        affix = self._valued_affix()
        data, _layout = self._load(self._record_with(affix, affix.value_min))
        tab = self.select("武器", 3)
        tab.value_vars[0].set(str(affix.value_max + 1))
        with mock.patch.object(ui.messagebox, "showerror") as shown:
            tab.apply_edits()
        self.assertTrue(shown.called)
        self.assertEqual(self.app.decrypted, data)

    # ------------------------------------------------------ 应 用 / 预 览
    def test_applying_an_edit_matches_what_the_engine_plans(self) -> None:
        data, layout = self._load_standard()
        tab = self.select("武器", 4)
        replacement = _first(self.ranged.db.all(),
                             lambda e: not e.is_fixed
                             and e.effect_id != self.bow_only.effect_id
                             and e.label in tab._candidate_entries)
        tab.slot_combos[0].set(replacement.label)
        tab.value_vars[0].set(str(replacement.value))
        edits = tab.current_edits()
        self.assertEqual(len(edits), 1)
        expected = editor.apply_equipment_edits(
            data, edits, item_db=self.item_db, pools=self.app.equipment_pools,
            grace_db=self.grace_db, layout=layout)
        tab.apply_edits()
        self.assertEqual(self.app.decrypted, expected)
        written = _first(editor.list_equipment(self.app.decrypted, big="武器",
                                               layout=layout, item_db=self.item_db),
                         lambda v: v.slot_index == 4)
        self.assertEqual(written.effects[0].effect_id, replacement.effect_id)
        self.assertEqual(written.effects[0].value, replacement.value)

    def test_clearing_a_slot_removes_the_affix(self) -> None:
        self._load_standard()
        tab = self.select("武器", 4)
        tab.slot_combos[0].set(ui.EMPTY_LABEL)
        edits = tab.current_edits()
        self.assertEqual(edits, ({"slot_index": 0, "effect_id": records.EMPTY_EFFECT_ID,
                                  "record_index": 4},))
        tab.apply_edits()
        written = _first(editor.list_equipment(self.app.decrypted, big="武器",
                                               layout=self.app.layout,
                                               item_db=self.item_db),
                         lambda v: v.slot_index == 4)
        self.assertTrue(written.effects[0].is_empty)

    def test_an_edit_that_the_engine_refuses_does_not_change_the_bytes(self) -> None:
        data, _layout = self._load_standard()
        tab = self.select("武器", 4)
        with mock.patch.object(ui.messagebox, "showerror") as shown:
            # 直接塞一条近战专属词条（绕过候选列表）——引擎必须拒绝。
            with mock.patch.object(tab, "current_edits", return_value=(
                    {"record_index": 4, "slot_index": 0,
                     "effect_id": self.melee_only.effect_id,
                     "value": self.melee_only.value},)):
                tab.apply_edits()
        self.assertTrue(shown.called)
        self.assertEqual(self.app.decrypted, data)

    def test_preview_reports_the_plan_without_writing(self) -> None:
        data, _layout = self._load_standard()
        tab = self.select("武器", 4)
        tab.slot_combos[0].set(ui.EMPTY_LABEL)
        tab.preview_edits()
        self.assertIn(ui.EMPTY_LABEL, tab.detail_var.get())
        self.assertIn("尚未写入", tab.detail_var.get())
        self.assertEqual(self.app.decrypted, data)

    def test_switching_records_resets_the_slot_widgets(self) -> None:
        self._load_standard()
        tab = self.select("武器", 3)
        tab.slot_combos[0].set("星")
        tab._on_slot_typed(0)
        tab = self.select("武器", 4)
        self.assertEqual(len(tab.slot_combos[0]["values"]), len(tab._candidates))
        self.assertEqual(tab.slot_combos[0].get(), self.bow_only.label)

    # -------------------------------------------------------- 等 级 / +值
    def test_level_and_plus_frames_show_the_caps_from_the_engine(self) -> None:
        self._load_standard()
        tab = self.select("武器", 3)
        limits = editor.class_limits_for_record(self.katana.item_id,
                                                item_db=self.item_db)
        self.assertEqual(limits.max_level, 180)
        self.assertEqual(limits.max_plus, 30)
        self.assertIn("180", tab.level_frame.cget("text"))
        self.assertIn("30", tab.plus_frame.cget("text"))
        self.assertEqual(tab.level_var.get(), "150")
        self.assertEqual(tab.plus_var.get(), "3")

    def test_level_180_is_accepted_and_181_is_refused(self) -> None:
        self._load_standard()
        tab = self.select("武器", 3)
        with mock.patch.object(ui.messagebox, "askokcancel", return_value=True):
            tab.level_var.set("180")
            tab.apply_level()
        written = _first(editor.list_equipment(self.app.decrypted, big="武器",
                                               layout=self.app.layout,
                                               item_db=self.item_db),
                         lambda v: v.slot_index == 3)
        self.assertEqual(written.level, 180)
        with mock.patch.object(ui.messagebox, "askokcancel", return_value=True), \
                mock.patch.object(ui.messagebox, "showerror") as shown:
            tab.level_var.set("181")
            tab.apply_level()
        self.assertTrue(shown.called)
        self.assertIn("180", str(shown.call_args))

    def test_plus_30_is_accepted_and_31_is_refused(self) -> None:
        self._load_standard()
        tab = self.select("武器", 3)
        with mock.patch.object(ui.messagebox, "askokcancel", return_value=True):
            tab.plus_var.set("30")
            tab.apply_plus()
        written = _first(editor.list_equipment(self.app.decrypted, big="武器",
                                               layout=self.app.layout,
                                               item_db=self.item_db),
                         lambda v: v.slot_index == 3)
        self.assertEqual(written.plus_value, 30)
        with mock.patch.object(ui.messagebox, "askokcancel", return_value=True), \
                mock.patch.object(ui.messagebox, "showerror") as shown:
            tab.plus_var.set("31")
            tab.apply_plus()
        self.assertTrue(shown.called)

    def test_a_cancelled_confirmation_changes_nothing(self) -> None:
        data, _layout = self._load_standard()
        tab = self.select("武器", 3)
        with mock.patch.object(ui.messagebox, "askokcancel", return_value=False):
            tab.level_var.set("170")
            tab.apply_level()
            tab.plus_var.set("10")
            tab.apply_plus()
        self.assertEqual(self.app.decrypted, data)

    # ------------------------------------------------------- 无 中 生 有
    def test_create_uses_a_same_kind_template_and_reports_it(self) -> None:
        self._load_standard()
        tab = self.tab("武器")
        tab.create_kind_combo.set(self.katana.label)
        tab.create_level_var.set("170")
        tab.create_plus_var.set("5")
        with mock.patch.object(ui.messagebox, "askokcancel", return_value=True):
            tab.create_item()
        created = [view for view in tab.views
                   if view.record_type == self.katana.item_id and view.slot_index != 3]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].level, 170)
        self.assertEqual(created[0].plus_value, 5)
        self.assertIn("模板 #", tab.create_status_var.get())
        self.assertIn(self.katana.name, tab.create_status_var.get())
        self.assertIn("已新建", self.app.status_var.get())

    def test_create_reports_the_donor_fallback_when_the_kind_is_absent(self) -> None:
        """同种类模板不存在时退回同类型：退回原因必须显示出来（不能默默替换）。"""
        self._load_standard()
        tab = self.tab("武器")
        other = _first((entry for entry in self.item_db.all()
                        if entry.big == "武器" and entry.small == "刀"
                        and entry.item_id != self.katana.item_id),
                       lambda entry: True)
        tab.create_kind_combo.set(other.label)
        with mock.patch.object(ui.messagebox, "askokcancel", return_value=True):
            tab.create_item()
        self.assertIn("模板 #", tab.create_status_var.get())
        self.assertIn("刀", tab.create_status_var.get())

    def test_create_refuses_when_there_is_no_free_slot(self) -> None:
        data, _layout = self._load_standard()
        tab = self.tab("武器")
        tab.create_kind_combo.set(self.katana.label)
        with mock.patch.object(ui, "plan_create_equipment",
                               side_effect=editor.CreationError("背包里没有空槽")), \
                mock.patch.object(ui.messagebox, "showerror") as shown:
            tab.create_item()
        self.assertTrue(shown.called)
        self.assertEqual(self.app.decrypted, data)

    def test_the_tab_goes_read_only_when_the_tables_are_missing(self) -> None:
        tab = self.tab("武器")
        tab.set_error("武器页签只读：随包的物品/词条表不可用")
        self.assertIn("disabled", tab.create_button.state())
        self.assertIn("disabled", tab.preview_button.state())

    def test_the_tab_reports_that_the_editor_column_scrolls(self) -> None:
        """新页签自带滚动列，但不占用 app.scroll_columns（那两条是饰品/魂核的）。"""
        self.assertEqual(len(self.app.scroll_columns), 2)
        for tab in self.app.equipment_tabs:
            self.assertIsInstance(tab.winfo_children()[0], ui.ttk.Panedwindow)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
