"""Command-line interface for Nioh3AccessoryEditor.

Commands:
    list    Discover saves and list accessory-like records with their affixes.
    scan    Report the record-array diagnosis of a save (read-only, no game).
    edit    Apply effect-slot edits to a selected save (persisted to disk).
    backup  Create a plaintext backup of a save.
    check   Decrypt a save and report integrity without modifying anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import paths, records, version
from .affixdb import (
    AffixDb,
    AffixError,
    GraceDb,
    ItemDb,
    load_soul_catalog,
    load_soul_item_catalog,
)
from .bootstrap import ensure_once
from .config import ConfigError, EditorConfig, load_config, write_default_config
from .editor import (
    EditorError,
    GraceEditError,
    SaveDescriptor,
    accessory_catalog_ids,
    apply_edits,
    apply_grace_edit,
    apply_kind_swaps,
    apply_level_edits,
    apply_plus_edits,
    apply_soul_edits,
    apply_creations,
    commit_save,
    discover_saves,
    find_free_slots,
    grace_edit_availability,
    list_accessories,
    list_soul_cores,
    open_save,
    plan_creation,
    plan_kind_swap,
    plan_level_edit,
    plan_plus_edit,
    resolve_grace_id,
    resolve_item_id,
    restore_backup,
    save_checksum_is_valid,
    soul_catalog_ids,
)
from .records import RecordError
from .savefile import (
    SAVE_WRITE_REQUIREMENT,
    BackupEntry,
    SaveCrypto,
    SaveError,
    backup_directory_for,
    create_backup,
    default_crypto_tool,
    list_backups,
    running_game_processes,
    save_root_directory,
)
from .version import version_banner, version_info

DISCLAIMER = (
    "本工具仅供测试学习用，请勿用于联机环境或影响游戏平衡。\n"
    "仁王3 为单机/纯 PVE 联机游戏，本工具不会影响其他玩家。\n"
    "使用前请备份存档；作者对存档损坏不承担任何责任。"
)

#: How this program is invoked (source: ``python launch_editor.py``; frozen: exe).
PROG = "launch_editor.py"


def _crypto(args: argparse.Namespace) -> SaveCrypto:
    """Build the crypto backend, degrading to pure Python when the exe is gone."""
    config = _config(args)
    if bool(getattr(args, "python_crypto", False)):
        return SaveCrypto(None, prefer_python=True)
    backend = config.crypto_backend()
    if backend.note:
        print(f"提示：{backend.note}", file=sys.stderr)
    return SaveCrypto(backend.executable, prefer_python=backend.prefer_python)


def _config(args: argparse.Namespace) -> EditorConfig:
    """Load the configuration file named by ``--config`` (cached per run)."""
    cached = getattr(args, "_config_cache", None)
    if cached is not None:
        return cached
    explicit = getattr(args, "config", None)
    config = load_config(Path(explicit) if explicit else None)
    # Command-line filters win over the file.
    config = config.with_overrides(account=getattr(args, "account", None),
                                   save_index=getattr(args, "save_index", None))
    try:
        args._config_cache = config
    except AttributeError:  # pragma: no cover - argparse namespaces accept it
        pass
    return config


def _state_root(args: argparse.Namespace) -> Path:
    """Where backups go: config value, else next to the exe / current directory."""
    return _config(args).resolved_backup_root()


def _select_save(args: argparse.Namespace) -> SaveDescriptor:
    config = _config(args)
    saves = discover_saves(config.resolved_save_root())
    if not saves:
        root = config.resolved_save_root() or save_root_directory()
        raise EditorError(
            f"未发现 Nioh 3 存档。请确认游戏已运行过且存档位于 {root}"
        )
    account = config.account
    if account is not None:
        saves = tuple(save for save in saves if save.account_id == account)
        if not saves:
            raise EditorError(f"未找到账号 {account} 的存档")
    slot = config.save_index
    if slot is not None:
        saves = tuple(save for save in saves if save.slot_index == slot)
        if not saves:
            raise EditorError(f"未找到栏位 {slot} 的存档")
    if len(saves) > 1 and slot is None:
        print(f"提示：发现 {len(saves)} 个候选存档，使用第一个；可用 --save-index/--account 选择。",
              file=sys.stderr)
    return saves[0]


def cmd_souls(args: argparse.Namespace) -> int:
    """List the save's 魂核 records (read-only)."""
    soul_db = AffixDb(load_soul_catalog())
    soul_item_db = ItemDb(load_soul_item_catalog())
    known_ids = soul_catalog_ids(soul_db)
    crypto = _crypto(args)
    save = _select_save(args)
    print(f"存档: {save.display}")
    print(f"路径: {save.path}")
    data = open_save(save, crypto)
    try:
        layout = records.locate_layout(data, known_ids=known_ids)
    except RecordError as error:
        print(f"\n未找到物品记录表：{error}")
        return 1
    print(f"记录表: {layout.describe('魂核')}")
    cores = list_soul_cores(data, soul_db=soul_db, soul_item_db=soul_item_db,
                            layout=layout, known_ids=known_ids)
    identified = [core for core in cores if not core.unidentified]
    print(f"\n魂核词条库 {len(soul_db)} 条 / 魂核种类表 {len(soul_item_db)} 条；"
          f"候选记录 {len(cores)} 条，其中已识别 {len(identified)} 条")
    print(f"列出 {len(identified)} 条魂核记录（魂核没有恩宠/套装词条）\n")
    for core in cores:
        print(
            f"记录 #{core.slot_index} @ {core.offset:#x}  "
            f"Lv{core.level} {core.rarity_name}  魂核词条命中 {core.catalog_hits}"
        )
        for line in core.describe_effects(soul_db, soul_item_db):
            print(line)
        print()
    if not identified:
        print("该存档里没有能识别的魂核记录。")
    return 0


