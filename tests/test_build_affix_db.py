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

from nioh3_accessory_editor.affixdb import (
    DEFAULT_CATALOG,
    DEFAULT_GRACE_CATALOG,
    GRACE_CATALOG_SCHEMA,
    load_catalog,
    load_grace_catalog,
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


if __name__ == "__main__":
    unittest.main()
