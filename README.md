# Nioh3AccessoryEditor

A Python accessory (饰品) affix editor for **Nioh 3 (PC)** that writes edits
**directly into the save file** so they persist across sessions — not a
memory-only trainer.

> **仅供测试学习用，不要用于联机影响游戏平衡。**
> Nioh 3's co-op is PvE-only, so this tool cannot affect other players. It is
> still provided strictly for learning/testing. Back up your save before use;
> the author is not responsible for any damage.

The project intentionally mirrors the style and architecture of
Nioh3-Scroll-Generator (pure stdlib Python, fail-closed guards,
subprocess-isolated crypto, quiescence fingerprinting, atomic durable writes).

Version history and the release checklist live in [CHANGELOG.md](CHANGELOG.md).

## Features

* **Two tabs: 饰品 and 魂核** — the 魂核 (soul core) tab edits cores through their
  *own* tables, because a 魂核 has no 恩宠/套装 affix and its own affix pool:
  287 ids from the workbook's `绘卷-魂核词条` sheet (`种类 = 魂核`) plus 83 core ids
  from `物品总目录` (`大类 = 魂核`), shipped as `data/soul_affixes.json` and
  `data/soul_items.json`. A record is identified as a core only when **both**
  facts hold — its header id is a 魂核 row *and* a slot names a 魂核 affix — since
  exactly one id (`0xfb24`) is listed in both pools and the item table is what
  keeps 八尺琼勾玉[武士]-style accessories out. On the reference save 137
  candidates resolved to 132 cores; the other 5 (two `0xda62`, which is not in
  the 魂核 sheet, and three accessories carrying `0xfb24`) are listed as
  unidentified and refused rather than guessed. The same rules as accessories
  apply: 同名固定 affixes are never editable (each core carries two), 等级 caps at
  180, and 种类 swaps are 魂核 ↔ 魂核 with the target kind's fixed affix copied
  from a real sample.
* **Persistent edits** — decrypt the PC user save, patch accessory effect
  slots, recompute the user checksum, re-encrypt, verify, and atomically replace
  the save with a plaintext backup + manifest.
* **Reads only the save file** — the game never has to be running to list or
  edit accessories; the tool never touches game memory.
* **Locates the record table instead of assuming it** — both the record array's
  offset *and* its record stride are version-scoped (the captured v2.00.02/v2.01
  layout is `0x176CCE` with `0xE8` records; a real v2.21 save uses `0x270066`
  with `0xF0` records), so the tool derives both from *your* save's own record
  headers and reports what it found. `scan` prints that diagnosis (engine
  offsets, detected stride, record-type and rarity histograms, raw effect-slot
  bytes) so a save from a build the tool has not seen can be reported back
  instead of silently reading nothing.
* **Accessories identified by evidence, not by a type table** — in v2.21 the
  field at record+0x00 holds a per-item id instead of the captured category ids,
  so a record counts as an accessory when its effect slots name affixes from the
  shipped 饰品词条 catalog. One real save resolved to 213 accessories out of
  1456 item records (the rest are weapons/armour/绘卷, listed separately). The
  trailing slot of an accessory holds its 恩宠/套装组合 effect (confirmed in game),
  which the table does not list, so that one slot is labelled accordingly instead
  of as an unknown affix.
