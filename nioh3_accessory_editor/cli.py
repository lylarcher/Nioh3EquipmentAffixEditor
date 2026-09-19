"""Command-line interface for Nioh3AccessoryEditor.

Commands:
    list    Discover saves and list accessory-like records with their affixes.
    edit    Apply effect-slot edits to a selected save (persisted to disk).
    backup  Create a plaintext backup of a save.
    check   Decrypt a save and report integrity without modifying anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .affixdb import AffixDb, AffixError
from .editor import (
    EditorError,
    SaveDescriptor,
    apply_edits,
    commit_save,
    discover_saves,
    list_accessories,
    open_save,
    save_checksum_is_valid,
)
from .records import RecordError
from .savefile import (
    SaveCrypto,
    SaveError,
    create_backup,
    default_crypto_tool,
)

DISCLAIMER = (
    "本工具仅供测试学习用，请勿用于联机环境或影响游戏平衡。\n"
    "仁王3 为单机/纯 PVE 联机游戏，本工具不会影响其他玩家。\n"
    "使用前请备份存档；作者对存档损坏不承担任何责任。"
)


def _crypto(args: argparse.Namespace) -> SaveCrypto:
    """Build the crypto backend, degrading to pure Python when the exe is gone."""
    prefer_python = bool(getattr(args, "python_crypto", False))
    try:
        executable = None if prefer_python else default_crypto_tool(Path.cwd())
    except FileNotFoundError as error:
        if not prefer_python:
            print(f"提示：{error}\n      已自动切换到纯 Python 后端（较慢）。", file=sys.stderr)
        executable = None
        prefer_python = True
    return SaveCrypto(executable, prefer_python=prefer_python)


def _select_save(args: argparse.Namespace) -> SaveDescriptor:
    saves = discover_saves()
    if not saves:
        raise EditorError(
            "未发现 Nioh 3 存档。请确认游戏已运行过且存档位于 "
            "%LOCALAPPDATA%\\KoeiTecmo\\NIOH3\\Savedata"
        )
    account = getattr(args, "account", None)
    if account is not None:
        saves = tuple(save for save in saves if save.account_id == account)
        if not saves:
            raise EditorError(f"未找到账号 {account} 的存档")
    slot = getattr(args, "save_index", None)
    if slot is not None:
        saves = tuple(save for save in saves if save.slot_index == slot)
        if not saves:
            raise EditorError(f"未找到栏位 {slot} 的存档")
    if len(saves) > 1 and getattr(args, "save_index", None) is None:
        print(f"提示：发现 {len(saves)} 个候选存档，使用第一个；可用 --save-index/--account 选择。",
              file=sys.stderr)
    return saves[0]


def cmd_list(args: argparse.Namespace) -> int:
    affix_db = AffixDb()
    crypto = _crypto(args)
    save = _select_save(args)
    print(f"存档: {save.display}")
    print(f"路径: {save.path}")
    data = open_save(save, crypto)
    print(f"校验和一致: {'是' if save_checksum_is_valid(data) else '否（存档可能来自其他版本或被修改过）'}")
    accessories = list_accessories(data)
    print(f"\n找到 {len(accessories)} 条疑似饰品/装备记录\n")
    for view in accessories:
        print(
            f"记录 #{view.slot_index} @ {view.offset:#x}  "
            f"type={view.record_type:#06x} Lv{view.level} {view.rarity_name}"
        )
        for line in view.describe_effects(affix_db):
            print(line)
        print()
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    crypto = _crypto(args)
    save = _select_save(args)
    data = open_save(save, crypto)
    accessories = list_accessories(data)
    print(json.dumps({
        "path": str(save.path),
        "account_id": save.account_id,
        "slot_index": save.slot_index,
        "size": len(data),
        "magic": data[:6].decode("ascii", "replace"),
        "checksum_consistent": save_checksum_is_valid(data),
        "accessory_records": len(accessories),
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
    crypto = _crypto(args)
    save = _select_save(args)
    if args.record < 0:
        raise EditorError("--record 必须指定一个非负的记录索引")
    edits = tuple(
        {"record_index": args.record, **_parse_edit_spec(spec)}
        for spec in args.edit
    )
    if not edits:
        raise EditorError("请至少提供一个 --edit 参数")

    print(DISCLAIMER)
    print(f"存档: {save.display}")
    data = open_save(save, crypto)
    patched = apply_edits(data, edits, affix_db=affix_db)

    for view in list_accessories(patched):
        if view.slot_index != args.record:
            continue
        print(f"修改后记录 #{view.slot_index}:")
        for line in view.describe_effects(affix_db):
            print(line)

    result = commit_save(
        save,
        patched,
        crypto=crypto,
        state_root=Path.cwd(),
        dry_run=args.dry_run,
        verify=not args.no_verify,
        allow_game_running=args.force_while_running,
    )
    print("\n" + json.dumps(result, ensure_ascii=False, indent=2))
    if not result["dry_run"]:
        print("\n完成。请在游戏中重新加载该存档以查看效果。")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    crypto = _crypto(args)
    save = _select_save(args)
    backup_dir = create_backup(save.path, state_root=Path.cwd(), crypto=crypto)
    print(f"已备份到: {backup_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="launch_editor.py",
        description="仁王3 饰品词条修改器（仅供测试学习用）",
    )
    parser.add_argument("--python-crypto", action="store_true",
                        help="使用纯 Python 加解密后端（较慢，无需外部 exe）")
    parser.add_argument("--save-index", type=int, default=None,
                        help="选择存档栏位（默认第一个；需写在子命令之前）")
    parser.add_argument("--account", type=int, default=None,
                        help="选择 Steam 账号 ID（默认第一个；需写在子命令之前）")
    sub = parser.add_subparsers(dest="command", required=True)

    parser_list = sub.add_parser("list", help="列出存档中的饰品记录与词条")
    parser_list.set_defaults(func=cmd_list)

    parser_check = sub.add_parser("check", help="只读检查存档完整性与饰品数量")
    parser_check.set_defaults(func=cmd_check)

    parser_edit = sub.add_parser("edit", help="修改饰品词条并写回存档")
    parser_edit.add_argument("--record", type=int, default=-1,
                             help="目标饰品记录索引（list 输出中的 #N）")
    parser_edit.add_argument("--edit", action="append", required=True,
                             help="编辑项 slot:effect_id[:value[:metadata]]，可多次指定")
    parser_edit.add_argument("--dry-run", action="store_true", help="仅演练，不写回")
    parser_edit.add_argument("--no-verify", action="store_true",
                             help="跳过写入前后的解密校验（更快，但风险更高）")
    parser_edit.add_argument("--force-while-running", action="store_true",
                             help="即使检测到游戏正在运行也继续写入（不推荐）")
    parser_edit.set_defaults(func=cmd_edit)

    parser_backup = sub.add_parser("backup", help="备份存档（解密明文副本）")
    parser_backup.set_defaults(func=cmd_backup)
    return parser


def main(argv: list[str] | None = None) -> int:
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