def _resolve_keyword(affix_db: AffixDb, keyword: str, *, what: str = "词条"):
    """Resolve a keyword to exactly one entry, or explain why not.

    Requirement (4): typing a keyword must show **every** match instead of
    silently picking the first one.  Zero matches and several matches are both
    errors that list what the user can do next.
    """
    matches = affix_db.search(keyword)
    if not matches:
        raise EditorError(
            f"没有匹配「{keyword}」的{what}。可用 edit --search {keyword} 查看，"
            "或换一个更短的关键词（也可以直接写词条 id，如 0x0b32）"
        )
    if len(matches) > 1:
        listing = "\n".join(f"    {entry.label}（数值 "
                            f"{entry.describe_value_range()}）" for entry in matches[:20])
        more = "" if len(matches) <= 20 else f"\n    …另有 {len(matches) - 20} 条"
        raise EditorError(
            f"「{keyword}」匹配到 {len(matches)} 条{what}，请写得更具体或直接用 id：\n"
            f"{listing}{more}"
        )
    return matches[0]


def _parse_effect_specs(affix_db: AffixDb, specs: list[str], *,
                        what: str = "词条") -> list[dict[str, int]]:
    """Parse ``slot:名称或id[:数值]`` specs into edit dicts (keyword aware)."""
    edits: list[dict[str, int]] = []
    for spec in specs:
        parts = spec.split(":")
        if len(parts) < 2 or len(parts) > 3:
            raise EditorError(f"--effect 格式应为 slot:名称或id[:数值]，实际 {spec!r}")
        try:
            slot = int(parts[0], 0)
        except ValueError as error:
            raise EditorError(f"--effect 的槽位必须是数字：{parts[0]!r}") from error
        target = parts[1].strip()
        entry = None
        if target:
            try:
                effect_id = int(target, 0)
            except ValueError:
                entry = _resolve_keyword(affix_db, target, what=what)
                effect_id = entry.effect_id
            else:
                entry = affix_db.lookup(effect_id)
                if entry is None:
                    entry = _resolve_keyword(affix_db, target, what=what)
                    effect_id = entry.effect_id
        else:
            effect_id = records.EMPTY_EFFECT_ID
        edit: dict[str, int] = {"slot_index": slot, "effect_id": effect_id}
        if len(parts) == 3:
            text = parts[2].strip()
            if not text:
                raise EditorError(f"--effect 的数值不能为空：{spec!r}")
            try:
                edit["value"] = int(text, 0)
            except ValueError as error:
                raise EditorError(f"--effect 的数值必须是数字：{text!r}") from error
        edits.append(edit)
    return edits


def _matches_kind_filter(item_db: ItemDb, record_type: int, needle: str) -> bool:
    """Whether a record's 种类 matches a name/id substring (filter for `list`)."""
    name = item_db.describe(record_type)
    haystack = f"{record_type:#06x} {record_type:#x} {name or ''}".lower()
    return needle.strip().lower() in haystack


def _grace_name_of(view, grace_db) -> str:
    """The 恩宠/套装 name(s) of a view's trailing slot ("" when it has none)."""
    names: list[str] = []
    for effect in view.occupied_effects:
        entry = grace_db.lookup(effect.effect_id)
        if entry is not None:
            names.append(entry.name)
    return " ".join(names)


def cmd_create(args: argparse.Namespace) -> int:
    """无中生有: write a brand-new 饰品/魂核 into a free slot."""
    affix_db = AffixDb()
    grace_db = GraceDb.best_effort()
    item_db = ItemDb.best_effort()
    if args.soul:
        affix_db = AffixDb(load_soul_catalog())
        item_db = ItemDb(load_soul_item_catalog())
    crypto = _crypto(args)
    save = _select_save(args)
    print(DISCLAIMER)
    print(f"存档: {save.display}")
    data = open_save(save, crypto)
    known_ids = (soul_catalog_ids(affix_db) if args.soul
                 else accessory_catalog_ids(affix_db))
    layout = records.locate_layout(data, known_ids=known_ids)

    # 种类 must resolve to exactly one row of the item table.
    item_matches = item_db.search(args.kind)
    if not item_matches:
        raise EditorError(f"没有匹配「{args.kind}」的{'魂核' if args.soul else '饰品'}种类")
    if len(item_matches) > 1:
        listing = "\n".join(f"    {entry.label}" for entry in item_matches[:20])
        raise EditorError(
            f"「{args.kind}」匹配到 {len(item_matches)} 个种类，请写得更具体：\n{listing}")
    item = item_matches[0]

    report = find_free_slots(data, layout=layout)
    print(f"空位: {report.describe()}")
    edits = _parse_effect_specs(affix_db, args.effect,
                                what="魂核词条" if args.soul else "词条")
    plan = plan_creation(
        data, record_type=item.item_id, level=args.level, effects=edits,
        affix_db=affix_db, item_db=item_db, known_ids=known_ids, layout=layout,
        slot_index=args.slot, soul=args.soul,
    )
    print(f"新建计划: {plan.describe(item_db)}")
    for slot in plan.effects:
        if slot.is_empty:
            continue
        entry = affix_db.lookup(slot.effect_id)
        role = "（固定词条，按模板带入）" if entry and entry.is_fixed else ""
        print(f"    槽{slot.slot_index + 1}: {affix_db.describe(slot.effect_id)}"
              f"（数值={slot.value}）{role}")
    patched = apply_creations(data, [plan])
    print("\n" + SAVE_WRITE_REQUIREMENT)
    running = running_game_processes()
    if running:
        print(f"当前状态：检测到 {'、'.join(running)} 正在运行——"
              f"{'已确认停在标题界面（--at-title-screen）' if _title_screen_confirmed(args) else '将被拒绝'}。")
    else:
        print("当前状态：未检测到游戏进程。")
    if args.dry_run:
        print("本次为演练模式（--dry-run），不会写入存档。")
    result = commit_save(
        save, patched, crypto=crypto, state_root=_state_root(args),
        dry_run=args.dry_run, verify=not args.no_verify,
        allow_game_running=_title_screen_confirmed(args),
    )
    if result.get("dry_run"):
        print("演练完成：校验通过，未写入存档。")
    else:
        print(f"已写入: {save.path}")
    print("请进游戏确认新物品是否正常出现；有问题可用 restore 还原。")
    return 0


