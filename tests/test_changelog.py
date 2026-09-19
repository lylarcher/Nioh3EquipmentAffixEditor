"""Tests that keep CHANGELOG.md honest.

The changelog is documentation, so it can only be tested structurally -- but
the two failure modes that actually matter are cheap to catch:

* the version in ``version.py`` was bumped without a changelog entry, and
* the file drifted into a format that no longer parses as "Keep a Changelog"
  (which would silently break the version/date extraction used below).
"""

from __future__ import annotations

import datetime
import re
import unittest
from pathlib import Path

from nioh3_accessory_editor.version import __version__
from tests import support

CHANGELOG = support.PROJECT_ROOT / "CHANGELOG.md"
README = support.PROJECT_ROOT / "README.md"

RELEASE_HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})$", re.M)
UNRELEASED_HEADING = re.compile(r"^## \[未发布\]", re.M)
SUBSECTION = re.compile(r"^### \S", re.M)
README_ANCHOR = re.compile(r"^#+ +Version information", re.M | re.I)


class ChangelogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not CHANGELOG.is_file():
            raise unittest.SkipTest("CHANGELOG.md is missing")
        cls.text = CHANGELOG.read_text(encoding="utf-8")

    def test_has_a_title_and_the_required_disclaimer(self) -> None:
        self.assertTrue(self.text.startswith("# "), "必须以一级标题开头")
        self.assertIn("仅供测试学习用，不要用于联机影响游戏平衡", self.text)

    def test_declares_its_format(self) -> None:
        self.assertIn("Keep a Changelog", self.text)
        self.assertIn("语义化版本", self.text)

    def test_has_an_unreleased_section(self) -> None:
        self.assertRegex(self.text, UNRELEASED_HEADING)

    def test_documents_the_current_version(self) -> None:
        versions = [match.group(1) for match in RELEASE_HEADING.finditer(self.text)]
        self.assertIn(
            __version__, versions,
            f"version.py 是 {__version__}，但 CHANGELOG.md 没有对应小节",
        )

    def test_versions_are_unique_and_newest_first(self) -> None:
        released = [(match.group(1), match.group(2))
                    for match in RELEASE_HEADING.finditer(self.text)]
        names = [name for name, _ in released]
        self.assertEqual(len(names), len(set(names)), f"重复的版本小节: {names}")

        def key(item: tuple[str, str]) -> tuple[int, ...]:
            return tuple(int(part) for part in item[0].split("."))

        self.assertEqual(released, sorted(released, key=key, reverse=True),
                         "版本小节必须从新到旧排列")

    def test_release_dates_are_valid_and_not_in_the_future(self) -> None:
        today = datetime.date.today()
        for match in RELEASE_HEADING.finditer(self.text):
            name, raw_date = match.group(1), match.group(2)
            try:
                date = datetime.date.fromisoformat(raw_date)
            except ValueError as error:  # pragma: no cover - defensive
                self.fail(f"版本 {name} 的日期无法解析: {raw_date} ({error})")
            self.assertLessEqual(date, today, f"版本 {name} 的日期在未来: {raw_date}")

    def test_current_version_lists_changes(self) -> None:
        start = self.text.index(f"## [{__version__}]")
        end = self.text.find("\n## [", start + 1)
        body = self.text[start:end if end != -1 else len(self.text)]
        self.assertGreaterEqual(len(SUBSECTION.findall(body)), 2,
                                "当前版本至少要有两个变更分类小节")

    def test_documents_the_unverified_boundary(self) -> None:
        """The honest-limits note must survive edits to the changelog."""
        self.assertIn("尚未用实机存档核对", self.text)

    def test_links_to_an_anchor_that_exists(self) -> None:
        if "README.md#version-information" not in self.text:
            self.skipTest("changelog does not link to the README section")
        readme = README.read_text(encoding="utf-8")
        self.assertRegex(readme, README_ANCHOR,
                         "README 缺少 'Version information' 标题，链接锚点会失效")

    def test_formatting_hygiene(self) -> None:
        self.assertTrue(self.text.endswith("\n"), "文件必须以换行结尾")
        for number, line in enumerate(self.text.splitlines(), start=1):
            self.assertFalse(line.rstrip() != line,
                             f"第 {number} 行有行尾空白")
            self.assertNotIn("\t", line, f"第 {number} 行包含制表符")
        self.assertNotIn("\r", self.text, "请使用 LF 换行")
        self.assertNotIn("\ufeff", self.text, "文件不应包含 BOM")


if __name__ == "__main__":
    unittest.main()
