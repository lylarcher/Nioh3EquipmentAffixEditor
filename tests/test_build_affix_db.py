"""Tests for tools/build_affix_db.py: catalog generation from the workbook.

Two things must hold, and both have already gone wrong in practice:

* the *committed* catalogs must still match what the committed workbook produces
  (a stale catalog silently labels real affixes as unknown), and
* 恩宠/套装 names must be collected from 词条总目录 into their own display-only
  table, never into the legal-edit catalog (armour carries 套装 codes too, so a
  grace id is not evidence of an accessory affix).
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

from nioh3_equipment_affix_editor.affixdb import (
    DEFAULT_CATALOG,
    DEFAULT_GRACE_CATALOG,
    DEFAULT_ITEM_CATALOG,
    DEFAULT_SOUL_CATALOG,
    GRACE_CATALOG_SCHEMA,
    ITEM_CATALOG_SCHEMA,
    AffixDb,
    load_catalog,
    load_grace_catalog,
    load_item_catalog,
    load_soul_catalog,
)

_ROOT = Path(__file__).resolve().parent.parent
_NAME = "build_affix_db_tool"
_SPEC = importlib.util.spec_from_file_location(_NAME,
                                              _ROOT / "tools" / "build_affix_db.py")
build_affix_db = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
sys.modules[_NAME] = build_affix_db
_SPEC.loader.exec_module(build_affix_db)

XLSX = _ROOT / "third_party" / "source-data" / "仁王3词条装备库v2.21.xlsx"


def workbook_rows(sheet: str) -> list[list[str]]:
    """Read one sheet from whichever source the tool resolves (xlsx or TSV)."""
    source = build_affix_db.resolve_source()
    if source.suffix.lower() == ".tsv":
        return build_affix_db._rows_from_tsv(source)
    return build_affix_db._rows_from_xlsx(source, sheet)


class GraceOwnerTests(unittest.TestCase):
    def test_bracketed_owners_are_recognised(self) -> None:
        self.assertEqual(build_affix_db.parse_grace_owner("[恩宠]"), "恩宠")
        self.assertEqual(build_affix_db.parse_grace_owner("[上位恩宠]"), "上位恩宠")
        self.assertEqual(build_affix_db.parse_grace_owner(" 武士套装 "), "武士套装")

    def test_other_owners_are_not_grace(self) -> None:
        for owner in ("[饰品]", "[近战]", "[魂核]", "", "[无]", "[头部/手臂]"):
            self.assertIsNone(build_affix_db.parse_grace_owner(owner), owner)


@unittest.skipUnless(XLSX.is_file(), "缺少原始数据表")
class GraceCollectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.entries, cls.skipped = build_affix_db.collect_grace_entries(XLSX)
        cls.by_id = {entry.effect_id: entry for entry in cls.entries}

    def test_collects_every_grace_and_set_row(self) -> None:
        self.assertEqual(len(self.entries), 77)
        kinds = {entry.category for entry in self.entries}
        self.assertEqual(kinds, {"恩宠", "上位恩宠", "武士套装", "忍者套装"})

    def test_names_the_id_a_real_save_carried(self) -> None:
        self.assertEqual(self.by_id[0x71F6].name, "不动明王的恩宠")
        self.assertEqual(self.by_id[0x4FA3].name, "稻荷神的恩宠")

    def test_ids_are_unique_and_non_empty(self) -> None:
        self.assertEqual(len(self.by_id), len(self.entries))
        for entry in self.entries:
            self.assertTrue(entry.name.strip())
            self.assertNotEqual(entry.effect_id, 0xFFFFFFFF)

    def test_grace_ids_are_outside_the_legal_catalog(self) -> None:
        legal = {entry.effect_id for entry in load_catalog(DEFAULT_CATALOG)}
        for entry in self.entries:
            self.assertNotIn(entry.effect_id, legal, f"{entry.effect_id:#x}")


@unittest.skipUnless(XLSX.is_file(), "缺少原始数据表")
class ItemCollectionTests(unittest.TestCase):
    """物品总目录's 饰品 rows: what an accessory *is* (display only)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.entries, cls.conflicts, cls.skipped = \
            build_affix_db.collect_item_entries(XLSX)
        cls.by_id = {entry.item_id: entry for entry in cls.entries}

    def test_collects_every_accessory_item_row(self) -> None:
        # 88 workbook rows plus 0x5c5f, which the sheet cannot express (both 八咫镜
        # rows carry 代码 `21 15`) and which comes from save evidence instead.
        self.assertEqual(len(self.entries), 89)
        self.assertEqual({entry.category for entry in self.entries},
                         {"武士饰品", "忍者饰品"})

    def test_the_unexpressible_yatagami_id_is_added_from_save_evidence(self) -> None:
        self.assertEqual(self.by_id[0x5C5F].name, "八咫镜[武士]")
        self.assertEqual(self.by_id[0x5C5F].category, "武士饰品")
        self.assertIn("存档实测", self.by_id[0x5C5F].source)
        # ...and the row the workbook *does* have is renamed to the 忍者 version,
        # because 0x1521's own fixed affix is 识破可增加灵力 on the real save.
        self.assertEqual(self.by_id[0x1521].name, "八咫镜[忍者]")
        self.assertEqual(self.by_id[0x1521].category, "忍者饰品")

    def test_workbook_rows_other_than_yatagami_keep_their_source_empty(self) -> None:
        self.assertEqual(self.by_id[0x3E3F].source, "")
        self.assertEqual(self.by_id[0x4987].source, "")

    def test_names_the_items_a_real_save_carried(self) -> None:
        # Measured: record #3 (the 龙笛 the user saw 不动明王的恩宠 on) and #4
        # (凶王耳饰 with 怨恨盖世) both resolve through these ids.
        self.assertEqual(self.by_id[0x3E3F].name, "龙笛[武士]")
        self.assertEqual(self.by_id[0xF5BB].name, "凶王耳饰[忍者]")
        self.assertEqual(self.by_id[0x4987].name, "八尺琼勾玉[武士]")

    def test_sheet_byte_order_is_swapped(self) -> None:
        """``BB F5`` in the sheet is the save's ``0xF5BB``, not ``0xBBF5``."""
        self.assertEqual(build_affix_db.parse_item_code("BB F5"), 0xF5BB)
        self.assertEqual(build_affix_db.parse_item_code("bb f5"), 0xF5BB)
        self.assertIsNone(build_affix_db.parse_item_code(""))
        self.assertIsNone(build_affix_db.parse_item_code("武器"))
        self.assertIsNone(build_affix_db.parse_item_code("AA BB CC"))

    def test_ids_are_unique_and_never_affix_ids(self) -> None:
        self.assertEqual(len(self.by_id), len(self.entries))
        legal = {entry.effect_id for entry in load_catalog(DEFAULT_CATALOG)}
        graces = {entry.effect_id for entry in load_grace_catalog(DEFAULT_GRACE_CATALOG)}
        for entry in self.entries:
            self.assertNotIn(entry.item_id, legal, f"{entry.item_id:#x}")
            self.assertNotIn(entry.item_id, graces, f"{entry.item_id:#x}")

    def test_duplicate_ids_are_reported_instead_of_hidden(self) -> None:
        """0x1521 is both 八咫镜[武士] and [忍者]: keep it visible."""
        self.assertEqual(len(self.conflicts), 1)
        self.assertIn("0x1521", self.conflicts[0])
        self.assertIn("八咫镜", self.conflicts[0])