#: ``list --kind`` 认这四个大类；**其余值仍是原来的"按物品名称/id 过滤"**。
LIST_BIG_KINDS = ("武器", "防具", "饰品", "魂核")


def _print_equipment_slots(view) -> None:
    """一件武器/防具的一行摘要 + 每个槽一行（空槽也列出来）。"""
    print(f"记录 #{view.slot_index} @ {view.offset:#x}  {view.item_label}  "
          f"[{view.category}/{view.small}]  Lv{view.level} +{view.plus_value}  "
          f"{view.rarity_name}  池 {view.pool_label}")
    for slot in view.slots():
        mark = " [固定]" if slot.is_fixed else (" [★]" if slot.is_star else "")
        tags = "/".join(slot.equipment_tags) or "-"
        if slot.is_empty:
            print(f"    槽{slot.slot_index + 1} （空）")
            continue
        print(f"    槽{slot.slot_index + 1} {slot.name}  数值 {slot.value}  "
              f"种类 {slot.category or '-'}  装备种类 {tags}{mark}")
    print()


def _list_big_records(args: argparse.Namespace, data: bytes, layout, big: str) -> int:
    """按大类列出记录（list --kind 武器 / 防具 / 魂核）。"""
    from .editor import list_equipment  # 只在走大类分支时才用得到

    print(DISCLAIMER)
    print()
    if args.grace:
        print("（--grace 只对饰品有效，这里按大类列出，已忽略该筛选）\n")
    if big == "魂核":
        soul_db = AffixDb(load_soul_catalog())
        views = list_soul_cores(data, layout=layout)
        print(f"记录表内 {layout.slot_count} 槽，其中魂核记录 {len(views)} 条\n")
        for view in views:
            print(f"记录 #{view.slot_index} @ {view.offset:#x}  "
                  f"Lv{getattr(view, 'level', '?')}  {view.describe_item()}")
            for effect in view.effects:
                if effect.is_empty:
                    continue
                entry = soul_db.lookup(effect.effect_id)
                print(f"    槽{effect.slot_index + 1} "
                      f"{entry.name if entry else f'{effect.effect_id:#010x}'}")
            print()
        if not views:
            print("该存档里没有魂核记录。")
        return 0

    views = list_equipment(data, big=big, layout=layout)
    print(f"记录表内 {layout.slot_count} 槽，其中{big}记录 {len(views)} 条\n")
    for view in views:
        _print_equipment_slots(view)
    if not views:
        print(f"该存档里没有{big}记录。")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    affix_db = AffixDb()
    grace_db = GraceDb.best_effort()
    item_db = ItemDb.best_effort()
    known_ids = accessory_catalog_ids(affix_db)
    crypto = _crypto(args)
    save = _select_save(args)
    print(f"存档: {save.display}")
    print(f"路径: {save.path}")
    data = open_save(save, crypto)
    print(f"校验和一致: {'是' if save_checksum_is_valid(data) else '否（存档可能来自其他版本或被修改过）'}")
    try:
        layout = records.locate_layout(data, known_ids=known_ids)
    except RecordError as error:
        print(f"\n未找到物品记录表：{error}")
        print("请运行 scan 命令查看诊断，并把输出反馈给作者。")
        return 1
    print(f"记录表: {layout.describe()}")
    wanted_big = (args.kind or "").strip()
    if wanted_big in LIST_BIG_KINDS and wanted_big != "饰品":
        return _list_big_records(args, data, layout, wanted_big)
    # list 本来就是饰品范围：--kind 饰品 等于不加名字过滤（其余值含义不变）。
    kind_filter = args.kind
    if wanted_big == "饰品":
        kind_filter = ""
        print(DISCLAIMER)
        print()
    views = list_accessories(data, layout=layout, known_ids=known_ids)
    accessories = [view for view in views if view.is_accessory is not False]
    total = len(accessories)
    if kind_filter:
        accessories = [view for view in accessories
                       if _matches_kind_filter(item_db, view.record_type, kind_filter)]
    if args.grace:
        needle = args.grace.strip().lower()
        accessories = [view for view in accessories
                       if needle in _grace_name_of(view, grace_db).lower()
                       or needle in f"{view.record_type:#x}"]
    if kind_filter or args.grace:
        filters = []
        if kind_filter:
            filters.append(f"种类含「{kind_filter}」")
        if args.grace:
            filters.append(f"恩宠/套装含「{args.grace}」")
        print(f"\n筛选（{'，'.join(filters)}）：{total} 件中 {len(accessories)} 件")
        if not accessories:
            print("没有符合条件的饰品；去掉筛选参数可看全部。")
    print(f"\n记录表内 {len(views)} 条物品记录，其中 {total} 条含饰品词条")
    print(f"列出 {len(accessories)} 条饰品记录\n")
    for view in accessories:
        # 种类 is printed by describe_effects below, so the header keeps the
        # numbers only.
        print(
            f"记录 #{view.slot_index} @ {view.offset:#x}  "
            f"Lv{view.level} {view.rarity_name}  词条命中 {view.catalog_hits}"
        )
        for line in view.describe_effects(affix_db, grace_db, item_db):
            print(line)
        print()
    if not accessories:
        print("该存档的记录表里没有含饰品词条的记录。")
        print("请运行 scan 命令查看记录表诊断，并把输出反馈给作者。")
    elif len(views) > len(accessories):
        print(f"（另有 {len(views) - len(accessories)} 条武器/防具/绘卷等记录，"
              f"其词条不在饰品词条库内，未列出）")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """Report what the tool can see in a save, for diagnosis."""
    crypto = _crypto(args)
    save = _select_save(args)
    data = open_save(save, crypto)
    diagnosis = records.layout_diagnosis(data, preview_records=args.preview)
    payload = {
        "path": str(save.path),
        "account_id": save.account_id,
        "slot_index": save.slot_index,
        "checksum_consistent": save_checksum_is_valid(data),
        "magic": data[:6].decode("ascii", "replace"),
        **diagnosis,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"存档      : {save.display}")
    print(f"路径      : {save.path}")
    print(f"校验和一致: {'是' if payload['checksum_consistent'] else '否'}")
    print(f"版本标识  : {version.version_info().commit}")
    print()
    for line in records.describe_diagnosis(diagnosis):
        print(line)
    print()
    print("提示：本诊断只读取存档文件，游戏无需运行。")
    print("若结果不符预期，请把以上输出（可用 scan --json）反馈给作者。")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    crypto = _crypto(args)
    save = _select_save(args)
    data = open_save(save, crypto)
    affix_db = AffixDb()
    known_ids = accessory_catalog_ids(affix_db)
    views = list_accessories(data, known_ids=known_ids)
    accessories = [view for view in views if view.is_accessory]
    print(json.dumps({
        "path": str(save.path),
        "account_id": save.account_id,
        "slot_index": save.slot_index,
        "size": len(data),
        "magic": data[:6].decode("ascii", "replace"),
        "checksum_consistent": save_checksum_is_valid(data),
        "item_records": len(views),
        "accessory_records": len(accessories),
        "other_records": len(views) - len(accessories),
    }, ensure_ascii=False, indent=2))
    return 0


