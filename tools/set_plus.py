"""Set the +値 (``+0x0A``) of one accessory — a one-field writer.

``edit --plus N`` is the normal way to do this; this script stays as the
no-subcommand version of the same edit.


    python tools/set_plus.py --record 28 --value 0            # dry-run
    python tools/set_plus.py --record 28 --value 0 --write    # really write

Why this exists: ``+0x0A`` is a per-instance word in the record header (0..30
across the 213 accessories of the reporting save, independent of level, rarity,
fixed slots and stored affix values) whose meaning nobody has verified.  Changing
exactly this word and looking at the item in game is the only way to find out
what it is.  Nothing else in the record is touched: no affix, no level, no mirror.

Always run it on a *copy* of your save first, and back up before writing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from nioh3_accessory_editor import records, savefile  # noqa: E402
from nioh3_accessory_editor.affixdb import AffixDb, ItemDb  # noqa: E402
from nioh3_accessory_editor.cli import (  # noqa: E402
    DISCLAIMER,
    SAVE_WRITE_REQUIREMENT,
    _crypto,
    _select_save,
    _state_root,
)
from nioh3_accessory_editor.editor import (  # noqa: E402
    EditorError,
    accessory_catalog_ids,
    apply_plus_edits,
    commit_save,
    list_accessories,
    open_save,
    plan_plus_edit,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把某个饰品的 +0x0A（含义未核实）改成指定值，用于进游戏对照试验",
    )
    parser.add_argument("--config", default=None, help="参数文件（同主程序）")
    parser.add_argument("--save-index", type=int, default=None)
    parser.add_argument("--account", type=int, default=None)
    parser.add_argument("--record", type=int, required=True,
                        help="目标记录索引（主程序 list 输出的 #N）")
    parser.add_argument("--value", type=int, required=True,
                        help=f"新的 +0x0A 值（0..{records.MAX_RECORD_PLUS}）")
    parser.add_argument("--write", action="store_true",
                        help="真正写入（缺省只演练；写入前会自动备份）")
    parser.add_argument("--at-title-screen", action="store_true",
                        help="确认游戏停在标题界面（未载入存档）时也允许写入")
    parser.add_argument("--force-while-running", action="store_true",
                        help="--at-title-screen 的旧名，效果相同")
    args = parser.parse_args(argv)

    db = AffixDb()
    items = ItemDb.best_effort()
    crypto = _crypto(args)
    save = _select_save(args)
    print(DISCLAIMER)
    print(f"存档: {save.display}")
    data = open_save(save, crypto)
    known = accessory_catalog_ids(db)
    layout = records.locate_layout(data, known_ids=known)
    views = {view.slot_index: view
             for view in list_accessories(data, layout=layout, known_ids=known)
             if view.is_accessory is not False}
    view = views.get(args.record)
    if view is None:
        raise EditorError(f"记录 #{args.record} 不在当前存档的饰品记录中")
    print(f"目标: #{view.slot_index} {items.describe(view.record_type) or '未知种类'} "
          f"Lv{view.level} {view.rarity_name}，当前 +值={view.plus_value}")

    plan = plan_plus_edit(data, args.record, args.value, affix_db=db,
                          known_ids=known, layout=layout)
    print(f"计划: {plan.describe()}")
    patched = apply_plus_edits(data, [plan])

    # Fail closed: only the two bytes of +0x0A may differ.
    changed = [index for index in range(len(data)) if data[index] != patched[index]]
    offset = layout.offset(args.record) + records.RECORD_PLUS_OFFSET
    if not changed or not set(changed) <= {offset, offset + 1}:
        raise EditorError(
            f"内部校验失败：改动字节 {changed} 超出 +0x0A 的两个字节 "
            f"[{offset}, {offset + 1}]，已中止（不会写入）"
        )
    print(f"差异校验通过：只有 +0x0A 的 {len(changed)} 个字节改变（{offset:#x} 起）")

    print("\n" + SAVE_WRITE_REQUIREMENT)
    running = savefile.running_game_processes()
    print("当前状态：" + (f"检测到 {'、'.join(running)} 正在运行。" if running
                          else "未检测到游戏进程。"))
    if not args.write:
        print("本次为演练（未加 --write），不会写入存档。")
        return 0
    result = commit_save(save, patched, crypto=crypto, state_root=_state_root(args),
                         dry_run=False, verify=True,
                         allow_game_running=bool(args.at_title_screen
                                                or args.force_while_running))
    print(f"已写入: {save.path}（备份 {result.get('backup_dir')}）")
    print("已改的是 +值（物品卡上的 +N）；请进游戏确认显示，"
          "并和同种类其它件对比。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
