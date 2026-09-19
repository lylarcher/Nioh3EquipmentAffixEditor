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

### 新增

* **写入前置条件提示（退出游戏 / 标题界面）**：存档编辑最终落在文件上，因此游戏不能
  正在内存里持有该存档。现在写入前会明确提示「请先完全退出游戏，或退回到游戏标题
  界面」并说明游戏内保存会覆盖本次修改：
  * CLI `edit`：写入前打印完整要求 + 当前门禁状态（是否检测到游戏进程、是否演练模式），
    成功后再提示「加载前请勿在游戏内保存」；
  * GUI：新增常驻红色提示行（始终可见，不只在弹窗里），写入确认对话框内嵌完整要求；
    检测到游戏运行时额外说明"只有标题界面写入才安全"；
  * 文案集中在 `savefile.SAVE_WRITE_REQUIREMENT`，CLI/GUI/拒绝信息三处保持同一口径。
* **原始数据入库**：`third_party/source-data/` 收纳《仁王3词条装备库v2.21.xlsx》与
  `Nioh3 v2.21.CT`（含来源与授权说明），词条库可离线重新生成，工程不再依赖外部目录。
* **单文件 exe（发行形态）**：发行包只包含 `Nioh3AccessoryEditor.exe` 一个文件，不再有
  任何 `.py`、目录或安装步骤。
  * 首次运行把随附文件解压到 **exe 同目录**：`config/editor.json`（参数配置）、
    `data/accessory_affixes.json`（词条库）、`bin/Nioh_Savefile_decrypt.exe`
    （随附加解密组件）、`third_party/source-data/`（原始 xlsx/CT）、`README.md`、
    `CHANGELOG.md`，并写入 `.extracted-manifest.json` 记录清单。
  * 解压**不会覆盖用户改过的文件**：清单里记的是本程序写入的哈希，内容一致才刷新；
    删掉的文件会重新解压出来；目录只读时降级为「只从 exe 内部读取」而不报错。
  * 控制台子系统：从终端运行时保留控制台（CLI 可用），双击启动 GUI 时自行隐藏控制台
    （仅在独占该控制台时隐藏，不影响用户的终端）。
* **参数配置文件 `config/editor.json`**：存档目录、账号/栏位过滤、加密后端、备份目录、
  GUI 演练/校验默认值都可在文件里改；命令行参数始终优先。
  * 新增 `config` 子命令：`config`（查看有效配置）、`config --json`、
    `config --init [路径] [--force]`（生成带说明的默认文件）。
  * 新增全局参数 `--config <路径>`，可指定其它配置文件。
  * 文件校验严格：`schema` 不匹配、未知键（如把 `default_dry_run` 写成 `dry_run`）、
    类型错误都会在启动时报错，不会静默忽略；`_` 开头的键视为注释。
    相对路径相对 exe 目录解析，便于整个文件夹搬迁。
* **`build.ps1` 新增 `-TestPattern`**：只跑匹配的测试模块（如 `-TestPattern test_cli.py`），
  便于单模块迭代。

### 变更

* `tools/build_affix_db.py`：默认来源改为仓库内的 `third_party/source-data/`
  副本（保留旧路径作为回退），新增 `resolve_source()` 与找不到文件时的路径提示。
* 游戏进程门禁统一为一处实现：`editor.commit_save` 改为调用
  `savefile.require_game_not_running()`，不再维护第二份判断与文案。
* `build.ps1`：发行流程改为「生成构建信息 → 跑测试 → 生成内嵌载荷
  （`tools/make_payload.py`）→ PyInstaller 单文件打包 → 在全新目录冒烟测试 exe
  （校验自解压产物与冻结的构建信息）→ 校验发行物无 `.py` → 打包 zip」，不再复制源码目录。
  * PyInstaller 只作为构建期依赖，自动装入 `.build-venv`（可用
    `-PyInstallerPython` 指定解释器；镜像源可用 `NIOH3_PIP_INDEX` 覆盖）。
  * **修复了一处会让失败构建看起来成功的缺陷**：Windows PowerShell 5.1 下
    `& exe 2>&1 | ForEach-Object` 这种合并输出写法可能返回过期的 `$LASTEXITCODE`（子进程
    实际退出码非 0，PowerShell 却报告 0），导致单元测试失败时构建仍继续出包。现在改为
    `Start-Process -Wait -PassThru` 读取真实退出码，并在构建开始时用
    `tools/check_build_gate.py` **自检**「失败的子步骤一定会被中止」，自检不过直接中止构建。
* `nioh3_accessory_editor/paths.py`（新增）：统一「源码布局」与「冻结布局」的路径解析，
  资源一律以 exe 同目录为准。
* `nioh3_accessory_editor/bootstrap.py`（新增）：随附文件的自解压与清单管理。
* `nioh3_accessory_editor/config.py`（新增）：参数配置的读取、校验与默认值生成。
* `savefile.default_crypto_tool()` / `savefile.discover_save_paths()` /
  `editor.discover_saves()`：改为按冻结布局与配置解析路径（`discover_save_paths(root=...)`、
  `discover_saves(root=...)` 可按配置指定存档根目录）。
* `version.py`：冻结运行时不再尝试调用 `git`，构建身份只取自 `_buildinfo`；版本信息额外
  显示程序目录与配置文件路径。
* GUI 的备份目录、演练/校验默认值改由配置决定（源码运行时仍为当前目录）。

### 移除

* 删除同工作区的 `Nioh3Trainer\` 工程（C++ 内存修改器框架，偏移全为占位符、从未生效，
  且不是 git 仓库）：其功能已被本工程取代。删除前已把其中两个不可再生的原始数据文件
  迁入 `third_party/source-data/`，并核对迁移后词条库重新生成结果与已提交内容逐字节一致。

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