@unittest.skipUnless(XLSX.is_file(), "缺少原始数据表")
class GreenSheetTests(unittest.TestCase):
    """绿色星号词条: the ★ affixes that may roll on 饰品 / 魂核.

    They live in their own sheet with the equipment kinds in column A, their value
    span in E/F and the value set in G.., and none of their ids appears in 饰品词条
    or 绘卷-魂核词条 — so skipping the sheet silently removed legal affixes from the
    editor's catalog.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = build_affix_db.resolve_source()
        cls.entries, cls.skipped = build_affix_db.collect_green_entries(cls.source)
        cls.rows = workbook_rows(build_affix_db.GREEN_SHEET)[1:]

    def test_both_catalogs_got_their_rows(self) -> None:
        self.assertEqual(len(self.entries["饰品"]), 32)
        self.assertEqual(len(self.entries["魂核"]), 51)

    def test_the_committed_catalogs_contain_them(self) -> None:
        accessory = {entry.effect_id for entry in load_catalog(DEFAULT_CATALOG)}
        soul = {entry.effect_id for entry in load_soul_catalog(DEFAULT_SOUL_CATALOG)}
        for entry in self.entries["饰品"]:
            self.assertIn(entry.effect_id, accessory)
        for entry in self.entries["魂核"]:
            self.assertIn(entry.effect_id, soul)
        # 276 + 32 and 287 + 51, i.e. the sheet's rows are additive.
        self.assertEqual(len(accessory), 308)
        self.assertEqual(len(soul), 338)

    def test_an_id_shared_by_both_kinds_is_in_both(self) -> None:
        accessory = {entry.effect_id for entry in self.entries["饰品"]}
        soul = {entry.effect_id for entry in self.entries["魂核"]}
        shared = accessory & soul
        self.assertEqual(len(shared), 3)
        catalog = {entry.effect_id for entry in load_catalog(DEFAULT_CATALOG)}
        for effect_id in shared:
            self.assertIn(effect_id, catalog)

    def test_every_entry_carries_the_sheets_own_span(self) -> None:
        for kind, entries in self.entries.items():
            for entry in entries:
                with self.subTest(kind=kind, effect_id=entry.effect_id):
                    self.assertTrue(entry.has_value_range, entry.name)
                    self.assertLessEqual(entry.value_min, entry.value_max)
                    self.assertLessEqual(entry.value_min, entry.value)
                    self.assertLessEqual(entry.value, entry.value_max)

    def test_the_sheets_value_sets_are_kept(self) -> None:
        """数值集合 is a *set*: 3 rows step by 2.5, so min/max alone is not enough."""
        stepped, listed = [], 0
        for row in self.rows:
            low, high = row[4].strip(), row[5].strip()
            if not low.isdigit() or not high.isdigit():
                continue
            span = build_affix_db.parse_value_set(row[6:], int(low), int(high))
            values = sorted({int(piece) for cell in row[6:]
                             for piece in str(cell).replace("|", " ").split()
                             if piece.isdigit()})
            self.assertTrue(values, f"每一行都应给出数值集合：{row[3]}")
            listed += 1
            if span is None:
                self.assertEqual(values, list(range(int(low), int(high) + 1)), row[3])
            else:
                stepped.append((row[3], span))
                self.assertEqual(list(span), values)
        self.assertEqual(listed, 257)
        # 33 of the 257 rows are stepped sets (a 2.5 step in 0.1% units); 3 of
        # them belong to 饰品, which is why the accessory catalog carries 3.
        self.assertEqual(len(stepped), 33, [item[0] for item in stepped])

    def test_a_stepped_set_refuses_the_values_between_its_steps(self) -> None:
        db = AffixDb()
        entry = next(item for item in db.all() if item.values)
        self.assertEqual(entry.values[:3], (380, 382, 385))
        self.assertTrue(entry.allows_value(380))
        self.assertFalse(entry.allows_value(381))
        self.assertIn("共 21 个取值", entry.describe_value_range())

    def test_an_encoded_value_outside_its_span_is_snapped_into_it(self) -> None:
        """3 of 257 rows encode a value their own span excludes; the catalog must not
        ship a default the editor would then refuse to write."""
        published = {entry.effect_id: entry
                     for entry in load_catalog(DEFAULT_CATALOG)}
        entry = published.get(0x248C)
        self.assertIsNotNone(entry, "0x248c 应在饰品目录里（[饰品] 行）")
        self.assertTrue(entry.has_value_range)
        self.assertTrue(entry.value_min <= entry.value <= entry.value_max
                        or (entry.values and entry.value in entry.values))

    def test_a_row_is_tagged_with_the_star_bit(self) -> None:
        from nioh3_equipment_affix_editor.affixdb import FLAG_STAR

        for entries in self.entries.values():
            for entry in entries:
                self.assertTrue(entry.flags & FLAG_STAR, entry.name)
                self.assertTrue(entry.is_star and not entry.is_fixed, entry.name)
                self.assertIn("(星)", entry.name)

    def test_melee_only_rows_stay_out_of_the_accessory_catalog(self) -> None:
        """[近战/手臂] and friends must never become a legal 饰品 affix."""
        accessory = {entry.effect_id for entry in load_catalog(DEFAULT_CATALOG)}
        offenders = []
        for row in self.rows:
            if len(row) < 4:
                continue
            kinds = [part.strip() for part in row[0].strip().strip("[]").split("/")]
            if "饰品" in kinds:
                continue
            try:
                effect_id = build_affix_db.parse_code(row[2].strip())[0]
            except ValueError:
                continue
            if effect_id in accessory:
                offenders.append((row[0], row[3]))
        self.assertEqual(offenders, [])

    def test_the_two_original_sheets_do_not_contain_these_ids(self) -> None:
        """Measured: the green ids are a separate space, so nothing was overwritten."""
        plain = set()
        for row in workbook_rows(build_affix_db.TARGET_SHEET)[1:]:
            if len(row) >= 2 and row[1].strip():
                try:
                    plain.add(build_affix_db.parse_code(row[1].strip())[0])
                except ValueError:
                    continue
        soul = {entry.effect_id
                for entry in build_affix_db.collect_soul_entries(self.source)[0]}
        for entries in self.entries.values():
            for entry in entries:
                self.assertNotIn(entry.effect_id, plain)
                self.assertNotIn(entry.effect_id, soul)

    def test_a_real_green_affix_gets_a_working_value_range(self) -> None:
        db = AffixDb()
        entry = next(item for item in db.all()
                     if item.is_star and item.effect_id == 0x496E)
        self.assertEqual((entry.value_min, entry.value_max), (80, 100))
        self.assertTrue(entry.allows_value(90))
        self.assertFalse(entry.allows_value(101))
        self.assertEqual(entry.describe_value_range(), "80..100")


class CommittedCatalogTests(unittest.TestCase):
    """The committed JSON must be what the committed workbook produces."""

    def test_accessory_catalog_is_not_stale(self) -> None:
        rows = workbook_rows(build_affix_db.TARGET_SHEET)
        produced: dict[int, tuple[int, int, str, str]] = {}
        for row in rows[1:]:
            if len(row) < 3:
                continue
            category, code, name = row[0].strip(), row[1].strip(), row[2].strip()
            if not name or not code:
                continue
            try:
                effect_id, value, flags = build_affix_db.parse_code(code)
            except (ValueError, TypeError):
                continue
            if effect_id == 0xFFFFFFFF or effect_id in produced:
                continue
            produced[effect_id] = (value, flags, name, category)

        committed = {entry.effect_id: (entry.value, entry.flags, entry.name,
                                       entry.category)
                     for entry in load_catalog(DEFAULT_CATALOG)}
        # The committed catalog is 饰品词条 **plus** the 绿色星号词条 rows that may roll
        # on 饰品 (column A carries the equipment kinds); see GreenSheetTests.
        green, _skipped = build_affix_db.collect_green_entries(
            build_affix_db.resolve_source())
        for entry in green["饰品"]:
            produced.setdefault(entry.effect_id, (entry.value, entry.flags,
                                                  entry.name, entry.category))
        self.assertEqual(committed, produced,
                         "data/accessory_affixes.json 与原始表不一致；"
                         "请重新运行 tools/build_affix_db.py")

    def test_grace_catalog_is_not_stale(self) -> None:
        entries, _skipped = build_affix_db.collect_grace_entries(XLSX)
        committed = {entry.effect_id: (entry.value, entry.flags, entry.name,
                                       entry.category)
                     for entry in load_grace_catalog(DEFAULT_GRACE_CATALOG)}
        self.assertEqual(committed,
                         {entry.effect_id: (entry.value, entry.flags, entry.name,
                                            entry.category)
                          for entry in entries},
                         "data/grace_affixes.json 与原始表不一致；"
                         "请重新运行 tools/build_affix_db.py")

    def test_grace_catalog_declares_its_own_schema(self) -> None:
        import json

        payload = json.loads(DEFAULT_GRACE_CATALOG.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], GRACE_CATALOG_SCHEMA)

    def test_item_catalog_is_not_stale(self) -> None:
        entries, conflicts, _skipped = build_affix_db.collect_item_entries(XLSX)
        committed = {entry.item_id: (entry.name, entry.category)
                     for entry in load_item_catalog(DEFAULT_ITEM_CATALOG)}
        self.assertEqual(committed,
                         {entry.item_id: (entry.name, entry.category)
                          for entry in entries},
                         "data/accessory_items.json 与原始表不一致；"
                         "请重新运行 tools/build_affix_db.py")
        import json

        payload = json.loads(DEFAULT_ITEM_CATALOG.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], ITEM_CATALOG_SCHEMA)
        self.assertEqual(payload["conflicts"], conflicts)
        self.assertEqual(payload["count"], len(entries))


if __name__ == "__main__":
    unittest.main()
