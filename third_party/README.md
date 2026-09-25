# third_party —— 第三方内容（不属于本项目许可证范围）

本目录与 `bin/` 下的第三方内容**版权归各自原作者所有**，本项目采用的
[PolyForm Noncommercial License 1.0.0](../LICENSE) **不覆盖它们**；
它们各自遵循原作者给出的条款，使用者需自行确认可否再分发。

| 文件 | 用途 | 大小 | 归属 |
|---|---|---|---|
| `source-data/仁王3词条装备库v2.21.xlsx` | 词条数据的唯一合法来源（`tools/build_affix_db.py` 的输入） | 约 811 KB | 神烦～（3DM）/ -神-烦-（bilibili），版权归原作者 |
| `source-data/Nioh3 v2.21.CT` | 装备结构参考（记录头、词条槽字段布局） | 约 1005 KB | Cheat Engine 表格，版权归原作者 |
| `../bin/Nioh_Savefile_decrypt.exe` | 存档解密组件 | 约 52 KB | 原作者，版权归原作者 |

## 说明

* 这些文件**只在本地生成/核对数据时使用**；运行时只读取由它们生成的
  `data/*.json`，不会执行它们。
* 词条库可以重跑生成：见 `source-data/README.md`。
* 本工具与游戏厂商（光荣特库摩 / Koei Tecmo）无关，未获其授权或背书；请支持正版。
* **仅供测试学习用，不要用于联机影响游戏平衡。**
