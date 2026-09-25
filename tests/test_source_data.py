"""Guards for the bundled source data in third_party/source-data/.

These files are opaque inputs, not build products: the affix catalog is derived
from the workbook, and the cheat table documents the record structure we rely
on.  Two real failure modes are worth pinning:

* a line-ending rule silently normalising the bytes (``*.CT`` was classified as
  text by ``text=auto`` and lost 18,138 CRLF pairs in the index), and
* the default catalog source drifting back to a machine-specific path.
"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from tests import support

SOURCE_DATA = support.PROJECT_ROOT / "third_party" / "source-data"
GITATTRIBUTES = support.PROJECT_ROOT / ".gitattributes"

#: Recorded from the originals; the catalog in data/ was generated from these
#: exact bytes, so any change here invalidates that provenance.
EXPECTED = {
    "仁王3词条装备库v2.21.xlsx": (
        830_883,
        "31598f20f32ba8e4a6ebad69717d28b8dd48718f052a27465180cc2d0d6877f5",
    ),
    "Nioh3 v2.21.CT": (
        1_028_727,
        "74b946749a39bf779e8501e2a902fd71e5bc9144b4ec36a7091ea7f23539eac4",
    ),
}


class SourceDataTests(unittest.TestCase):
    def test_files_are_present_with_the_original_bytes(self) -> None:
        for name, (size, digest) in EXPECTED.items():
            path = SOURCE_DATA / name
            with self.subTest(name=name):
                self.assertTrue(path.is_file(), f"缺少原始数据文件: {path}")
                data = path.read_bytes()
                self.assertEqual(len(data), size, "文件大小与原始数据不一致")
                self.assertEqual(hashlib.sha256(data).hexdigest(), digest,
                                 "文件内容与原始数据不一致（可能被行尾转换破坏）")

    def test_readme_documents_provenance(self) -> None:
        text = (SOURCE_DATA / "README.md").read_text(encoding="utf-8")
        for needle in ("神烦", "Cheat Engine", "非商业", "build_affix_db.py"):
            self.assertIn(needle, text)

    def test_gitattributes_marks_them_binary(self) -> None:
        """`*.CT` has no NUL byte early on, so `text=auto` would mangle it."""
        text = GITATTRIBUTES.read_text(encoding="utf-8")
        for pattern in ("*.CT binary", "*.xlsx binary", "*.exe binary"):
            self.assertIn(pattern, text, f".gitattributes 缺少二进制规则: {pattern}")


class CatalogSourceTests(unittest.TestCase):
    def test_default_source_is_inside_the_repository(self) -> None:
        from tools import build_affix_db

        source = build_affix_db.DEFAULT_SOURCE
        self.assertTrue(
            str(source).startswith(str(support.PROJECT_ROOT)),
            f"默认词条来源必须是仓库内文件，实际是 {source}",
        )
        self.assertTrue(source.is_file(), f"默认词条来源不存在: {source}")

    def test_resolve_source_prefers_the_bundled_copy(self) -> None:
        from tools import build_affix_db

        self.assertEqual(build_affix_db.resolve_source(), build_affix_db.DEFAULT_SOURCE)
        explicit = Path("X:/somewhere/else.xlsx")
        self.assertEqual(build_affix_db.resolve_source(explicit), explicit)

    def test_no_machine_specific_paths_are_hardcoded(self) -> None:
        """源码里不得出现任何本机绝对路径（公开仓库尤其重要）。"""
        from tools import build_affix_db

        self.assertFalse(
            hasattr(build_affix_db, "LEGACY_SOURCES"),
            "旧的 LEGACY_SOURCES 本机回退路径应当已经删除",
        )
        text = (support.PROJECT_ROOT / "tools" / "build_affix_db.py").read_text(
            encoding="utf-8")
        for marker in ("D:\\", "C:\\", "/Users/", "/home/"):
            self.assertNotIn(marker, text, f"源码里出现本机路径片段: {marker}")


if __name__ == "__main__":
    unittest.main()
