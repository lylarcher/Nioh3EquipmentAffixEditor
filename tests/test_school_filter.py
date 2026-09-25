"""饰品页签的「武士/忍者」筛选（需求 2）。"""

from __future__ import annotations

import unittest
from unittest import mock

from nioh3_equipment_affix_editor import editor, ui
from tests import support

known_ids = editor.accessory_catalog_ids(affix_db=None) if False else None


class SchoolFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with mock.patch.object(ui.AccessoryEditorApp, "refresh_saves", lambda self: None):
            cls.app = ui.AccessoryEditorApp()
        cls.app.withdraw()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.app.destroy()

    def setUp(self) -> None:
        self.app.accessory_views = []
        self.app.kind_filter_var.set(self.app.ALL_FILTER)
        self.app.grace_filter_var.set(self.app.ALL_FILTER)
        self.app.school_filter_var.set(self.app.ALL_FILTER)

    def _views(self):
        """真实视图：用物品总目录里的 id 造两条记录（一条武士、一条忍者）。"""
        from nioh3_equipment_affix_editor import records

        picked = {}
        for entry in self.app.item_db.all():
            school = (entry.category or "").replace("饰品", "")
            if school and school not in picked:
                picked[school] = entry
        self.assertGreaterEqual(len(picked), 2)
        slots = {}
        for index, (school, entry) in enumerate(sorted(picked.items())):
            slots[index] = support.build_record(record_type=entry.item_id, level=170,
                                                rarity=5)
        save = support.build_plain_save(records_by_slot=slots)
        layout = records.locate_layout(save)
        views = list(editor.list_accessories(save, layout=layout, known_ids=known_ids))
        self.assertEqual(len(views), len(slots))
        return {self.app._school_of(view): view for view in views}

    def test_the_combo_offers_every_school(self) -> None:
        values = self.app._school_filter_values()
        self.assertIn("武士", values)
        self.assertIn("忍者", values)
        self.app._refresh_accessory_tree()
        listed = self.app.school_filter_combo.cget("values")
        self.assertIn("武士", listed)
        self.assertIn("忍者", listed)

    def test_picking_a_school_filters_the_tree(self) -> None:
        views = self._views()
        self.app.accessory_views = list(views.values())
        self.app.school_filter_var.set("武士")
        self.app._refresh_accessory_tree()
        shown = self.app.tree.get_children()
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0], str(views["武士"].slot_index))

    def test_clear_filters_restores_everything(self) -> None:
        views = self._views()
        self.app.accessory_views = list(views.values())
        self.app.school_filter_var.set("忍者")
        self.app._refresh_accessory_tree()
        self.assertEqual(len(self.app.tree.get_children()), 1)
        self.app.clear_filters()
        self.assertEqual(len(self.app.tree.get_children()), len(views))

    def test_the_school_filter_cascades_with_the_others(self) -> None:
        """选了武士之后仍然可以切到忍者；被别的筛选排除的取值才标「当前无记录」。"""
        views = self._views()
        self.app.accessory_views = list(views.values())
        self.app.school_filter_var.set("武士")
        self.app._refresh_accessory_tree()
        listed = self.app.school_filter_combo.cget("values")
        self.assertIn("忍者", listed)  # 还能切换
        # 种类 = 武士的那件 + 武士/忍者 = 忍者 是不可能组合：种类下拉要标出来。
        samurai_label = next(label for label, entry in self.app.item_choices.items()
                             if entry.item_id == views["武士"].record_type)
        self.app.school_filter_var.set("忍者")
        self.app.kind_filter_var.set(samurai_label)
        self.app._refresh_accessory_tree()
        kinds = self.app.kind_filter_combo.cget("values")
        self.assertIn(f"{samurai_label}（当前无记录）", kinds)

    def test_an_unknown_school_keeps_the_choice_visible(self) -> None:
        self.app.accessory_views = list(self._views().values())
        self.app.school_filter_var.set("阴阳师")
        self.app._refresh_accessory_tree()
        listed = self.app.school_filter_combo.cget("values")
        self.assertIn("阴阳师（当前无记录）", listed)


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
