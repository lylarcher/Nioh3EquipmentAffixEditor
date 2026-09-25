# third_party / source-data

这里保存本工程赖以工作的**原始数据文件**。它们不是代码，也不会在运行时被读取：
`data/accessory_affixes.json` 是从 `仁王3词条装备库v2.21.xlsx` 抽取后生成的词条库，
已随仓库提交。保留原始文件是为了让词条库**可重新生成、可核对**。

| 文件 | 用途 | 来源 |
|---|---|---|
| `仁王3词条装备库v2.21.xlsx` | 饰品词条的唯一合法来源（`tools/build_affix_db.py` 的输入） | 神烦～（3DM）/ -神-烦-（bilibili） |
| `Nioh3 v2.21.CT` | 装备结构参考（记录头、词条槽字段布局的来源） | Cheat Engine 表格，v2.21 |

## 重新生成词条库

```powershell
python tools/build_affix_db.py            # 默认读取本目录下的 xlsx
python tools/build_affix_db.py --dry-run   # 只解析并报告，不写文件
```

## 授权与使用

* 词条数据版权归原作者所有，**非商业使用需署名**；请勿二次商用发布。
* 这些原始数据**不属于本项目许可证（PolyForm Noncommercial License 1.0.0）的范围**，
  详见上级目录的 `third_party/README.md`。
* 本目录仅用于本地测试学习；仁王 3 为单机 / 纯 PVE 联机游戏，请勿用于影响游戏平衡的用途。
* 详细致谢见仓库根目录 `README.md` 的 Credits 一节。

## 历史说明

这两个文件原本放在同工作区的 `Nioh3Trainer\` 工程里。该工程是一个 C++ 内存修改器框架
（Win32 + GDI，50 项功能表），但其偏移量自述均为**占位符**、从未真正生效，功能已被本
工程（直接编辑存档、持久化生效）完全取代，因此已按计划删除。删除前把上述两个原始数据
文件迁入本目录，以保证词条库仍可重新生成、结构参考仍可查证。

同时丢弃的内容（如仍需可从本目录的 xlsx 重新得到，或本就无长期价值）：

* `_xlsx_dump\*.tsv`：xlsx 各工作表的导出结果，可用本工程的解析代码重新导出；
* `src\*.cpp/.h`、`CMakeLists.txt`、`build.bat`、`data\offsets.json`：占位偏移的内存
  修改器实现，已无用途；
* `docs\OffsetsFinding.md`：Cheat Engine 指针链定位教程，只适用于内存修改器路线；
  本工程走存档文件路线，不做内存写入，故不再保留。