def _parse_edit_spec(spec: str, affix_db: AffixDb | None = None,
                     *, what: str = "词条") -> dict[str, int]:
    """Parse 'slot:词条或id[:value[:metadata]]' into an effect-slot edit.

    The second field may be a **keyword** (需求 4): ``3:火抗性`` resolves through
    :func:`_resolve_keyword`, which refuses ambiguity by listing every match
    instead of silently picking the first one.  The target record index is
    supplied separately via ``--record``.
    """
    parts = spec.split(":")
    if len(parts) < 2:
        raise EditorError("编辑格式应为 slot:词条名称或id[:value[:metadata]]")
    try:
        slot = int(parts[0], 0)
    except ValueError as error:
        raise EditorError(f"非法编辑参数（槽位必须是数字）: {spec!r}") from error
    target = parts[1].strip()
    effect_id: int | None = None
    if target:
        try:
            effect_id = int(target, 0)
        except ValueError:
            effect_id = None
        # A *text* keyword (需求 4) resolves through the catalog; a number is taken
        # literally, so an id outside the table still gets the catalog's own
        # refusal message instead of a "no keyword match" one.
        if effect_id is None:
            if affix_db is None:
                raise EditorError(f"非法编辑参数（词条必须是 id）: {spec!r}")
            effect_id = _resolve_keyword(affix_db, target, what=what).effect_id
    else:
        effect_id = records.EMPTY_EFFECT_ID
    edit: dict[str, int] = {"slot_index": slot, "effect_id": effect_id}
    try:
        if len(parts) >= 3:
            edit["value"] = int(parts[2], 0)
        if len(parts) >= 4:
            edit["metadata"] = int(parts[3], 0)
        if len(parts) > 4:
            raise EditorError(f"编辑参数过多: {spec!r}")
    except ValueError as error:
        raise EditorError(f"非法编辑参数（数值/标识必须是数字）: {spec!r}") from error
    return edit


def _title_screen_confirmed(args: argparse.Namespace) -> bool:
    """True when the operator confirmed the game sits on its title screen.

    ``--at-title-screen`` is the explicit spelling; ``--force-while-running`` is
    the older name for the same acknowledgement and keeps working.
    """
    return bool(getattr(args, "at_title_screen", False)
                or getattr(args, "force_while_running", False))


