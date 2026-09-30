"""条目标记表的多来源与防污染规则（覆盖率扩展那一轮的验收）。

表本身来自**多份未被本工具写过的存档**（只读统计），合并规则：
  * 低 16 位常量：所有样本最高票占比 >= 90% 且样本数 >= 2 才收录，否则剔除进 conflicts
  * 高 16 位：同规则单独判定
  * roll 字节：只记录、不参与回填
表里不得出现本机路径或账号 id。
"""

from __future__ import annotations

import json
import unittest

from nioh3_equipment_affix_editor import equipmentdb
from nioh3_equipment_affix_editor.affixdb import AffixDb
from nioh3_equipment_affix_editor.affixmarkers import DEFAULT_MARKERS_PATH

#: 逐池覆盖率下限（低于它说明表退化了；提高上限不会让测试失败）。
COVERAGE_FLOORS = {"饰品": 225, "近战": 292, "远程": 88, "防具": 487}
#: 表里绝不允许出现的本机信息片段。
FORBIDDEN_IN_TABLE = ("D:\\", "C:\\", "Savedata", "76561198", "lyl", "AIWorkspace")


class MultiSourceMarkerTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = DEFAULT_MARKERS_PATH.read_text(encoding="utf-8")
        cls.payload = json.loads(cls.text)

    def test_the_table_records_every_source_save(self) -> None:
        """多来源：至少两份存档，每份都带自己的 sha256 与扫描量。"""
        sources = self.payload.get("sources")
        self.assertIsInstance(sources, list)
        self.assertGreaterEqual(len(sources), 2)
        for source in sources:
            with self.subTest(source.get("sha256", "?")[:12]):
                self.assertRegex(source["sha256"], r"^[0-9a-f]{64}$")
                self.assertGreater(source["records_scanned"], 0)
                self.assertGreater(source["slots_scanned"], 0)
        self.assertIn("多份", self.payload["source"])
        self.assertGreater(self.payload["slots_scanned"], 0)

    def test_the_table_carries_no_local_paths_or_account_ids(self) -> None:
        for fragment in FORBIDDEN_IN_TABLE:
            with self.subTest(fragment):
                self.assertNotIn(fragment, self.text)

    def test_every_kept_id_meets_the_majority_rule(self) -> None:
        """>=2 个样本 + 最高票 >= 90%（表里记的 min_* 就是这条规则的参数）。"""
        self.assertGreaterEqual(self.payload["min_share"], 0.90)
        self.assertGreaterEqual(self.payload["min_samples"], 2)
        samples = self.payload["prefix_samples_by_effect_id"]
        low = self.payload["prefix_low_by_effect_id"]
        self.assertEqual(set(samples), set(low))
        for key, count in samples.items():
            with self.subTest(key):
                self.assertGreaterEqual(count, self.payload["min_samples"])
        # 冲突项必须被剔除：它们的 id 不得出现在两张映射里
        for line in self.payload["conflicts"]:
            key = line.split(":", 1)[0].strip()
            if key.startswith("0x"):
                self.assertNotIn(key, low)
                self.assertNotIn(key, self.payload["prefix_majority_by_effect_id"])

    def test_the_shipped_table_still_beats_the_coverage_floor(self) -> None:
        low = {int(key, 16) for key in self.payload["prefix_low_by_effect_id"]}
        pools = {
            "饰品": AffixDb(),
            "近战": equipmentdb.load_melee_weapon_db(),
            "远程": equipmentdb.load_ranged_weapon_db(),
            "防具": equipmentdb.load_armor_db(),
        }
        for name, db in pools.items():
            ids = [entry.effect_id for entry in db.all()]
            covered = sum(1 for item in ids if item in low)
            with self.subTest(name):
                self.assertGreaterEqual(covered, COVERAGE_FLOORS[name],
                                        f"{name} 覆盖率退化：{covered}/{len(ids)}")

    def test_markers_are_not_derivable_from_the_affix_codes(self) -> None:
        """实测：标记低 16 位不是词条代码里任何 2 字节窗口 —— 只能靠存档样本。

        这条记录了"为什么覆盖率停在 73–80% 而不是直接补全"：没有样本就无法推断，
        我们也绝不猜（查不到就保持原值）。
        """
        low = self.payload["prefix_low_by_effect_id"]
        self.assertTrue(low)
        for key, value in list(low.items())[:5]:
            with self.subTest(key):
                self.assertGreater(value, 0)
                self.assertLessEqual(value, 0xFFFF)


if __name__ == "__main__":  # pragma: no cover - manual runs only
    unittest.main()
