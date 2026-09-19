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
from .affixdb import AffixDb, AffixError, GraceDb
from .bootstrap import ensure_once
from .config import ConfigError, EditorConfig, load_config, write_default_config
from .editor import (
    EditorError,
    GraceEditError,
    SaveDescriptor,
    accessory_catalog_ids,
    apply_edits,
    apply_grace_edit,
    commit_save,
    discover_saves,
    grace_edit_availability,
    list_accessories,
    open_save,
    resolve_grace_id,
    restore_backup,
    save_checksum_is_valid,
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


def cmd_list(args: argparse.Namespace) -> int:
    affix_db = AffixDb()
    grace_db = GraceDb.best_effort()
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
    views = list_accessories(data, layout=layout, known_ids=known_ids)
    accessories = [view for view in views if view.is_accessory is not False]
    print(f"\n记录表内 {len(views)} 条物品记录，其中 {len(accessories)} 条含饰品词条")
    print(f"列出 {len(accessories)} 条饰品记录\n")
    for view in accessories:
        print(
            f"记录 #{view.slot_index} @ {view.offset:#x}  "
            f"type={view.record_type:#06x} Lv{view.level} {view.rarity_name}"
            f"  词条命中 {view.catalog_hits}"
        )
        for line in view.describe_effects(affix_db, grace_db):
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


def _parse_edit_spec(spec: str) -> dict[str, int]:
    """Parse 'slot:effect_id[:value[:metadata]]' into an effect-slot edit.

    The target record index is supplied separately via ``--record``.
    """
    parts = spec.split(":")
    if len(parts) < 2:
        raise EditorError("编辑格式应为 slot:effect_id[:value[:metadata]]")
    try:
        slot = int(parts[0], 0)
        effect_id = int(parts[1], 0)
        edit: dict[str, int] = {"slot_index": slot, "effect_id": effect_id}
        if len(parts) >= 3:
            edit["value"] = int(parts[2], 0)
        if len(parts) >= 4:
            edit["metadata"] = int(parts[3], 0)
        if len(parts) > 4:
            raise EditorError(f"编辑参数过多: {spec!r}")
    except ValueError as error:
        raise EditorError(f"非法编辑参数: {spec!r}") from error
    return edit


def cmd_edit(args: argparse.Namespace) -> int:
    affix_db = AffixDb()
    grace_db = GraceDb.best_effort()
    crypto = _crypto(args)
    save = _select_save(args)
    if args.record < 0:
        raise EditorError("--record 必须指定一个非负的记录索引")
    grace_id = None
    if args.grace:
        grace_id = resolve_grace_id(grace_db, args.grace)
    if bool(args.edit) == (grace_id is not None):
        raise EditorError("请二选一：--edit 改饰品词条，--grace 改恩宠"
                          "（两者不能同时使用，也不能都不给）")
    edits = tuple(
        {"record_index": args.record, **_parse_edit_spec(spec)}
        for spec in (args.edit or ())
    )

    print(DISCLAIMER)
    print(f"存档: {save.display}")
    data = open_save(save, crypto)
    known_ids = accessory_catalog_ids(affix_db)
    layout = records.locate_layout(data, known_ids=known_ids)
    if grace_id is not None:
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
        patched = apply_edits(data, edits, affix_db=affix_db,
                              known_ids=known_ids, layout=layout)

    for view in list_accessories(patched, layout=layout, known_ids=known_ids):
        if view.slot_index != args.record:
            continue
        print(f"修改后记录 #{view.slot_index}:")
        for line in view.describe_effects(affix_db, grace_db):
            print(line)

    # State the requirement before touching the file, and say whether the gate
    # is currently satisfied so the user is never surprised by a refusal.
    print("\n" + SAVE_WRITE_REQUIREMENT)
    running = running_game_processes()
    if running:
        print(f"当前状态：检测到 {'、'.join(running)} 正在运行——"
              f"{'已强制继续（--force-while-running）' if args.force_while_running else '将被拒绝'}。")
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
        allow_game_running=args.force_while_running,
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
              f"{'已强制继续（--force-while-running）' if args.force_while_running else '将被拒绝'}。")
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
        allow_game_running=args.force_while_running,
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
    parser_list.set_defaults(func=cmd_list)

    parser_scan = sub.add_parser(
        "scan", help="诊断存档的物品记录表（只读，游戏无需运行）")
    parser_scan.add_argument("--json", action="store_true", help="输出 JSON 格式诊断")
    parser_scan.add_argument("--preview", type=int, default=4,
                             help="附带几条记录的效果槽原始字节（默认 4，0 为不附带）")
    parser_scan.set_defaults(func=cmd_scan)

    parser_check = sub.add_parser("check", help="只读检查存档完整性与饰品数量")
    parser_check.set_defaults(func=cmd_check)

    parser_edit = sub.add_parser("edit", help="修改饰品词条或恩宠并写回存档")
    parser_edit.add_argument("--record", type=int, default=-1,
                             help="目标饰品记录索引（list 输出中的 #N）")
    parser_edit.add_argument("--edit", action="append",
                             help="编辑项 slot:effect_id[:value[:metadata]]，可多次指定")
    parser_edit.add_argument("--grace", default=None,
                             help="把末位槽的恩宠改成另一个恩宠：id（0x4fa3）"
                                  "或名称（稻荷神）；套装/专属套装词条一律拒绝")
    parser_edit.add_argument("--dry-run", action="store_true", help="仅演练，不写回")
    parser_edit.add_argument("--no-verify", action="store_true",
                             help="跳过写入前后的解密校验（更快，但风险更高）")
    parser_edit.add_argument("--force-while-running", action="store_true",
                             help="即使检测到游戏正在运行也继续写入（不推荐）")
    parser_edit.set_defaults(func=cmd_edit)

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
    parser_restore.add_argument("--force-while-running", action="store_true",
                                help="即使检测到游戏正在运行也继续写入（不推荐）")
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