def cmd_edit(args: argparse.Namespace) -> int:
    affix_db = AffixDb()
    grace_db = GraceDb.best_effort()
    item_db = ItemDb.best_effort()
    if args.soul:
        # 魂核 use their own affix pool and have no 恩宠/套装 affix, so the whole
        # edit path switches catalogs; the 恩宠 option is meaningless there.
        affix_db = AffixDb(load_soul_catalog())
        item_db = ItemDb(load_soul_item_catalog())
        if args.grace:
            raise EditorError("魂核没有恩宠/套装词条，--grace 不适用于 --soul")
    crypto = _crypto(args)
    save = _select_save(args)
    if args.record < 0:
        raise EditorError("--record 必须指定一个非负的记录索引")
    grace_id = None
    if args.grace:
        grace_id = resolve_grace_id(grace_db, args.grace)
    chosen = sum(1 for flag in (bool(args.edit), grace_id is not None,
                                args.level is not None,
                                args.plus is not None,
                                args.kind is not None,
                                args.search is not None) if flag)
    if chosen != 1:
        raise EditorError("六选一：--edit 改词条，--grace 改恩宠，--level 改等级，"
                          "--plus 改 +值，--kind 改种类，--search 只搜索词条不改存档"
                          "（不能同时使用，也不能都不给）")
    edits = tuple(
        {"record_index": args.record,
         **_parse_edit_spec(spec, affix_db,
                            what="魂核词条" if args.soul else "词条")}
        for spec in (args.edit or ())
    )

    print(DISCLAIMER)
    print(f"存档: {save.display}")
    data = open_save(save, crypto)
    known_ids = (soul_catalog_ids(affix_db) if args.soul
                 else accessory_catalog_ids(affix_db))
    layout = records.locate_layout(data, known_ids=known_ids)
    if args.search is not None:
        # 需求(4): 只搜索、不改存档；把全部匹配列出来供选择。
        matches = affix_db.search(args.search)
        what = "魂核词条" if args.soul else "饰品词条"
        print(f"\n关键词「{args.search}」匹配到 {len(matches)} 条{what}：")
        if not matches:
            print("  （没有匹配结果）请换更短的关键词，或直接写词条 id。")
            return 0
        for entry in matches[:60]:
            extra = "（同名固定，不能手动写入）" if entry.is_fixed else ""
            print(f"  {entry.label}\n      数值区间 {entry.describe_value_range()} "
                  f"{extra}")
        if len(matches) > 60:
            print(f"  …另有 {len(matches) - 60} 条，请写得更具体。")
        print("\n用法：edit --record N --edit 槽位:名称或id[:数值]")
        return 0
    if args.soul:
        cores = {core.slot_index: core
                 for core in list_soul_cores(data, soul_db=affix_db,
                                             soul_item_db=item_db,
                                             layout=layout, known_ids=known_ids)}
        current = cores.get(args.record)
        if current is None:
            raise EditorError(f"记录 #{args.record} 不在当前存档的魂核记录中"
                              "（魂核按「种类表 + 魂核词条库」双重证据识别）")
        print(f"目标魂核: {current.describe_item(item_db)} Lv{current.level}")
    if args.level is not None:
        plan = plan_level_edit(data, args.record, args.level,
                                affix_db=affix_db, known_ids=known_ids,
                                layout=layout)
        print(f"等级: {plan.describe()}")
        print("注意：只写入等级字段（+0x06/+0x08）与校验和；"
              "存档里同种饰品的词条数值不随等级变化（已实测），"
              "但游戏是否会在读取后按等级重算显示数值无法由存档证明，"
              "请谨慎使用并进游戏确认。")
        patched = apply_level_edits(data, [plan])
    elif args.plus is not None:
        plan = plan_plus_edit(data, args.record, args.plus,
                              affix_db=affix_db, known_ids=known_ids,
                              layout=layout)
        print(f"+值: {plan.describe()}")
        print("注意：只写入 +值 字段（+0x0A）与校验和。"
              "该字段已由游戏内实测确认（存档字节与物品卡显示的 +13/+18/+19 一致），"
              "改完请进游戏确认显示。")
        patched = apply_plus_edits(data, [plan])
    elif args.kind is not None:
        target = resolve_item_id(item_db, args.kind)
        plan = plan_kind_swap(data, args.record, target, affix_db=affix_db,
                              item_db=item_db, known_ids=known_ids,
                              layout=layout)
        print(f"种类: {plan.describe()}")
        for slot_index, effect_id, value, byte9 in plan.fixed_after:
            print(f"  固定槽[{slot_index}] ← {affix_db.describe(effect_id)}"
                  f" (id={effect_id:#06x} 数值={value} 标识第9字节={byte9:#04x})")
        print("注意：普通词条与末位恩宠/套装槽保持原样，"
              "只有种类与固定词条被改写；固定词条是从本存档里同种类"
              "真实样本复制的，不是编造的。改完请进游戏确认。")
        patched = apply_kind_swaps(data, [plan])
    elif grace_id is not None:
        views = {view.slot_index: view
                 for view in list_accessories(data, layout=layout,
                                              known_ids=known_ids)}
        view = views.get(args.record)
        if view is None:
            raise GraceEditError(f"记录 #{args.record} 不在当前存档的饰品记录中")
        availability = grace_edit_availability(view, grace_db=grace_db,
                                               affix_db=affix_db)
        print(f"末位槽: {availability.describe_current()}"
              f"（{availability.kind or '未分类'}）")
        if not availability.allowed:
            raise GraceEditError(availability.reason)
        patched = apply_grace_edit(
            data, args.record, grace_id, affix_db=affix_db, grace_db=grace_db,
            known_ids=known_ids, layout=layout,
        )
    else:
        if args.soul:
            patched = apply_soul_edits(data, edits, soul_db=affix_db,
                                       soul_item_db=item_db,
                                       known_ids=known_ids, layout=layout)
        else:
            patched = apply_edits(data, edits, affix_db=affix_db, grace_db=grace_db,
                                  known_ids=known_ids, layout=layout)

    if args.soul:
        for core in list_soul_cores(patched, soul_db=affix_db,
                                     soul_item_db=item_db, layout=layout,
                                     known_ids=known_ids):
            if core.slot_index != args.record:
                continue
            print(f"修改后记录 #{core.slot_index}:")
            for line in core.describe_effects(affix_db, item_db):
                print(line)
    else:
        for view in list_accessories(patched, layout=layout, known_ids=known_ids):
            if view.slot_index != args.record:
                continue
            print(f"修改后记录 #{view.slot_index}:")
            for line in view.describe_effects(affix_db, grace_db, item_db):
                print(line)

    # State the requirement before touching the file, and say whether the gate
    # is currently satisfied so the user is never surprised by a refusal.
    print("\n" + SAVE_WRITE_REQUIREMENT)
    running = running_game_processes()
    if running:
        print(f"当前状态：检测到 {'、'.join(running)} 正在运行——"
              f"{'已确认停在标题界面（--at-title-screen）' if _title_screen_confirmed(args) else '将被拒绝'}。")
    else:
        print("当前状态：未检测到游戏进程。")
    if args.dry_run:
        print("本次为演练模式（--dry-run），不会写入存档。")
        print("提示：真正写入时请保持游戏处于已退出或标题界面状态。")

    result = commit_save(
        save,
        patched,
        crypto=crypto,
        state_root=_state_root(args),
        dry_run=args.dry_run,
        verify=not args.no_verify,
        allow_game_running=_title_screen_confirmed(args),
    )
    print("\n" + json.dumps(result, ensure_ascii=False, indent=2))
    if not result["dry_run"]:
        print("\n完成。请在游戏中重新加载该存档以查看效果"
              "（加载前请勿在游戏内保存，否则会覆盖本次修改）。")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    crypto = _crypto(args)
    save = _select_save(args)
    backup_dir = create_backup(save.path, state_root=_state_root(args), crypto=crypto)
    print(f"已备份到: {backup_dir}")
    print(f"内容    : 解密后的明文副本（SAVEDATA-*-plain.bin）与 backup-manifest.json")
    print(f"恢复    : {PROG} restore --list 查看历史备份，"
          f"{PROG} restore --from latest 可还原")
    return 0


