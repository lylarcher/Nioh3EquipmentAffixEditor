# 更新日志 / Changelog

本文件记录 Nioh3AccessoryEditor 的所有重要变更。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

> **仅供测试学习用，不要用于联机影响游戏平衡。**

本仓库目前只在本地提交、尚未配置远端，因此无法使用 `compare`/`releases` 链接，
条目改为标注提交号前缀（`git log --oneline` 显示的 7 位）。注意 `build.ps1` 打印的
`commit` 是**提交号后 8 位**（本项目约定，见 [README 的 Version information](README.md#version-information)），
两者指同一个提交。

每个版本对应的构建身份（commit 后 8 位 / 来源 / 构建时间 / 语言）由
`build.ps1` 在构建时生成并写入 `BUILD-INFO.txt`、`BUILD-INFO.json` 与
`nioh3_accessory_editor/_buildinfo.py`；这些文件属于构建产物、不入库。

---

## [未发布]

暂无。

## [0.1.0] - 2026-09-19

首个版本：仁王 3 PC 存档饰品词条修改器，编辑结果直接写入存档文件（非仅内存），
纯标准库 Python + Tkinter。

### 新增

* **存档读写管线**（`nioh3_accessory_editor/`）
  * `nioh_aes.py`：定制 AES 的表驱动实现（自实现 S 盒/RS 盒、字序反转的初始轮密钥、
    列旋转式 ShiftRows）。
  * `crypto.py`：参考实现的计数器异或流，双密钥对（header 用固定根密钥，body 取自
    明文头 `0x49/0x59/0x69/0x79`）；覆盖区间 `[0, 0x158 + 0x900050)`，尾部 8 字节
    在所有变换中原样保留。
  * `checksum.py`：body `[0x190, 0x900190)` 按 `0x400` 分块的 `<q` 求和与 seed 折叠，
    并提供只读校验 `verify_user_checksum`。
  * `records.py`：记录区 `0x176CCE`、400 槽 × `0xE8`、每记录 7 个 `0x18` 词条槽；
    编辑字典严格校验（未知字段、非整数、越界、重复槽位一律拒绝）。
  * `savefile.py`：存档发现、双加解密后端（内置参考 exe / 纯 Python）、静默期双读
    指纹、游戏进程门禁、明文备份 + manifest、写入前后解密校验与失败回滚、
    `MoveFileExW` 原子落盘。
* **编辑器三层**（`editor.py` / `cli.py` / `ui.py`）
  * CLI 子命令 `list` / `check` / `edit` / `backup`，全部失败关闭（fail closed）。
  * Tkinter GUI：常驻免责声明、默认「仅演练」、词条下拉选择、应用修改后再写入存档。
* **词条库**：`tools/build_affix_db.py` 从《仁王3词条装备库v2.21.xlsx》饰品词条表
  生成 `data/accessory_affixes.json`（276 条合法词条，schema
  `nioh3-accessory-affixes/v1`）；表外词条一律拒绝。
* **测试**：`tests/` 282 个用例，`python tools/run_tests.py`（可选
  `--pure-crypto` 跑慢速全文件纯 Python 路径）。
* **重量级交叉校验**：`tools/crosscheck_crypto_vs_exe.py` 对 9,437,608 字节的
  加密覆盖区做纯 Python 与参考 exe 的逐字节比对。
* **构建与版本信息**：`build.ps1` + `tools/make_build_info.py`，版本信息输出
  commit 后 8 位、来源（项目根 + 加密 exe 路径及其 SHA-256）、构建时间、编程语言；
  同时显示在 CLI（`--version` / `version` / `--json`）、GUI 底栏与「版本信息」按钮、
  `BUILD-INFO.txt|json` 与构建日志中。

### 修复

* **header 末 8 字节未被加密**：异或长度误用整除（`length // 16`）而非向上取整，
  导致 `[0x150,0x158)` 保持明文；由全量 exe 交叉校验捕获（`0x150` 起 8 字节不一致），
  改为向上取整并按精确长度异或。
* **GUI 崩溃**：`_populate_accessories` 先把 `selected_accessory` 置空、随后又用它设置
  树选中项，触发 `TclError: Item None not found`；现保存目标索引并在重建树后显式恢复。
* **GUI 每次应用都重写全部 7 个槽位**：改为与当前记录比对，只提交真正改动的槽位；
  重选同一条词条、清空已空的槽位都不再产生写入。
* **GUI 臆测 metadata**：原先会把词条库的 固定/星 标志位合并进 `metadata`，但该位在
  存档中的编码尚未核实（且 `星 = 0x04` 与类别位重叠）；现 GUI 只写有据可依的
  `effect_id` + 标称 `value`，`metadata` 保持原样，CLI 可显式指定。
* **`--edit` 指向不存在的记录会被静默忽略**：现在显式报错；同时修正
  `plan_edits` 会伪造 `item_count=1` 的行为（新增 `records.read_item_record`）。
* **校验和语义误导**：`commit_save` 的 `checksum_was_consistent` 实际是对「入参缓冲」
  的度量（编辑后必然为 False），已改名并明确语义。
* **游戏进程检测重复**：大小写变体导致同一进程名出现两次，已去重。
* **词条库重复 ID**：生成器改为按 `effect_id` 去重（严格模式），同 ID 不同
  数值/标志的行记入 `conflicts` 而不是静默取其一。
* **CLI 退出码/路由**：`version` 子命令未登记到 CLI 路由集合导致误入 GUI；已修复并
  新增「CLI 解析器的所有子命令都必须可路由」的测试。
* **`--version` 输出被重排**：argparse 内置 version action 会把多行横幅交给 help
  formatter 折成一段，改为自定义 action 原样打印。

### 安全 / 可靠性

* 写入前检查存档静默期（存档 + `BACKUP.BIN` + 账号系统存档双读指纹），检测到游戏
  正在保存则中止（`SAVE_SYNC_ACTIVE`）；指纹计算会重新 `stat` 以发现竞态。
* 检测到 `Nioh3.exe` / `Nioh3-Win64-Shipping.exe` 运行时拒绝写入，除非显式
  `--force-while-running`。
* 每次写入前生成明文备份 + manifest（含主存档与明文的 SHA-256），备份文件名带
  时间戳与随机后缀，避免并发覆盖。
* 写入流程：暂存 → 解密校验 → 重新校验指纹 → 原子替换 → 再次解密校验 → 失败回滚。
* 记录解析失败即失败关闭（header 不匹配不猜测）；GUI 常驻免责声明并默认演练模式。

### 说明（已知边界）

* **真实存档中的饰品词条槽布局尚未用实机存档核对**：偏移来自参考工程的卷轴布局与
  作弊表的装备结构，且记录区包含全部装备族，`list` 会列出所有匹配捕获头的记录。
  请先在自己的存档上运行 `list` / `check` 核对，并保留自动备份。
* 参考实现的「解密」不是其「加密」的逆：其生效的 `rsbox` 与 `sbox` 完全相同，且该
  自定义 S 盒不是置换（164 个不同值、69 个重复值、92 个值不可达），信息在 SubBytes
  阶段即不可恢复。因此本工程按参考的实际行为，用同一 ECB keystream 异或实现解密；
  `NiohAes.decrypt_block` 仅用于忠实复刻 `aes.c`，管线中从不调用。

---

## 版本号与发布流程

1. 修改 `nioh3_accessory_editor/version.py` 里的 `__version__`。
2. 在本文件顶部新增对应版本小节（`## [x.y.z] - YYYY-MM-DD`），把「未发布」内容
   移入该小节。
3. 运行 `powershell -File .\build.ps1`：跑测试 → 生成构建身份 → 组装并冒烟测试
   `dist/` → 打包 zip。缺少当前版本小节时 `tests/test_changelog.py` 会失败。
4. 提交（本仓库只做本地提交，不打 tag/不发远端）。
