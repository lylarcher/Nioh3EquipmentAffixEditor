"""Affix catalog tests: schema validation, lookups, flag decoding."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from nioh3_accessory_editor.affixdb import (
    DEFAULT_CATALOG,
    DEFAULT_GRACE_CATALOG,
    FLAG_FIXED,
    FLAG_STAR,
    AffixDb,
    AffixEntry,
    AffixError,
    GraceDb,
    load_catalog,
    load_grace_catalog,
    save_catalog,
    save_grace_catalog,
)


class RealCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = AffixDb()

    def test_catalog_is_present_and_parses(self) -> None:
        self.assertTrue(DEFAULT_CATALOG.is_file())
        self.assertGreater(len(self.db), 100)

    def test_every_entry_is_well_formed(self) -> None:
        for entry in self.db.all():
            self.assertIsInstance(entry, AffixEntry)
            self.assertTrue(entry.name.strip())
            self.assertTrue(entry.category.strip())
            self.assertTrue(0 <= entry.effect_id <= 0xFFFFFFFF)
            self.assertTrue(0 <= entry.value <= 0xFFFFFFFF)

    def test_ids_are_unique(self) -> None:
        ids = [entry.effect_id for entry in self.db.all()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_lookup_and_require(self) -> None:
        entry = self.db.all()[0]
        self.assertIs(self.db.lookup(entry.effect_id), entry)
        self.assertIs(self.db.require(entry.effect_id), entry)
        self.assertTrue(self.db.is_legal(entry.effect_id))
        self.assertIn(entry.effect_id, self.db)

    def test_require_rejects_unknown_id(self) -> None:
        with self.assertRaises(AffixError):
            self.db.require(0xDEADBEEF)
        self.assertIsNone(self.db.lookup(0xDEADBEEF))
        self.assertFalse(self.db.is_legal(0xDEADBEEF))

    def test_describe(self) -> None:
        entry = self.db.all()[0]
        self.assertEqual(self.db.describe(entry.effect_id), entry.name)
        self.assertIn("未知词条", self.db.describe(0xDEADBEEF))

    def test_labels_start_with_the_empty_choice(self) -> None:
        labels = self.db.labels()
        self.assertEqual(labels[0], "(空)")
        self.assertEqual(len(labels), len(self.db) + 1)
        for entry in self.db.all():
            self.assertIn(entry.label, labels)

    def test_categories_and_filtering(self) -> None:
        categories = self.db.categories()
        self.assertTrue(categories)
        self.assertEqual(len(categories), len(set(categories)))
        total = sum(len(self.db.by_category(name)) for name in categories)
        self.assertEqual(total, len(self.db))
        self.assertEqual(self.db.by_category(None), self.db.all())

    def test_flag_decoding_matches_the_code_layout(self) -> None:
        fixed = [entry for entry in self.db.all() if entry.is_fixed]
        starred = [entry for entry in self.db.all() if entry.is_star]
        self.assertTrue(fixed, "expected 同名固定 variants in the catalog")
        self.assertTrue(starred, "expected 星号 variants in the catalog")
        for entry in fixed:
            self.assertTrue(entry.flags & FLAG_FIXED)
        for entry in starred:
            self.assertTrue(entry.flags & FLAG_STAR)
            self.assertFalse(entry.flags & FLAG_FIXED)


class ValidationTests(unittest.TestCase):
    def _write(self, payload: object) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="nioh3-affixdb-"))
        path = directory / "catalog.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_missing_file(self) -> None:
        with self.assertRaises(AffixError):
            load_catalog(Path(tempfile.gettempdir()) / "definitely-missing.json")

    def test_malformed_json(self) -> None:
        directory = Path(tempfile.mkdtemp(prefix="nioh3-affixdb-"))
        path = directory / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(AffixError):
            load_catalog(path)

    def test_non_object_payload(self) -> None:
        with self.assertRaises(AffixError):
            load_catalog(self._write([1, 2, 3]))

    def test_missing_affix_list(self) -> None:
        with self.assertRaises(AffixError):
            load_catalog(self._write({"affixes": []}))
        with self.assertRaises(AffixError):
            load_catalog(self._write({"count": 1}))

    def test_count_mismatch(self) -> None:
        entry = {"effect_id": 1, "value": 2, "flags": 0, "name": "x", "category": "y"}
        with self.assertRaises(AffixError):
            load_catalog(self._write({"count": 5, "affixes": [entry]}))

    def test_entry_field_types(self) -> None:
        good = {"effect_id": 1, "value": 2, "flags": 0, "name": "x", "category": "y"}
        for key, bad in (("effect_id", "1"), ("value", None), ("flags", 1.5),
                         ("effect_id", -1), ("value", 0x100000000)):
            payload = {"count": 1, "affixes": [dict(good, **{key: bad})]}
            with self.assertRaises(AffixError):
                load_catalog(self._write(payload))

    def test_entry_names_and_categories(self) -> None:
        base = {"effect_id": 1, "value": 2, "flags": 0, "category": "y"}
        for override in ({"name": ""}, {"name": None}, {"name": "ok", "category": 3}):
            payload = {"count": 1, "affixes": [dict(base, **override)]}
            with self.assertRaises(AffixError):
                load_catalog(self._write(payload))

    def test_entry_must_be_an_object(self) -> None:
        with self.assertRaises(AffixError):
            load_catalog(self._write({"count": 1, "affixes": ["nope"]}))

    def test_duplicate_ids_are_rejected(self) -> None:
        entry = AffixEntry(0x10, 1, 0, "a", "b")
        with self.assertRaises(AffixError):
            AffixDb([entry, entry])

    def test_affix_db_from_file_and_default(self) -> None:
        db = AffixDb.from_file(DEFAULT_CATALOG)
        self.assertEqual(len(db), len(AffixDb()))

    def test_save_catalog_round_trip(self) -> None:
        directory = Path(tempfile.mkdtemp(prefix="nioh3-affixdb-"))
        path = directory / "out.json"
        entries = [AffixEntry(0x20, 7, FLAG_STAR, "测试", "类别")]
        save_catalog(entries, path, source="unit-test")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["schema"], "nioh3-accessory-affixes/v1")
        reloaded = load_catalog(path)
        self.assertEqual(reloaded, entries)

    def test_save_catalog_rejects_duplicates(self) -> None:
        directory = Path(tempfile.mkdtemp(prefix="nioh3-affixdb-"))
        entry = AffixEntry(0x20, 7, 0, "测试", "类别")
        with self.assertRaises(AffixError):
            save_catalog([entry, entry], directory / "out.json")


class GraceCatalogTests(unittest.TestCase):
    """恩宠/套装 names, used to label an accessory's last effect slot.

    The workbook lists them in 词条总目录 (归属 = [恩宠] / [上位恩宠] / [武士套装] /
    [忍者套装]); they are not legal accessory affixes, so they must never end up in
    the edit catalog.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = GraceDb.best_effort()

    def test_table_is_present_and_parses(self) -> None:
        self.assertTrue(DEFAULT_GRACE_CATALOG.is_file())
        self.assertTrue(self.db.is_loaded, self.db.error)
        self.assertEqual(self.db.error, "")
        self.assertGreater(len(self.db), 50)

    def test_names_the_id_seen_in_a_real_save(self) -> None:
        # Record #3 of the reporting user's save holds 0x71f6 in its last slot.
        self.assertEqual(self.db.describe(0x71F6), "不动明王的恩宠（上位恩宠）")
        self.assertEqual(self.db.describe(0x4FA3), "稻荷神的恩宠（恩宠）")

    def test_covers_both_grace_and_set_kinds(self) -> None:
        kinds = {entry.category for entry in self.db.all()}
        self.assertEqual(kinds, {"恩宠", "上位恩宠", "武士套装", "忍者套装"})

    def test_unknown_id_has_no_name(self) -> None:
        self.assertIsNone(self.db.describe(0xDEADBEEF))
        self.assertNotIn(0xDEADBEEF, self.db)

    def test_grace_ids_are_not_legal_edits(self) -> None:
        """Display must not widen what may be written."""
        legal = AffixDb()
        for entry in self.db.all():
            self.assertNotIn(entry.effect_id, legal, f"{entry.effect_id:#x}")

    def test_duplicate_ids_in_the_table_are_deduped_not_fatal(self) -> None:
        entry = AffixEntry(0x11, 0, 0, "稻荷神的恩宠", "恩宠")
        db = GraceDb([entry, entry])
        self.assertEqual(len(db), 2)
        self.assertEqual(db.describe(0x11), "稻荷神的恩宠（恩宠）")

    def test_empty_table_reports_no_names_instead_of_failing(self) -> None:
        db = GraceDb.best_effort(Path(tempfile.gettempdir()) / "missing-grace.json")
        self.assertFalse(db.is_loaded)
        self.assertTrue(db.error)
        self.assertIsNone(db.describe(0x71F6))

    def test_save_grace_catalog_round_trip(self) -> None:
        directory = Path(tempfile.mkdtemp(prefix="nioh3-grace-"))
        path = directory / "grace.json"
        entries = [AffixEntry(0x4FA3, 0, 0, "稻荷神的恩宠", "恩宠")]
        save_grace_catalog(entries, path, source="unit-test")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], "nioh3-grace-affixes/v1")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(load_grace_catalog(path), entries)

    def test_a_missing_affix_list_is_rejected(self) -> None:
        directory = Path(tempfile.mkdtemp(prefix="nioh3-grace-"))
        path = directory / "bad.json"
        path.write_text('{"count": 0}', encoding="utf-8")
        with self.assertRaises(AffixError):
            load_grace_catalog(path)


if __name__ == "__main__":
    unittest.main()