def _format_backup(index: int, entry: BackupEntry) -> str:
    marker = "" if entry.integrity_ok else "  [校验不符，已拒绝用于恢复]"
    return f"  [{index}] {entry.when}  {entry.plain_size:#x} 字节  {entry.plain_path.name}{marker}"


def cmd_restore(args: argparse.Namespace) -> int:
    """List or restore plaintext backups for the selected save."""
    crypto = _crypto(args)
    save = _select_save(args)
    state_root = _state_root(args)
    entries = list_backups(save.path, state_root)
    directory = backup_directory_for(save.path, state_root)

    if args.list or args.from_ is None:
        print(f"存档      : {save.display}")
        print(f"备份目录  : {directory}")
        if not entries:
            print("未发现备份。写入存档前会自动备份，也可以先用 backup 子命令手动备份。")
            print("提示：备份目录可用 config/editor.json 的 backup_root 指定。")
            return 0
        print(f"共 {len(entries)} 个备份（新→旧）：")
        for index, entry in enumerate(entries):
            print(_format_backup(index, entry))
        if args.list:
            print(f"\n恢复指定备份：{PROG} restore --from <序号|latest|路径>")
            return 0
        if args.from_ is None and not args.list:
            print(f"\n未指定 --from，仅列出备份（未做任何修改）。")
            return 0

    entry = _pick_backup(args.from_, entries, directory)
    print(f"存档      : {save.display}")
    print(f"恢复来源  : {entry.plain_path}")
    print(f"备份时间  : {entry.when}")
    print(f"备份 SHA-256: {entry.plain_sha256}")
    if entry.account_id is not None or entry.slot_index is not None:
        print(f"备份来源  : 账号 {entry.account_id} / 栏位 "
              f"{'--' if entry.slot_index is None else format(entry.slot_index, '02d')}")
    if entry.slot_index is not None and entry.slot_index != save.slot_index:
        print("⚠ 该备份来自其它栏位——请确认这是你想要的内容。")

    print("\n" + SAVE_WRITE_REQUIREMENT)
    running = running_game_processes()
    if running:
        print(f"当前状态：检测到 {'、'.join(running)} 正在运行——"
              f"{'已确认停在标题界面（--at-title-screen）' if _title_screen_confirmed(args) else '将被拒绝'}。")
    else:
        print("当前状态：未检测到游戏进程。")
    if args.dry_run:
        print("本次为演练模式（--dry-run），不会写入存档。")
    else:
        print("\n" + DISCLAIMER)

    result = restore_backup(
        save,
        entry,
        crypto=crypto,
        state_root=state_root,
        dry_run=args.dry_run,
        verify=not args.no_verify,
        allow_game_running=_title_screen_confirmed(args),
    )
    print("\n" + json.dumps(result, ensure_ascii=False, indent=2))
    if not result["dry_run"]:
        print(f"\n已还原。还原前的存档已另行备份到：\n{result['safety_backup_dir']}")
        matches = result.get("matches_original_save")
        if matches is True:
            print("校验：还原后的文件与备份时记录的原文件 SHA-256 完全一致。")
        elif matches is False:
            source = result.get("tail_source")
            if source == "on-disk":
                print("说明：内容已按备份还原；文件 SHA-256 与备份时不同，"
                      "仅因存档末尾 8 字节不在备份范围内（参考解密工具会将其清零），"
                      "已沿用当前文件的值。")
            else:
                print("注意：还原后的 SHA-256 与备份时记录的原文件不一致。")
        for problem in result.get("source_mismatch") or ():
            print(f"⚠ {problem}")
        print("请在游戏中加载该存档确认。")
    return 0