* **Shows which accessory each record is — and can swap it (同分类 only)** — that
  same per-item id is resolved against the 饰品 rows of the workbook's 物品总目录
  (shipped as `data/accessory_items.json`, **89 entries**; two of them carry
  save-measured provenance, see
  [the reference save](#the-reference-save) below), so `list` and the GUI name every
  record (`种类 0x3e3f 龙笛[武士]`, `0x4987 八尺琼勾玉[武士]`,
  `0xf5bb 凶王耳饰[忍者]`). Measured on the reference save: **213 of 213**
  accessories resolve. Changing 饰品种类 is supported **inside the same 中类**
  (武士饰品 ↔ 武士饰品, 忍者饰品 ↔ 忍者饰品) from the GUI's 种类 row or
  `edit --kind`, and the target kind's 同名固定 affix is copied **from a real
  sample of that kind already in the save being edited** — never invented. See
  [Changing 种类](#changing-种类-同分类互换).
* **Fixed affixes cannot be edited (fail closed)** — every kind has its own
  同名固定 affix (some have none). A slot counts as fixed when the catalog marks
  the id `(同名固定)` and/or the slot's metadata byte 9 has bit `0x40`; measured on
  the reference save the two agreed on all 795 catalogued slots with zero
  exceptions. Fixed slots are shown as `（固定词条，不可修改）`, refused by
  `plan_edits`, and refused as a *target* too (a normal slot may not be turned into
  a fixed affix). The only code that rewrites them is the 种类 swap, which copies
  the target kind's own entry.
* **等级 editing, capped at 180** — `edit --level 180` or the GUI's 等级 row writes
  only `+0x06`/`+0x08` and refuses anything above 180 (the reference project reads
  the effective level as `min(record +0x06, 180)` and the reference save tops
  out at exactly 180). Measured: a whole 9.4 MB save changes in 5 bytes (2 level
  bytes + 3 checksum bytes). Stored affix values do **not** scale with level (see
  the verified note on 数值 and 等级/`+值`), so this is a caution-flagged edit:
  the GUI asks for confirmation and says to check the result in game.
* **Live write-condition display** — the GUI footer shows whether a Nioh 3
  process is running (green = writable, red = the write would be refused), so the
  game-closed requirement is visible before you click 写入存档.
* **Legal-affix-only editing** — the affix catalog is built from the
  `仁王3词条装备库v2.21.xlsx` 饰品词条 sheet (→ 276 unique effect ids); any affix
  outside the table is rejected (fail closed).
* **Legal 恩宠 editing (恩宠 → 恩宠 only)** — an accessory's last slot can be
  changed to another `xxx的恩宠` (the 21 rows the workbook marks 恩宠 or 上位恩宠),
  from the GUI's 恩宠 row or `edit --grace`. Item-specific 套装/专属套装 effects
  (e.g. 怨恨盖世) and plain 词条 are refused, as are non-trailing slots and
  records that are not accessories: the gate is the game's own family byte
  (metadata byte 9 — `0x0C` for all 196 恩宠 slots and `0x4C` for all 16 套装
  slots of the reference save's 213 accessories). Only the slot's effect id and
  value change; the family/sub-kind bytes and the unexplained byte 11 are kept
  exactly as the game wrote them. Measured on a copy of a real save: a whole
  9.4 MB save changes in 5 bytes — the 2 id bytes and 3 checksum bytes.
* **Dual crypto backend** — the bundled reference executable
  (`bin/Nioh_Savefile_decrypt.exe`, ~0.4 s per pass) by default; a
  bit-exact pure-Python port of the custom Nioh AES as a zero-dependency
  fallback (`--python-crypto`, ~32 s per pass).
* **GUI (Tkinter) and CLI** — no third-party dependencies.
* **Stamped builds** — `build.ps1` bakes the commit tail, source path, build
  time and language into the app, so every copy can say exactly what it is. A
  build packages in about a minute; add `-Test` to gate it on the suite.
* **Verified write pipeline** — quiescence double-read, game-process gate,
  checksum recompute, plaintext backup + manifest, staged decryption check
  before install, post-write check with automatic rollback, and a durable
  `MoveFileExW` replace.
* **Backups you can restore** — every write first saves a timestamped plaintext
  copy plus a manifest; `restore` lists them and puts any one back (re-encrypted,
  with a safety copy of what it replaced). See
  [Backup and restore](#backup-and-restore).

## Requirements

* Windows 10/11, Python 3.10+ (stdlib only: `tkinter`, `ctypes`, `zipfile`).
* A real Nioh 3 PC save under
  `%LOCALAPPDATA%\KoeiTecmo\NIOH3\Savedata\<SteamID>\SAVEDATA??\SAVEDATA.BIN`.

## Quick start

> **Before writing: close the game, or park it at the title screen.** Edits are
> written into the save file, so the game must not have that save loaded (see
> [Safety model](#safety-model)). Every write prints this requirement; the GUI
> shows it permanently and repeats it in the confirmation dialog.

```powershell
# GUI
python launch_editor.py

# CLI: list saves and their accessory records
python launch_editor.py list

# CLI: diagnose the record table of a save (read-only; no game needed)
python launch_editor.py scan

# CLI: read-only integrity report (JSON)
python launch_editor.py check

# CLI: edit record #3's effect slot 1 to affix 0x0B32 (value 200), write back
python launch_editor.py edit --record 3 --edit 1:0x0B32:200

# CLI: dry run (validates everything, writes nothing)
python launch_editor.py edit --record 3 --edit 1:0x0B32:200 --dry-run

# CLI: replace record #3's 恩宠 with another one — by id, or by a unique name
python launch_editor.py edit --record 3 --grace 0x4fa3
python launch_editor.py edit --record 3 --grace 稻荷神 --dry-run
#   only 恩宠 → 恩宠 is allowed; 套装/专属套装 (e.g. 怨恨盖世) and plain 词条 are
#   refused with the reason, and nothing is written.

# CLI: change 等级 (1..180; only +0x06/+0x08 and the checksum change)
python launch_editor.py edit --record 3 --level 180 --dry-run

# CLI: swap 种类 inside the same 中类 (武士饰品 ↔ 武士饰品)
python launch_editor.py edit --record 3 --kind 八尺琼勾玉[武士] --dry-run
python launch_editor.py edit --record 3 --kind 0x4987 --dry-run
#   the target kind's 同名固定 affix is copied from a real sample in this save;
#   cross-category swaps, kinds with no sample, and ambiguous kinds are refused.

# CLI: plaintext backup only
python launch_editor.py backup

# CLI: list this save's backups, then restore the newest one (dry run first)
python launch_editor.py restore --list
python launch_editor.py restore --from latest --dry-run
python launch_editor.py restore --from latest

# CLI: version/build information (commit tail, source, build time, language)
python launch_editor.py --version
python launch_editor.py version --json
```

### If 读取饰品 (or `list`) finds no records

The game does **not** need to be running — reading only touches the save file.
An empty or near-empty result means the tool could not recognise the save's
record table, and *both* the array offset and the record stride are
version-scoped: the captured v2.00.02/v2.01 layout puts 400 records of `0xE8`
bytes at `0x176CCE`, while a real v2.21 save keeps 2000 records of `0xF0` bytes
at `0x270066`. Scanning such a save with the captured stride hits a real record
only every `0xE8 × 30 == 0xF0 × 29` bytes, which looks exactly like "no
accessories". The tool therefore derives both from the save's own headers and
reports what it found:

```powershell
python launch_editor.py scan          # human-readable diagnosis
python launch_editor.py scan --json   # same, machine-readable
```

The report states where (or whether) the record table was located, which stride
it uses, how strong the evidence is, the record-type and rarity histograms, the
level range and a few records' raw effect-slot bytes. Please send that output
back with your save version if the numbers do not match what the game shows.

When you have the save file itself (no in-game reference needed), the deep
read-only inspector answers the next two questions with statistics instead of
assumptions:

```powershell
# where is the table, which effect-slot base is real, which records are accessories?
python tools\inspect_save.py --config D:\path\to\editor.json --records 10
python tools\inspect_save.py --save "D:\...\SAVEDATA00\SAVEDATA.BIN" --json report.json
```

* **Effect-slot base sweep** — for every candidate base it counts how many of
  the u32 values at `base + k*0x18 + 4` are *known 饰品词条 ids* from the shipped
  catalog. Arbitrary bytes hit that catalog ~`276/2**32` of the time, so a real
  base stands out by orders of magnitude, and the captured `0x34` is confirmed
  rather than trusted. Bases a whole stride apart read the same lattice shifted
  (they miss the first or last real slot), so they are labelled
  `与 0x34 同格` instead of being silently ranked as independent evidence.
* **Accessory candidates** — records whose occupied slots *all* name catalog
  entries, listed with slot-by-slot `名称 / id / 数值 / 标识`; records holding
  unknown ids are reported separately.
* **Per-type summary** — count, level range, rarities, occupied-slot histogram
  and catalog hit rate for every record type in the array.

Everything is read-only: the save is decrypted in memory, no state directory is
created, and the report ends by saying so.

### Release build: one single-file executable

Download/copy `Nioh3AccessoryEditor.exe` anywhere and run it. There is **no
installer, no `dist/` folder and no `.py` file** in a release: everything the
program needs is inside that one file.

* **Double-click** it to open the GUI.
* Run it from a terminal for the CLI (the GUI hides the console it was started
  with, but a CLI run keeps it):

  ```powershell
  .\Nioh3AccessoryEditor.exe list
  .\Nioh3AccessoryEditor.exe check
  .\Nioh3AccessoryEditor.exe edit --record 3 --edit 1:0x0B32:200
  .\Nioh3AccessoryEditor.exe restore --list
  .\Nioh3AccessoryEditor.exe version
  ```

**First run unpacks the side-by-side files next to the exe** (this is by
design, so you can read and edit them):

| Path | What it is |
| --- | --- |
| `config/editor.json` | parameter file: save root, account/slot filter, crypto backend, backup root, GUI defaults |
| `assets/` | program icon (`app.ico`) and logos (`logo.png`, `logo-64.png`, `logo-32.png`) |
| `data/accessory_affixes.json` | the legal affix catalogue the editor validates against |
| `data/grace_affixes.json` | 恩宠/套装组合 name table — labels an accessory's last slot, never used to allow an edit |
| `bin/Nioh_Savefile_decrypt.exe` | bundled crypto helper (fast path; pure Python is the fallback) |
| `third_party/source-data/` | the original `.xlsx` / `.CT` inputs (regenerate the catalogue yourself) |
| `README.md`, `CHANGELOG.md` | documentation, copied for offline reading |
| `.extracted-manifest.json` | bookkeeping (hash of every file this program wrote) |

Rules that make this safe to keep next to your own files:

* A file you edited is **never overwritten** by a later version of the exe --
  only files whose contents still match what was extracted are refreshed.
* Deleting a file restores it on the next run.
* If the folder is read-only (e.g. `Program Files`), the program still runs and
  simply reads resources from inside the executable instead of writing them.
* Keep the exe in a writable folder of your choice; backups are written to the
  same folder (`_nioh3_accessory_backup/`) unless the config says otherwise.

The executable itself unpacks its Python runtime into the usual per-session
`%TEMP%\_MEIxxxx` directory (standard one-file behaviour, deleted on exit); the
files listed above are the only ones that stay on disk.

### Icon and logo

Everything the program shows is generated from code, so the art is reviewable and
licence-free (no Koei Tecmo assets, no unexplained binary blob):

```powershell
python tools/make_icon.py            # (re)write assets/
python tools/make_icon.py --check    # CI-style check: assets/ == generator output
python tools/make_icon.py --preview build\icon-preview.png
```

* The motif is a 勾玉 (magatama): a fat head tapering to a point, which is what
  distinguishes it from a plain crescent (the two bounding circles are internally
  tangent).  Small sizes are drawn bolder and without the cord hole so 16 px stays
  readable, and `tests/test_icon.py` asserts that (gold coverage per size).
* `assets/app.ico` carries all nine sizes (16–256) and is compiled into the
  executable, so Explorer, the taskbar and the window title bar all show it.
* The GUI loads `assets/logo-64.png` for the header and `assets/app.ico` for the
  window icon, and falls back to text if you delete them -- branding is never a
  hard dependency.  Since `assets/` is extracted next to the exe like everything
  else, you can drop in your own `logo-*.png` / `app.ico` and keep it: your files
  are never overwritten.
* The build refuses to ship stale art: `--check` fails first, and after packaging
  `tools/check_exe_icon.py` parses the PE resource directory and requires all nine
  sizes to be present and byte-identical to `assets/app.ico`.

### Parameter file

```powershell
# show the effective settings (file + defaults)
.\Nioh3AccessoryEditor.exe config

# same, as JSON
.\Nioh3AccessoryEditor.exe config --json

# (re)create config/editor.json with comments explaining every key
.\Nioh3AccessoryEditor.exe config --init
.\Nioh3AccessoryEditor.exe config --init --force     # overwrite an existing file
.\Nioh3AccessoryEditor.exe --config D:\my\editor.json config   # use another file
```

The file is deliberately strict: a wrong `schema`, an unknown key (e.g.
`dry_run` instead of `default_dry_run`) or a wrong type is reported on startup
instead of being silently ignored. Keys starting with `_` are comments.
Relative paths are relative to the exe, so the folder stays portable. Command
line options always win over the file.

`--python-crypto`, `--save-index N` and `--account ID` are global options and
must precede the subcommand. `--no-verify` skips the pre/post write decryption
checks (faster, riskier); `--force-while-running` overrides the game-process
gate.

Every command except `edit` and `restore --from ...` is strictly read-only
(`restore` without `--from` only lists). A write refuses to touch the save when:
the save is being written by the game (quiescence check), Nioh 3 is running
(unless overridden), an affix is outside the legal table, the target record does
not exist in the save, the staged re-encryption does not decrypt back to the
patched bytes, or the chosen backup is corrupt.

## Backup and restore

**Every write backs the save up first.** A backup holds the *decrypted* bytes, so
restoring is not a file copy: the tool re-encrypts them exactly as an edit does.

Where backups go — `<state root>` is `backup_root` from `config/editor.json`, else
the folder holding the exe (or the current directory when run from source):

```text
<state root>/_nioh3_accessory_backup/
    account-<steam id>/          # e.g. account-76561198000000000
        slot-<NN>/               # e.g. slot-03, matching SAVEDATA03
            SAVEDATA-<YYYYmmdd-HHMMSS>-<uuid8>-plain.bin
            backup-manifest.json
```

| File | What it is |
| --- | --- |
| `SAVEDATA-*-plain.bin` | the save exactly as the editor read it, **decrypted** (`RNNUSR`, `0x9001B0` bytes) — a plaintext backup, *not* a copy of `SAVEDATA.BIN` |
| `backup-manifest.json` | schema, account, slot, `created_at`, `main_save_sha256` (the encrypted file as it was) and `plain_backup_sha256` |

Backups are never overwritten — every one gets its own timestamped name — so they
accumulate and you can roll back to any point.

```powershell
# list this save's backups, newest first (read-only)
python launch_editor.py restore --list

# dry run: validate the backup and every gate, write nothing
python launch_editor.py restore --from latest --dry-run

# restore the newest one; also --from 2 (an index) or --from D:\my-plain.bin
python launch_editor.py restore --from latest
```

The GUI has the matching **恢复备份** (restore backup) button: it lists the backups
with their time and size, then asks for confirmation before writing.

A restore is itself reversible: the current save is backed up *before* it is
overwritten, and the report prints where that safety copy went
(`safety_backup_dir`). It refuses to write when the backup's SHA-256 no longer
matches its manifest or when the bytes are not a valid `RNNUSR` save of
`0x9001B0` bytes, and it obeys the same game-running gate, quiescence re-check
and `SAVE_SYNC_ACTIVE` refusal as an edit.

`matches_original_save` in the report says whether the restored file is
byte-identical to the file that was backed up. It can honestly be `false` for a
reason worth knowing: the reference crypto tool zeroes the save's last 8 bytes —
which lie outside the encrypted region — in **both** directions, so a backup
cannot record them. The restore then leaves those bytes as the game wrote them
instead of zeroing them (`tail_source: "on-disk"`): the payload is restored, the
tail is preserved, and the report says exactly that. With the pure-Python backend
the tail round-trips and a byte-identical restore is reported as `true`.

> The game keeps its own `BACKUP.BIN` next to `SAVEDATA.BIN`. This tool never
> reads or writes it; it appears in the fingerprint checks only, so a running game
> writing there still counts as activity.

## Build

```powershell
# Release: stamp build identity, build the single-file exe,
# smoke-test it in a fresh folder, then zip it -> dist/
# (the unit-test suite is NOT part of this: pass -Test to include it)
powershell -File .\build.ps1

# Same, but verify first -- use this before releasing a changelog entry
powershell -File .\build.ps1 -Test

# Drop older dist/ artifacts first (only Nioh3AccessoryEditor* entries)
powershell -File .\build.ps1 -Clean -Test

# Only refresh the build identity of the working tree (no dist, no zip)
powershell -File .\build.ps1 -Configuration Debug

# Only this test module, e.g. while iterating on the CLI (-TestPattern implies -Test)
powershell -File .\build.ps1 -Configuration Debug -TestPattern test_cli.py

# Faster iteration / custom interpreter
powershell -File .\build.ps1 -SkipZip -Python D:\Python310\python.exe
```

`build.ps1` resolves the interpreter, reads the git facts, generates the build
identity, stages a runnable tree under `dist\`, and smoke-tests that staged copy
(it must report the frozen commit, not whatever git says now). The unit-test
suite is **opt-in**: a default build is a packaging build and skips it, because
the suite takes minutes and none of the packaging steps depend on it — the final
report always states `单元测试 : 已运行 / 未运行`, so a build log can never be
mistaken for a verified one. Parameters: `-Python`, `-Configuration
Release|Debug`, `-OutputDirectory`, `-PyInstallerPython`, `-Test`, `-TestPattern`
(implies `-Test`), `-PureCryptoTests` (implies `-Test`), `-SkipZip`, `-Clean`,
`-Quiet`, plus the legacy `-SkipTests` (now a no-op, since skipping is the
default). It works on Windows PowerShell 5.1 and PowerShell 7+.

> `build.ps1` is saved as UTF-8 **with BOM** on purpose: Windows PowerShell
> reads a BOM-less script with the ANSI code page (GBK on zh-CN), which would
> corrupt its Chinese messages and break parsing. Keep the BOM when editing.

### Version information

Every version surface reports the same four facts:

```text
Nioh3AccessoryEditor v0.1.0
commit    : 34cca8de (工作区有未提交改动)
来源      : D:\wherever-you-put-it\Nioh3AccessoryEditor
加密组件  : D:\wherever-you-put-it\Nioh3AccessoryEditor\bin\Nioh_Savefile_decrypt.exe
构建时间  : 2026-09-19T11:01:52+08:00
语言      : CPython 3.10.10 (仅标准库 / stdlib only, 含 tkinter GUI)
构建来源  : D:\AIWorkspace\DSHWorkSpcae\Nioh3AccessoryEditor
```

| Fact | Meaning |
|---|---|
| `commit` | the **last 8 characters** of the git commit id (project convention, not the usual prefix) |
| `来源` / `加密组件` | where the running copy **is right now**: the folder holding the executable and the crypto component it reads from there. Move the exe and this follows it — no rebuild needed |
| `构建时间` | local build time, ISO-8601 with UTC offset |
| `语言` | language/runtime the build targets (CPython + stdlib only) |
| `构建来源` | only shown when the folder the build ran in differs from where the copy runs now; `BUILD-INFO.txt` always records the build-time path |

Where it shows up:

* `python launch_editor.py --version` (or the `version` subcommand; add `--json`
  for machine-readable output),
* the GUI footer, plus the **版本信息** button for the full banner (full commit,
  branch, component SHA-256, information source),
* `BUILD-INFO.txt` / `BUILD-INFO.json` in the project root and inside the
  staged `dist\` tree, and the build log itself.

`tools/make_build_info.py` generates `nioh3_accessory_editor/_buildinfo.py`
(frozen facts, imported at runtime), `BUILD-INFO.json` and `BUILD-INFO.txt`. It
verifies its own output by re-importing the generated module and comparing every
field, and it fails the build if the short commit is not exactly 8 characters.
The generated module, both `BUILD-INFO.*` files and `dist/` are gitignored
build artifacts: a fresh checkout reports live git information
(`信息来源: git`) with `构建时间: 未构建（源码运行）` until the next build.

## Tests

```powershell
# Whole suite (645 tests, ~7 min; needs bin/Nioh_Savefile_decrypt.exe)
python tools/run_tests.py

# Verbose / single module / keyword filter
python tools/run_tests.py -v
python tools/run_tests.py -p test_records.py
python tools/run_tests.py -k game_process

# Also run the slow full-file pure-Python crypto round trip (~3.5 min extra)
python tools/run_tests.py --pure-crypto
```

The suite covers the crypto layer (golden key schedule, exe-observed keystream,
region coverage, involution/XOR equivalence), the checksum fold, record
parsing/patching, the affix catalog schema, save discovery + durability +
backup + rollback, the editor/CLI pipelines, a full CLI write against a
synthetic save on disk, the 恩宠 legality gate (a 恩宠 slot is writable; a
专属套装 effect such as 怨恨盖世, a plain 词条 in the last slot, an unknown family
byte and an unlisted id are all refused, and the write moves only the id bytes),
the display-only item table (including that an item id can never become an
editable affix, and that the committed JSON still matches the workbook),
and the backup/restore path — including a real
encrypt → back up → damage → restore round trip asserting that the restored file
is byte-identical to the backed-up one (and that the uncovered tail is never
zeroed). Tests that need the reference executable or the sibling reference
checkout skip themselves when those are unavailable.

### Heavyweight cross-check (manual)

```powershell
# Encrypt a 9.4 MB synthetic save with both backends and compare every byte
python tools/crosscheck_crypto_vs_exe.py
```

This is the check that pins the pure-Python crypto to the reference: it
compares all 9,437,608 crypto-covered bytes and reports the trailing-8-byte
contract. It caught a real regression during development (an integer-division
bug that left the last 8 header bytes unencrypted), so keep it green after any
crypto change.

## Regenerate the affix catalog

```powershell
python tools/build_affix_db.py            # writes data/accessory_affixes.json
                                          #   and data/grace_affixes.json
python tools/build_affix_db.py --dry-run  # parse + report only
```

The default source is the bundled copy
`third_party/source-data/仁王3词条装备库v2.21.xlsx` (see that folder's README for
provenance and licensing); pass an explicit path (xlsx or the `A1=...` TSV dump)
to override. The builder de-duplicates by effect id and records any
same-id/different-value conflict in the catalog's `conflicts` field instead of
silently picking one.

Two tables come out of one workbook, deliberately kept apart:

* **`data/accessory_affixes.json`** — the 饰品词条 sheet (276 ids). This is what
  the editor accepts as an edit; anything else fails closed.
* **`data/grace_affixes.json`** — the 恩宠/套装组合 rows of 词条总目录 (77 ids:
  恩宠 10, 上位恩宠 11, 武士套装 34, 忍者套装 22). It names the last effect slot of
  an accessory (`0x71f6 不动明王的恩宠（上位恩宠）`) instead of showing an unknown
  id, and the 21 恩宠/上位恩宠 rows are also the allowed **targets** of a 恩宠
  change (`edit --grace`, the GUI 恩宠 row). The 56 套装 rows are never writable:
  they stay display-only, because armour carries 套装 codes too, so those ids are
  not accessory-affix evidence — and the editor additionally requires the slot
  being replaced to carry the 恩宠 family byte (0x0C), which a 专属套装 effect
  (e.g. 怨恨盖世, 0x4C) never does.
* **`data/accessory_items.json`** — the 饰品 rows of 物品总目录 (89 rows → 88 ids:
  武士饰品 46, 忍者饰品 42; one id, `0x1521`, is listed twice — 八咫镜[武士] and
  [忍者] — which is recorded in the table's `conflicts` field instead of being
  hidden). This is what an accessory **is**, keyed by the record header's per-item
  id, and it is display only like the tables above: the ids live in a different
  space from 词条 ids (measured: zero overlap with the 276 affix ids and the 77
  grace/set ids), and `list`/GUI only name the record with them.

## Verified facts vs. unverified boundaries

Verified against the reference executable (golden values captured from its
debug output and full-file byte comparison):

* `_key_setup` header key/IV pairs match byte-for-byte
  (`CD1F3135…84B` / `1BDFDD57…925` / `CD958C0E…4D5` / `FD4C40A2…7BD`).
* The header keystream prefix matches the observed `31d530acb6d67a46`.
* Full-file encryption matches the exe for every byte in the crypto-covered
  region; only the trailing 8 bytes (outside the crypto scope) differ, and the
  writer restores those from the on-disk file.
* The ported S-box/RS-box tables are byte-identical to the reference `aes.c`.

Useful crypto findings (documented because they are easy to get wrong):

* The reference's **decrypt branch is not the inverse of its encrypt branch**:
  its active `rsbox` is a copy of its `sbox`, and the custom S-box is not even a
  permutation (164 distinct values, 69 repeated, 92 unreachable). Decryption is
  performed by XORing the *same* ECB keystream that encryption produces, which
  is what this project does; `NiohAes.decrypt_block` exists only to mirror
  `aes.c` exactly and is never used by the pipeline.
* The header pass covers all `0x158` bytes using 22 counter blocks, i.e. a
  partially used final block. Truncating that (integer division instead of
  ceiling) silently leaves the last 8 header bytes unencrypted.

| Item | Value |
|---|---|
| Save size | `0x9001B0` (header `0x158` + body `0x900058`) |
| Crypto coverage | `[0, 0x158 + 0x900050)`; last 8 bytes preserved verbatim |
| Record region | located at runtime (captured reference layout: `0x176CCE`, 400 slots × `0xE8`; real v2.21 save: `0x270066`, 2000 slots × `0xF0`) |
| Record stride | detected per save from the recognized records (`0xE0`/`0xE8`/`0xF0`/`0xF8`/`0x100` are swept) |
| Effect slots | 7 slots × `0x18`, starting at `0x34` — confirmed on a real v2.21 save |
| Slot fields | `<6I`: prefix@0, effect_id@4, value@8, metadata@0xC, tail_0@0x10, tail_1@0x14 |
| Value semantics | slot `+0x08` equals the catalog's own `value` (e.g. `精力恢复速度 +3.0%` → 30) — confirmed 13/13 on a real save |
| Checksum | body `[0x190, 0x900190)` in `0x400` blocks; seed@`0x900190`, value@`0x900194` |
| Affix code | bytes0-3 effect id, bytes4-7 value, byte9 bit6 固定, byte10 bit2 星|

How the record region is found: a slot is recognised by the reference project's
own test — the type word and the level word each mirror themselves (`type@0 ==
type@2`, `level@6 == level@8`), the type is not zero, and the slot is either a
scroll type or has `1` at `@4`. That `@4` word is the middle 16 bits of the
account id (Steam64 ids of this magnitude always carry `0x0001` there), which is
what keeps the predicate stable. Recognised slots are grouped by offset modulo a
candidate stride; the stride whose largest residue class holds an order of
magnitude more records than any other is the save's real stride (one save: 1457
records on `0xF0` versus 58 on `0xE8`), and that class's extent gives the array.
Requiring the mirrored words matters: a free effect slot holds `0xFFFFFFFF` and
would otherwise mirror itself into a fake nested record. A random block passes
with probability ≈ `1/2^48`, so hits are evidence, and a save with no such
evidence fails closed instead of editing guessed bytes.

Which records are accessories: the shipped catalog contains 饰品词条 only, so a
record whose occupied effect slots name catalog ids is an accessory, and one
whose affixes never hit the catalog (weapons, armour, 绘卷) is not. In v2.21 this
is the only workable rule, because the field at record+`0x00` holds a per-item id
(`a5 6f a5 6f` = `0x6FA5` self-duplicated) rather than the captured category ids.
One real save therefore resolves to **213 accessories out of 1456 item
records**.

What an edit actually writes: the slot's `effect_id` and `value` fields. The
`metadata` field (which is where the 固定/星 flag bits are expected to live) is
**never** modified by the GUI, because the slot-level encoding of those bits has
not been confirmed against a real save; the CLI can set it explicitly with
`--edit <slot>:<id>:<value>:<metadata>` when you know what you want. Selecting a
词条 whose id already occupies the slot is treated as "no change" and writes
nothing.

> **⚠️ Honest boundary:** the record stride, the effect-slot base (`0x34`), the
> `effect_id` position (`slot+0x04`) and the value semantics (`slot+0x08`) are
> now confirmed against a real decrypted v2.21 save, and a write to a copy of that
> save changed **exactly 5 bytes** (3 in the target slot, 2 in the checksum
> field). A real save also confirms the in-game layout: every accessory ends with
> a 恩宠 or 套装组合 effect (e.g. 稻荷神的恩宠 on a 龙笛), named from the
> workbook's 词条总目录 — of the 231 trailing slots in that save, 215 were named
> and 16 ids across 12 kinds were not in the workbook at all, so an unnamed slot
> still reads `恩宠/套装词条（表外，id=…）`. Still unverified: which record id
> belongs to which *item* (the game's item-name table is not in the save), and the
> meaning of the `metadata` bits (the workbook's byte 9/10 do match what the save
> holds for 恩宠 (`0x0C 0x02`) and 套装 (`0x4C 0x01`) codes, but that is a
> single-save observation, not a confirmation). **Run `list` (or `scan`) on your
> own save first, confirm the listed records and affixes look right, and keep the
> automatic backup** before writing.
>
> **恩宠 edits specifically:** the rule "only `xxx的恩宠` → another `xxx的恩宠`" is
> enforced from three independent facts — the slot's family byte (0x0C, measured on
> all 196 恩宠 slots of that save vs 0x4C on all 16 套装 slots), the workbook's 归属
> (恩宠 / 上位恩宠 only) and the trailing-slot position. What is **not** verified:
> whether 上位恩宠 is gated in game by level/difficulty (the edit does not check
> that), what metadata byte 10 (0x02 vs 0x0A) and byte 11 (0x00/0x18/0x29/0xDB/…
> for the *same* 恩宠 on different accessories) actually mean — both are therefore
> copied from the slot being replaced instead of being invented, and whether the
> in-game item list refreshes cleanly after such a swap (only load the save and
> look). If the swapper shows something odd, restore the automatic backup.
>
> **饰品种类 (which accessory it is) and 等级 are writable, with the limits
> below.** The field is known — the record header's per-item id at `+0x00`, with a
> mirror at `+0x02` (equal on all 213 records), resolving to
> `data/accessory_items.json` for 212 of them. Both fields are written only through
> the rules in [Changing 种类](#changing-种类-同分类互换) and
> [Changing 等级](#changing-等级), and both are measured on a copy of a real save
> first. What is **not** verified, and what the tool therefore refuses or flags:
> (1) what happens to the *existing* normal effect slots of a swapped record — a
> kind swap leaves them untouched, so an 八尺琼勾玉 can keep affixes that only a
> 龙笛 may carry (the game shows them, repairs them or rejects them — unknown), so
> the edit is caution-flagged in the CLI and confirmed in the GUI; (2) whether an
> item-specific 专属套装 effect (e.g. 怨恨盖世 on 凶王耳饰) must match the new kind —
> the 套装 slot is *not* rewritten by a swap, so the same kind of mismatch is
> possible there; (3) whether the game recomputes displayed values from 等级 —
> stored affix values are level-independent (measured), so any change must be the
> game's own derivation; (4) `0x1521` maps to two different items (八咫镜[武士] and
> [忍者]) and is reported as unlisted rather than guessed. Verify in game after a
> swap/level edit; the automatic backup restores the previous state.

### Changing 种类 (同分类互换)

`edit --kind <名称|id>` (CLI) or the GUI's 种类 row swaps which accessory a record
is, under three fail-closed rules:

1. **Same 中类 only** — the record's own id and the target id must both be 饰品 rows
   of 物品总目录 with the same 中类 (`武士饰品` ↔ `武士饰品`, `忍者饰品` ↔
   `忍者饰品`). A cross-category swap, or an id with no table row (including
   `0x5c5f`), is refused with the reason.
2. **The fixed affix is copied, never invented** — the target kind's 同名固定 affix
   is read from a real copy of that kind already in *your* save: its slot index,
   effect id, value and metadata byte 9. Measured over the 60 kinds with a fixed
   affix in the reference save, those fields are identical on every copy of a
   kind (58/60; the two exceptions are the 八咫镜 variants, which are refused as
   ambiguous). Metadata bytes 10/11 (only 25/60 kinds agree) and the slot `prefix`
   (4 kinds disagree) are **per-instance**, so they are left byte-for-byte as they
   were. If the target kind has no copy in this save, the swap is refused — the tool
   will not guess a fixed affix.
3. **Normal slots and the 恩宠/套装 slot are untouched** — only the header id (both
   mirrored copies) and the fixed slot(s) change. Measured on a copy of the real
   save: swapping record #3 (龙笛[武士] → 八尺琼勾玉[武士]) changes 7 bytes
   (4 header bytes + the fixed slot's id/value) plus the 3 checksum bytes, and the
   record reads back as `0x4987 八尺琼勾玉[武士]` with
   `(同名固定)组合效果的所需装备数 -1 id=0x53d4 数值=1`.

### The 魂核 tab

`python launch_editor.py souls` (CLI, read-only) or the GUI's 魂核 tab works on
soul cores with the same three edits, switched to the core tables by `--soul`:

| what | CLI | rule |
| --- | --- | --- |
| 魂核词条 | `edit --soul --record N --edit 3=<名称\|id>` | id must be one of the 287 `绘卷-魂核词条` rows with `种类 = 魂核` |
| 等级 | `edit --soul --record N --level 180` | 1..180, writes `+0x06`/`+0x08` only |
| 种类 | `edit --soul --record N --kind <名称\|id>` | 魂核 ↔ 魂核; the fixed affix is copied from a real sample |

* **No 恩宠/套装** — that falls out of the gate instead of being special-cased: an
  恩宠 id is simply not in the 魂核 pool, so `--soul --grace` is refused outright and
  the tab has no 恩宠 row. Measured on the save: 0 confirmed cores carry 恩宠/套装.
* **同名固定词条不可修改** — every confirmed core carries **two** fixed affixes
  (`#12 魑魅的魂核`: `id=0xd0c5` and `id=0x74df`); both rows are disabled and
  labelled `固定词条，禁止修改` in the tab and refused by the engine.
* **Verification** — the workbook's own 固定词条代码 matched the save on all 132
  confirmed cores with **0 mismatches**, which is what makes the catalog flag the
  right fixed-affix evidence here too.

### 筛选、关键词搜索、数值区间与「无中生有」

Five behaviours added on request, all fail-closed and all reachable from both the
GUI and the CLI:

| # | what | GUI | CLI | rule (evidence) |
| --- | --- | --- | --- | --- |
| 1 | 不能再加固定词条 | fixed rows are disabled | any write of a fixed id is refused | a 同名固定 affix is part of *what the item is*; 0→1 / 1→2 / 2→3 are all refused (measured: 795/795 slots agree with the catalog flag, 0 exceptions) |
| 2 | 改词条数值（限区间） | 数值 box per slot | `--edit 槽:名称或id:数值` | the value must be inside the affix's own span from `全词条数值(3稀有度…)` 取值集合; **807/807** catalogued slots of the reporting save are inside, 0 outside |
| 3 | 按种类 / 恩宠·套装筛选 | 筛选 row above the tree | `list --kind <名称\|id> --grace <名称\|id>` | filters are pure display: they change which rows are listed, never what is written |
| 4 | 关键词匹配词条 | 关键词 box → 搜索词条 | `edit --search <关键词>`, or `--edit 槽:关键词[:数值]` | **every** match is listed; 0 matches and >1 matches are both refused with the candidate list, so an ambiguous keyword can never silently pick one |
| 5 | 无中生有 | 无中生有 区块 on both tabs | `create --kind … [--soul]` | a new record is copied from a **same-kind real sample**; 背包满 is refused with 「背包已满…请先在游戏里清理背包后再添加」 |

Notes that matter in practice:

* **数值区间** — the 饰品词条 sheet has *no* span columns; the span lives in
  `全词条数值(3稀有度…)`'s 取值集合 column, keyed by the **low 16 bits** of the affix
  id (its 代码 column is little-endian, e.g. `BC 53` → `0x53BC`). 魂核 spans come from
  `绘卷-魂核词条`'s 数值区间/数值集合. Coverage after regenerating:
  **276/276** 饰品 and **287/287** 魂核 affixes carry a span.
  Most 饰品词条 are **single-valued** (`火抗性 +10` → 固定值 10: the value *is* the
  affix's identity), so the value box only really opens up for the rows the workbook
  gives a real range — e.g. `0xfb24 获得魂核时恢复体力` 158..230 (the only ranged 饰品
  row in this workbook). Where there is no span at all, only the catalog value may be
  written; the tool never guesses one.
* **固定词条优先** — a fixed slot is refused *before* the value check, so it reports
  「同名固定词条不能修改」 rather than a range error.
* **无中生有 的模板** — `find_free_slots` uses `type == 0` as the empty-slot marker
  (measured on the reporting save: **465** of 2000 slots, scattered — 209 of them have
  occupied neighbours on both sides — with clean effect slots). The new record is
  byte-copied from the lowest-indexed sample of the same 种类 and only 种类 (two
  mirrored words)、等级 (two mirrored words) and the chosen affixes are rewritten.
  That is deliberate: `+0x18` takes 6 distinct values across records while
  `+0x1c`/`+0x20`/`+0x28` are **unique per record** (213/213), and their meaning is not
  decoded — inventing them would be guessing, so a kind with no sample in the save is
  refused with 「存档里没有 … 的样本」 and nothing is written. The fixed affixes come
  from the template and cannot be edited in this flow.
* **背包已满** — with no `type == 0` slot left, creation refuses and prints
  「背包已满：记录表 2000 个槽位全部被占用，请先在游戏里清理背包后再添加」.
* **A read-only search** — `edit --search` loads and reports only; the save bytes are
  byte-identical afterwards (asserted in the tests).

### The reference save

Every measurement quoted in this README (213 accessories out of 1456 item records,
132 identified 魂核, 807/807 affix values inside their span, 795/795 slots agreeing
with the catalog's 同名固定 flag, 465 free slots, the two 八咫镜 ids) comes from the
same **reference save**: one ordinary, unmodified PC save file that the project was
developed against, always opened read-only. It is referred to that way — as *the
reference save*, or *a sample save* — because the tool is not tied to any particular
file; what is tied to one file is only the *evidence*, which is why each measured
claim below names it.

### Will it work on another player's save?

The parts that decide **what may be written** are not tied to that file:

* the legal affix, value-span, 恩宠/套装、魂核 and 种类 tables are generated from
  `仁王3词条装备库v2.21.xlsx`, not from a save;
* identification is evidence-based and fails closed: a 饰品 needs catalog affixes, a
  魂核 needs **both** an item-table row and a 魂核-affix hit, and anything that does
  not qualify is listed as 未识别 rather than edited;
* the record array is *located*, not assumed: the scanner votes between candidate
  strides and the winning stride's extent gives the array (see
  [the layout section](#verified-facts-vs-unverified-boundaries)); a save with no
  such evidence is refused instead of being edited at guessed offsets.

The parts that **were measured on one file** are the byte-level ones, and each has a
defined failure mode on a save that does not match:

| measured fact | if another save disagrees |
| --- | --- |
| `type == 0` is the empty-slot marker (465 of 2000 slots) | no `type == 0` slot means "背包已满" and creation is refused |
| 同名固定 = catalog flag **and** metadata byte-9 bit `0x40` (795/795) | a catalog-flagged slot is still refused; but where a save encoded 固定 differently, the byte evidence is unverified — check on a copy before trusting a slot the catalog calls editable |
| `+0x1c`/`+0x20`/`+0x28` are unique per record (213/213) | create/改种类 copy them from a same-kind sample **in the same save**; no sample of that kind → refused with 「存档里没有 … 的样本」 |
| level `+0x06` mirrors `+0x08`, cap 180 | mismatched mirrors or a higher cap → the edit is refused |
| 0x5c5f = 八咫镜[武士] (from that save's fixed affix) | ids are game-global, so this holds for every save; it is the *only* item row derived from save evidence |

So: on someone else's save the catalog-driven features (listing, filtering, keyword
search, value spans, fixed-affix protection, 恩宠 edits) should work as-is, while the
write paths that need a same-kind sample are refused with a message when the sample is
missing. To check a new file, run `check` and `list`: they print the located layout
(stride/offset/slot count, and whether it matches the reference project's captured
layout) plus how many records identified as accessories.

### The one field that is still unknown: `+0x0A`

Every other header word is pinned to something: `+0x00`/`+0x02` are the 种类 id,
`+0x04` the item count, `+0x06`/`+0x08` the level, `+0x0C` a constant, `+0x30` the
rarity. `+0x0A` is not. What is measured about it (reference save, 213 accessories):

* it takes **25 distinct values between 0 and 30**, and it is a property of the
  *instance*, not of the kind — 42 of 74 kinds show several values;
* it does not track level (a Lv170 item can be 0), rarity (神器 spans 0..30, 粗物 is
  always 0), or the number of fixed affixes;
* the **stored affix values do not change with it**: across 82 affixes that appear at
  both low (≤5) and high (≥15) `+0x0A`, the average stored value is identical. So it
  is not an on-disk stat multiplier — the same reason the 等级 edit does not rewrite
  affix values.

Because writing it is the only way to find out what the game shows, it has one
dedicated tool instead of a general edit path:

```powershell
python tools/set_plus.py --record 28 --value 0            # 演练，不写入
python tools/set_plus.py --record 28 --value 0 --write    # 真正写入（自动备份）
```

It refuses anything outside 0..30, verifies that **only** the two bytes of `+0x0A`
changed and aborts otherwise, and never touches an affix, a level or a mirror. The
safest first step needs no write at all: open the game and compare two same-kind
items that already differ in `+0x0A` (e.g. three 龙笛[武士] at 13 / 18 / 19).

### Changing 等级

`edit --level <1..180>` (CLI) or the GUI's 等级 row writes `+0x06` **and** its mirror
`+0x08` and nothing else. 180 is the game's serialisable maximum: the reference
project derives the effective level as `min(record +0x06, 180)`, and the reporting
user's save tops out at exactly 180. The edit refuses 0/negative/>180, refuses a
no-op, and refuses a record whose two level fields disagree (measured equal on all
213 accessories, so a mismatch means a shape this tool never verified). It is
flagged **谨慎修改** because the stored affix values do not scale with level:
measured across levels 135–180, every copy of a kind holds the same values (e.g.
除雷护身符[武士] `雷属性伤害降低 +15` at levels 156–170), so any in-game increase
must be computed by the game on load — which a save file cannot prove. The `+值`
field is **not** written: the only per-instance counter in the header (`+0x0A`,
0..30 on accessories) is an unconfirmed candidate for it, so the tool displays it
(`+值候选`) and waits for an in-game check.

## Safety model

* **Write requirement**: edits are written into the save **file**, so the game
  must not hold that save in memory. Close the game completely, or at least stay
  at the **title screen** (no save loaded, not in-game) before writing; a save
  made in-game afterwards overwrites the edit and can corrupt the slot. The rule
  is stated before every write (`savefile.SAVE_WRITE_REQUIREMENT`): in the CLI
  output, in the GUI notice line, and in the write-confirmation dialog. The GUI
  also shows the game-process state continuously (green/red line at the bottom,
  re-checked every 3 s on a worker thread), so the condition is visible before you
  click 写入存档 instead of only after a refusal.
* Backups: `<state root>/_nioh3_accessory_backup/account-<id>/slot-<NN>/` holds a
  decrypted `SAVEDATA-<timestamp>-<random>-plain.bin` plus `backup-manifest.json`
  (schema, account, slot, main-save SHA-256, plaintext SHA-256); see
  [Backup and restore](#backup-and-restore).
* Game process gate: a detected `Nioh3.exe` / `Nioh3-Win64-Shipping.exe` makes
  the writer fail closed (`GameRunningError`, CLI exit 1) unless
  `--force-while-running` is passed explicitly.
* Quiescence: save + `BACKUP.BIN` + the account system save are hashed twice
  0.2 s apart; any change aborts with `SAVE_SYNC_ACTIVE`.
* Fingerprint races: hashing re-stats the file and aborts if size/mtime moved.
* Write path: stage → decrypt-verify → re-check fingerprints → atomic replace →
  decrypt-verify again → roll back the original bytes if the final check fails.
* Restore path: the same pipeline plus a manifest/SHA-256 check on the backup and
  a fresh safety backup of the save being replaced, so a restore is undoable by
  restoring that copy. The save's last 8 bytes (outside the crypto region) are
  kept from the current file when the backup cannot carry them, never zeroed.
* The GUI shows the required disclaimer in the window title and footer, and
  defaults to 仅演练 (dry run).

## Credits & attribution

* Affix data: 仁王3词条装备库v2.21.xlsx by 神烦～ (3DM) / -神-烦- (bilibili),
  thanks QQ@ReIAm, QQ@MasterBayesian, QQ群 1106302479.
  Non-commercial use with credit; do not re-publish commercially.
* Save-crypto research: Nioh3-Scroll-Generator (rework of pawREP/
  Nioh-Savedata-Decryption-Tool), including the bundled reference exe and the
  custom-AES C source that this project's pure-Python port is derived from.
* Cheat Table: Nioh3 v2.21.CT (in-memory structure reference only).

## Disclaimer

This tool is for testing and learning only. Do not use it in online modes in a
way that affects game balance. Nioh 3's co-op is PvE-only; even so, respect the
game's terms of service. Always keep a backup of your save.
