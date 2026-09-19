"""Guard against the class of damage that once garbled this project's README.

An earlier PowerShell write decoded UTF-8 bytes as GBK and saved the result: the
Chinese text turned into plausible-looking gibberish, and bytes that could not be
re-encoded became ``?`` or private-use characters.  It stayed unnoticed for many
commits, so the signatures are now checked automatically.

The detector is measured, not guessed.  Run :func:`damage_signatures` over the
README as committed *before* the repair (``git show 174249d:README.md``) and it
reports 32 sites; run it over the repaired file and every other text file here and
it reports 0.

Its three signatures:

1. a private-use character (``U+E000``-``U+F8FF``) -- never legitimate;
2. a CJK character directly against an ASCII ``?`` -- a dropped byte (Chinese
   prose uses the full-width ``？``);
3. a run of CJK characters that re-encodes to GBK and decodes back as valid
   UTF-8 *and* yields plausible Chinese.  The plausibility filter is what keeps
   correct text out of the results: ``位`` alone "recovers" to ``λ`` and
   ``同目录`` to ``ͬĿ¼``, neither of which contains a common character.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests import support

ROOT = support.PROJECT_ROOT

TEXT_SUFFIXES = {".py", ".md", ".json", ".txt", ".ps1", ".spec", ".cfg", ".toml"}
TEXT_NAMES = {".gitattributes", ".gitignore"}
SKIP_DIRECTORIES = {".git", ".build-venv", "build", "dist", "__pycache__",
                    ".pytest_cache"}

#: This file holds deliberately damaged samples, so it cannot scan itself.
SELF = "tests/test_text_encoding.py"

PUA = re.compile(r"[\ue000-\uf8ff]")
CJK_CLASS = "\u3000-\u303f\u4e00-\u9fff\uff00-\uffef"
LOST_BYTE = re.compile(rf"[{CJK_CLASS}]\?|\?[{CJK_CLASS}]")
RUN = re.compile(r"[^\x00-\x7f]+")
PURE_CJK = re.compile(rf"^[{CJK_CLASS}]+$")

#: The ~1000 most frequent Chinese characters (plus the ones that appear in this
#: project's own prose), used only to decide "does this look like Chinese?".
COMMON = set(
    "的一是不了在人有我他这个们中来上大为和国地到以说时要就出会可也你对生能而子那得于着下"
    "自之年过发后作里用道行所然家种事成方多经么去法学如都同现当没动面起看定天分还进好小"
    "部其些主样理心她本前开但因只从想实日军者意无力它与长把机十民第公此已工使情明性知全"
    "三又关点正业外将两高间由问很最重并物手应战向头文体政美相见被利什二等产或新己制身果"
    "加西斯月话合回特代内信表化老给世位次度门任常先海通教儿原东声提立及比员解水名真论处"
    "走义各入几口认条平系气题活尔更别打女变四神总何电数安少报才结反受目太量再感建务做接"
    "必场件计管期市直德资命山金指克许统区保至队形社便空决治展马科司五基眼书非则听白却界"
    "达光放强即像难且权思王象完设式色路记南品住告类求据程北边死张该交规万取拉格望觉术领"
    "共确传师观清今切院让识候带导争运笑飞风步改收根干造言联持组每济车亲极林服快办议往元"
    "英士证近失转夫令准布始怎呢存未远叫台单影具罗字爱击流备兵连调深商算质团集百需价花党"
    "华城石级整府离况亚请技际约示复病息究线似官火断精满支视消越器容照须九增研写称企八功"
    "吗包片史委乎查轻易早曾除农找装广显吧阿李标谈吃图念六引历首医局突专费号尽另周较注语"
    "仅考落青随选列武红响虽推势参希古众构房半节土投某案黑维革划敌致陈律足态护七兴派孩验"
    "责营星够章音跟志底站严巴例防族供效续施留讲型料终答紧黄绝奇察母京段依批群项故按河米"
    "围江织害斗双境客纪采举杀攻父苏密低朝友诉止细愿千值仍男钱破网热助倒育属坐帝限船脸职"
    "速刻乐否刚威毛状率甚独球般普怕弹校苦创假久错承印晚兰试股拿脑预谁益阳若哪微尼继送急"
    "血惊伤素药适波夜省初喜卫源食险待述陆习置居劳财环排福纳欢雷警获模充负云停木游龙树疑"
    "层冷洲冲射略范竟句室异激汉村哈策演简卡罪判担州静退既衣您宗积余痛检差富灵协角占配征"
    "修皮挥胜降阶审沉坚善妈刘读啊超免压银买皇养伊怀执副乱抗犯追帮宣佛岁航优怪香著田铁控"
    "税左换藏缓冲板欢哄宠猴烘涓搗紙儲宸浜"
)

#: Samples copied from the damaged revision, kept so the guard proves it works.
DAMAGED_SAMPLES = (
    "> **浠呬緵娴嬭瘯瀛︿範鐢紝涓嶈鐢ㄤ簬鑱旀満褰卞搷娓告垙骞宠　銆?*",
    "| `鏉ユ簮` / `鍔犲瘑缁勪欢` | where the build came from |",
    "* The motif is a 鍕剧帀 (magatama): a fat head tapering to a point",
    "  (`CD1F3135鈥?84B` / `1BDFDD57鈥?925`).",
    "  defaults to 浠呮紨缁?(dry run).",
)
CLEAN_SAMPLES = (
    "> **仅供测试学习用，不要用于联机影响游戏平衡。**",
    "| `来源` / `加密组件` | where the build came from |",
    "* 校验和语义误导：`commit_save` 实际是对「入参缓冲」的校验",
    "  `metadata` field (which is where the 固定/星 flag bits are expected to live)",
    "  状态栏显示**未发现存档 · 查找位置 …**（中文标点不算丢字节）",
)


def _plausible(recovered: str, run: str) -> bool:
    """Does this round trip look like recovered Chinese rather than noise?

    Two independent tests, because either alone misses real damage:
    a 3+ character run that recovers to *pure CJK* (``鍕剧帀`` -> ``勾玉``), or any
    2+ character run that recovers to common Chinese (``璇█`` -> ``语言``).
    Noise recovers to symbols or Latin letters (``位`` -> ``λ``,
    ``同目录`` -> ``ͬĿ¼``), so neither test fires on correct text.
    """
    if len(run) >= 3 and PURE_CJK.match(recovered):
        return True
    return len(run) >= 2 and any(char in COMMON for char in recovered)


def damage_signatures(line: str) -> list[str]:
    """Return one description per damage signature found in ``line``."""
    found: list[str] = []
    if PUA.search(line):
        found.append("private-use character")
    if LOST_BYTE.search(line):
        found.append("dropped byte next to CJK")
    for match in RUN.finditer(line):
        run = match.group(0)
        if len(run) < 2:
            continue
        try:
            recovered = run.encode("gbk").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if recovered != run and _plausible(recovered, run):
            found.append(f"GBK mojibake: {run} -> {recovered}")
    return found


def text_files() -> list[Path]:
    found = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if any(part in SKIP_DIRECTORIES for part in relative.parts):
            continue
        if str(relative).replace("\\", "/") == SELF:
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_NAMES:
            found.append(path)
    return sorted(found)


class EncodingDamageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.files = text_files()
        if not cls.files:
            raise unittest.SkipTest("no text files found")

    def _scan(self) -> list[str]:
        hits: list[str] = []
        for path in self.files:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                hits.append(f"{path.relative_to(ROOT)}: 不是合法 UTF-8")
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                for signature in damage_signatures(line):
                    hits.append(f"{path.relative_to(ROOT)}:{number}: {signature}"
                                f" | {line.strip()[:70]}")
        return hits

    def test_no_text_file_shows_encoding_damage(self) -> None:
        self.assertEqual(self._scan(), [],
                         "写入时把 UTF-8 当成 GBK 会造成这类乱码与丢字节")

    def test_the_guard_detects_the_historical_damage(self) -> None:
        """A guard that cannot fail is worthless: check it against real samples."""
        for sample in DAMAGED_SAMPLES:
            with self.subTest(sample=sample[:30]):
                self.assertTrue(damage_signatures(sample),
                                f"漏报了真实的乱码样本: {sample}")

    def test_the_guard_accepts_correct_chinese(self) -> None:
        for sample in CLEAN_SAMPLES:
            with self.subTest(sample=sample[:30]):
                self.assertEqual(damage_signatures(sample), [],
                                 f"误报了正常中文: {sample}")

    def test_documents_use_lf_without_bom(self) -> None:
        for name in ("README.md", "CHANGELOG.md"):
            data = (ROOT / name).read_bytes()
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"), f"{name} 不应有 BOM")
            self.assertNotIn(b"\r\n", data, f"{name} 应使用 LF 换行")
            self.assertTrue(data.endswith(b"\n"), f"{name} 应以换行结尾")

    def test_the_scan_actually_covers_the_documents(self) -> None:
        """A guard that silently scans nothing is worse than no guard."""
        names = {path.name for path in self.files}
        self.assertIn("README.md", names)
        self.assertIn("CHANGELOG.md", names)
        self.assertIn("savefile.py", names)
        self.assertGreater(len(self.files), 30)


if __name__ == "__main__":
    unittest.main()