def _pick_backup(value: str, entries: tuple[BackupEntry, ...],
                 directory: Path) -> BackupEntry:
    """Resolve ``--from``: latest | index | a path to a *-plain.bin file."""
    if not entries and value != "latest":
        raise EditorError(f"备份目录中没有可用备份：{directory}")
    if value == "latest":
        if not entries:
            raise EditorError(f"备份目录中没有可用备份：{directory}")
        return entries[0]
    if value.isdigit():
        index = int(value)
        if not 0 <= index < len(entries):
            raise EditorError(f"备份序号 {index} 超出范围（共 {len(entries)} 个）")
        return entries[index]
    path = Path(value).expanduser()
    if not path.is_file():
        raise EditorError(f"找不到备份文件：{path}")
    for entry in entries:
        if entry.plain_path == path:
            return entry
    # A file outside the backup directory: hash it and let the reader validate.
    return BackupEntry(
        plain_path=path,
        created_at=path.stem,
        plain_size=path.stat().st_size,
        plain_sha256="",
    )


class _VersionAction(argparse.Action):
    """Print the version banner verbatim.

    ``argparse``'s built-in ``version`` action routes the text through the help
    formatter, which re-wraps it into a paragraph and destroys the line breaks
    of the multi-line banner.  Printing directly keeps the four facts readable.
    """

    def __init__(self, option_strings, dest, **kwargs) -> None:
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None) -> None:
        print(version_banner())
        parser.exit(0)


def cmd_config(args: argparse.Namespace) -> int:
    """Show the effective configuration, or create the default file."""
    config = _config(args)
    if args.init:
        target = Path(args.init) if args.init is not True else None
        try:
            written = write_default_config(
                target, version=version_info().version,
                commit=version_info().commit, overwrite=args.force,
            )
        except ConfigError as error:
            raise EditorError(str(error)) from error
        print(f"已写入默认配置文件: {written}")
        print("按需修改后重启程序生效；也可用 --config <路径> 指定其它文件。")
        return 0

    if args.json:
        payload = config.to_dict()
        payload["_source"] = str(config.source) if config.source else None
        payload["_config_path"] = str(paths.default_config_path())
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print("Nioh3AccessoryEditor 配置")
    for line in config.describe():
        print(line)
    print(f"默认路径  : {paths.default_config_path()}"
          f"{'' if paths.default_config_path().is_file() else '（尚未创建，可用 config --init 生成）'}")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    """Print the build/version block (also available as ``--version``)."""
    info = version_info()
    print(version_banner(info))
    if args.json:
        print("\n" + json.dumps(info.as_dict(), ensure_ascii=False, indent=2))
    crypto = _crypto(args)
    if crypto.executable is None:
        print("\n当前加密后端: 纯 Python（内置实现）")
    else:
        print(f"\n当前加密后端: 外部 exe ({crypto.executable})")
    print(f"附属文件目录: {paths.resource_root()}")
    print(f"配置文件    : {paths.default_config_path()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="launch_editor.py",
        description="仁王3 饰品词条修改器（仅供测试学习用）",
    )
    parser.add_argument("--version", action=_VersionAction,
                        help="显示版本信息（commit 后8位、来源、构建时间、语言）")
    parser.add_argument("--python-crypto", action="store_true",
                        help="使用纯 Python 加解密后端（较慢，无需外部 exe）")
    parser.add_argument("--config", default=None,
                        help="指定参数配置文件（默认为程序目录下的 config/editor.json）")
    parser.add_argument("--save-index", type=int, default=None,
                        help="选择存档栏位（默认第一个；需写在子命令之前）")
    parser.add_argument("--account", type=int, default=None,
                        help="选择 Steam 账号 ID（默认第一个；需写在子命令之前）")
    sub = parser.add_subparsers(dest="command", required=True)

    parser_list = sub.add_parser("list", help="列出存档中的饰品记录与词条")
    parser_list.add_argument("--kind", default=None,
                             help="只列出该种类（名称或 id，可只写一部分，如 八尺琼）")
    parser_list.add_argument("--grace", default=None,
                             help="只列出带该恩宠/套装的饰品（名称或 id 的一部分）")
    parser_list.set_defaults(func=cmd_list)

    parser_scan = sub.add_parser(
        "scan", help="诊断存档的物品记录表（只读，游戏无需运行）")
    parser_scan.add_argument("--json", action="store_true", help="输出 JSON 格式诊断")
    parser_scan.add_argument("--preview", type=int, default=4,
                             help="附带几条记录的效果槽原始字节（默认 4，0 为不附带）")
    parser_scan.set_defaults(func=cmd_scan)

    parser_check = sub.add_parser("check", help="只读检查存档完整性与饰品数量")
    parser_check.set_defaults(func=cmd_check)

    parser_souls = sub.add_parser(
        "souls", help="列出存档中的魂核记录与词条（只读，魂核无恩宠/套装）")
    parser_souls.set_defaults(func=cmd_souls)

    parser_edit = sub.add_parser("edit", help="修改饰品词条或恩宠并写回存档")
    parser_edit.add_argument("--soul", action="store_true",
                             help="改为编辑魂核（用魂核词条库，魂核没有恩宠/套装词条）")
    parser_edit.add_argument("--record", type=int, default=-1,
                             help="目标饰品记录索引（list 输出中的 #N）")
    parser_edit.add_argument("--edit", action="append",
                             help="编辑项 slot:effect_id[:value[:metadata]]，可多次指定")
    parser_edit.add_argument("--grace", default=None,
                             help="把末位槽的恩宠改成另一个恩宠：id（0x4fa3）"
                                  "或名称（稻荷神）；套装/专属套装词条一律拒绝")
    parser_edit.add_argument("--level", type=int, default=None,
                             help=f"改等级（合法范围 "
                                  f"{records.MIN_ITEM_LEVEL}..{records.MAX_ITEM_LEVEL}，"
                                  f"{records.MAX_ITEM_LEVEL} 是游戏上限）；"
                                  "只写等级字段，出厂词条数值不变")
    parser_edit.add_argument("--plus", type=int, default=None, metavar="N",
                             help=f"改 +值（0..{records.MAX_RECORD_PLUS}）；"
                                  "该字段已由游戏内实测确认（+0x0A），只写这一个字段")
    parser_edit.add_argument("--kind", default=None,
                             help="改种类：目标必须是同分类（武士饰品/忍者饰品）"
                                  "且在存档里已有实例的饰品种类，"
                                  "固定词条会按该种类的真实样本自动同步")
    parser_edit.add_argument("--search", default=None, metavar="关键词",
                             help="只按关键词搜索合法词条并列出全部匹配结果"
                                  "（不修改存档；用于先看有哪些候选再决定）")
    parser_edit.add_argument("--dry-run", action="store_true", help="仅演练，不写回")
    parser_edit.add_argument("--no-verify", action="store_true",
                             help="跳过写入前后的解密校验（更快，但风险更高）")
    parser_edit.add_argument("--at-title-screen", action="store_true",
                             help="确认游戏正停在标题界面（未载入存档）时也允许写入；"
                                  "等价于 GUI 里勾选那个确认框")
    parser_edit.add_argument("--force-while-running", action="store_true",
                             help="--at-title-screen 的旧名，效果相同（不推荐，"
                                  "请优先用 --at-title-screen 表达你确认的内容）")
    parser_edit.set_defaults(func=cmd_edit)

    parser_create = sub.add_parser(
        "create", help="无中生有：新建一件饰品/魂核放进存档空槽（背包满则拒绝）")
    parser_create.add_argument("--kind", required=True,
                               help="要新建的种类（名称或 id，如 八尺琼勾玉[武士]）")
    parser_create.add_argument("--level", type=int, default=records.MAX_ITEM_LEVEL,
                               help=f"等级（{records.MIN_ITEM_LEVEL}.."
                                    f"{records.MAX_ITEM_LEVEL}，默认 "
                                    f"{records.MAX_ITEM_LEVEL}）")
    parser_create.add_argument("--effect", action="append", default=[],
                               help="词条 slot:名称或id[:数值]，可多次指定"
                                    "（固定词条槽由模板自动带入，不能指定）")
    parser_create.add_argument("--slot", type=int, default=None,
                               help="指定写入哪个空槽（默认第一个空位）")
    parser_create.add_argument("--soul", action="store_true",
                               help="新建魂核（用魂核词条库与魂核种类表）")
    parser_create.add_argument("--dry-run", action="store_true", help="仅演练，不写回")
    parser_create.add_argument("--no-verify", action="store_true",
                               help="跳过写入前后的解密校验（更快，但风险更高）")
    parser_create.add_argument("--at-title-screen", action="store_true",
                               help="确认游戏正停在标题界面（未载入存档）时也允许写入")
    parser_create.add_argument("--force-while-running", action="store_true",
                               help="--at-title-screen 的旧名，效果相同")
    parser_create.set_defaults(func=cmd_create)

    parser_backup = sub.add_parser("backup", help="备份存档（解密明文副本）")
    parser_backup.set_defaults(func=cmd_backup)

    parser_restore = sub.add_parser(
        "restore", help="列出备份并还原（备份是解密明文，会重新加密写回）")
    parser_restore.add_argument("--list", action="store_true",
                                help="只列出该存档的历史备份")
    parser_restore.add_argument("--from", dest="from_", default=None,
                                metavar="序号|latest|路径",
                                help="还原哪一个备份（默认只列出，不还原）")
    parser_restore.add_argument("--dry-run", action="store_true",
                                help="仅演练：检查并校验，不写入")
    parser_restore.add_argument("--no-verify", action="store_true",
                                help="跳过写入前后的解密校验（更快，但风险更高）")
    parser_restore.add_argument("--at-title-screen", action="store_true",
                                help="确认游戏正停在标题界面（未载入存档）时也允许恢复")
    parser_restore.add_argument("--force-while-running", action="store_true",
                                help="--at-title-screen 的旧名，效果相同")
    parser_restore.set_defaults(func=cmd_restore)

    parser_version = sub.add_parser("version", help="显示版本与构建信息")
    parser_version.add_argument("--json", action="store_true",
                                help="同时输出 JSON 格式的版本信息")
    parser_version.set_defaults(func=cmd_version)

    parser_config = sub.add_parser("config", help="查看或生成参数配置文件")
    parser_config.add_argument("--json", action="store_true", help="以 JSON 输出有效配置")
    parser_config.add_argument("--init", nargs="?", const=True, default=None,
                               metavar="路径",
                               help="写入默认配置文件（可指定路径）")
    parser_config.add_argument("--force", action="store_true",
                               help="与 --init 一起使用时覆盖已存在的文件")
    parser_config.set_defaults(func=cmd_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Frozen builds unpack their side-by-side files (config/, data/, bin/,
    # third_party/) next to the executable before anything reads them.
    ensure_once()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (EditorError, SaveError, AffixError, RecordError, ValueError) as error:
        print(f"错误: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130
    except OSError as error:
        print(f"系统错误: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
