"""Tkinter GUI for Nioh 3 Equipment Affix Editor (zero third-party dependencies).

Workflow:
1. 刷新 -> discover saves, decrypt the selected one, scan accessory records.
2. Pick an accessory record in the tree, edit its 7 affix slots with searchable
   legal-affix dropdowns, click 应用修改, then 写入存档.
3. Every write: quiescence re-check, checksum recompute, plaintext backup,
   staged verification, atomic durable replacement, post-write verification.

The window shows the required disclaimer permanently.
"""

from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import equipmentdb, limits, paths
from .affixdb import (
    AffixDb,
    AffixError,
    GraceDb,
    ItemDb,
    load_soul_catalog,
)
from .bootstrap import ensure_once, last_report
from .config import ConfigError, EditorConfig, load_config
from .editor import (
    apply_equipment_grace_edit,
    grace_family_of,
    equipment_grace_availability,
    plan_equipment_grace_edit,
    AccessoryView,
    CreationError,
    EditorError,
    EquipmentView,
    GraceEditError,
    SaveDescriptor,
    SoulCoreView,
    accessory_catalog_ids,
    apply_creations,
    apply_equipment_creations,
    apply_equipment_edits,
    class_limits_for_record,
    equipment_affix_allowed,
    find_free_slots,
    plan_creation,
    apply_edits,
    apply_grace_edit,
    apply_kind_swaps,
    apply_level_edits,
    apply_plus_edits,
    apply_rarity_edits,
    apply_soul_edits,
    collect_kind_samples,
    commit_save,
    discover_saves,
    grace_edit_availability,
    inspect_layout,
    list_accessories,
    list_backups,
    list_equipment,
    list_soul_cores,
    open_save,
    plan_create_equipment,
    plan_equipment_edits,
    plan_kind_swap,
    plan_level_edit,
    plan_plus_edit,
    plan_rarity_edit,
    resolve_grace_id,
    restore_backup,
    save_checksum_is_valid,
    soul_catalog_ids,
)
from .paths import default_soul_items_path, resource_root
from .records import (
    EFFECT_COUNT,
    EMPTY_EFFECT_ID,
    MAX_ITEM_LEVEL,
    MAX_RECORD_PLUS,
    EffectSlot,
    InventoryLayout,
    RecordError,
    describe_diagnosis,
    layout_diagnosis,
)
from .savefile import (
    SAVE_WRITE_REQUIREMENT,
    BackupEntry,
    SaveCrypto,
    SaveError,
    backup_directory_for,
    create_backup,
    running_game_processes,
    save_root_directory,
)
from .version import UNKNOWN, version_banner, version_info

DISCLAIMER = (
    "仅供测试学习用，不要用于联机影响游戏平衡。\n"
    "仁王3 为单机/纯 PVE 联机游戏，本工具不会影响其他玩家。"
)

#: One-line form of savefile.SAVE_WRITE_REQUIREMENT for the always-visible
#: status area; the full wording goes into the write-confirmation dialog.
WRITE_REQUIREMENT_SHORT = (
    "写入存档前请先退出游戏，或退回到游戏标题界面；在游戏内写入会被游戏下次保存覆盖。"
)

TITLE = "仁王3 装备词条修改器（仅供测试学习用）"
SUBTITLE = ("武器 / 防具 / 饰品 / 魂核 词条 · 取自《仁王3词条装备库v2.21》"
            " · 修改结果直接写回存档")
EMPTY_LABEL = "(空)"
#: Sentinel for "no filter" in the 种类 / 恩宠 filter comboboxes (需求 3).
ALL_FILTER = "(全部)"

#: 稀有度数值 -> 游戏内颜色的完整对照（显示用；数值本身来自 limits 这唯一来源）。
RARITY_COLOR_HINT = " / ".join(
    f"{value} {name}" for value, name in sorted(limits.RARITY_COLOR_BY_VALUE.items())
)


def rarity_color_of(value: object) -> str:
    """稀有度数值的游戏内颜色名；未知值返回空串（fail-soft，不抛异常）。"""
    return limits.rarity_color_name(value)


def rarity_label(rarity: int, name: str) -> str:
    """列表「品质」列的文字：保留既有游戏稀有度名，后面追加颜色名（「神器（绿色）」）。

    颜色只做显示，**绝不替换**原来的名字（既有断言看的就是这个名字）；颜色查不到时
    原样返回名字，不显示括号。
    """
    color = rarity_color_of(rarity)
    return f"{name}（{color}）" if color else name

#: Game-process state line.  Writing needs the game closed *or* an explicit
#: acknowledgement that it sits on its title screen, so the state is shown
#: continuously instead of only when a write is refused.
GAME_STATUS_UNKNOWN = "游戏状态：检查中…"
#: 「还没读数」与「读了但没选」必须分开说：合并成一句时，在武器/防具页签点
#: 全局【应用修改】（旧行为只认饰品选中项）会得到一句完全误导的提示。
MSG_NEED_DATA = "还没有读取数据，请先点【读取数据】"
MSG_NEED_SELECTION = "还没有选中记录，请在左侧列表里点一行"


def affix_value_editable(entry) -> bool:
    """这条词条是否允许多个取值 —— 决定「数值」框能不能改（用户规则）。

    只有唯一合法取值的词条（``value_min == value_max``，或取值集合只有一个元素）
    数值框只显示、不可编辑；换词条后立刻按新词条重算。区间未知（原始表没给出）
    时同样按不可编辑处理：引擎也只接受它自己的目录值，让用户白打一遍没有意义。

    表外词条（``None``）保持**可编辑**，这是既有口径：这类槽要先在同一个槽里换成
    表内词条，才谈得上改数值，界面不额外加一层禁用（既有测试钉住了这一点）。
    """
    if entry is None:
        return True
    values = getattr(entry, "values", None)
    if values:
        return len(set(values)) > 1
    if not entry.has_value_range:
        return False
    return entry.value_min != entry.value_max


#: 空槽位不可写时写在槽位标签上的理由（开关见 ``limits.EMPTY_SLOT_EDITABLE``）。
EMPTY_SLOT_HINT = "空槽位在当前周目不可修改（四周目 / DLC2 开放后可能启用）"


def apply_empty_slot_state(combo, label_var, value_entry, value_var) -> bool:
    """把"空槽位"这一格刷成当前周目该有的样子，返回它还能不能写。

    用户口径：空槽位（既没有词条、也没有值）在当前最高周目（三周目）下**不可修改**；
    四周目 / DLC2 开放后可能启用。界面这里只是把它置灰并写明理由 —— **真正的闸门在
    引擎**（``editor._assert_no_empty_slot_writes``），界面漏判也不会写进去。

    "清空一个已有词条的槽"不算修改空槽，那条路径不经过这里。
    """
    value_var.set("")
    if limits.EMPTY_SLOT_EDITABLE:
        combo.state(["!disabled"])
        value_entry.state(["!disabled"])
        label_var.set("")
        return True
    combo.state(["disabled"])
    value_entry.state(["disabled"])
    label_var.set(EMPTY_SLOT_HINT)
    return False


GAME_STATUS_CLOSED = "游戏状态：未检测到仁王3 进程 —— 可以写入存档"
GAME_STATUS_RUNNING = (
    "游戏状态：{names} 正在运行 —— 写入会被拒绝；"
    "若游戏确实停在标题界面，请勾选窗口底部的确认框再写入"
)
#: How often the process list is re-checked (ms).  The check spawns tasklist, so
#: it runs on the worker thread and never blocks the window.
GAME_STATUS_INTERVAL_MS = 3000


class UiEditError(ValueError):
    """Raised when the current widget selection cannot be turned into an edit."""


def _load_image(path: Path, master: tk.Misc | None = None) -> tk.PhotoImage | None:
    """Load a PNG for the GUI, returning ``None`` when it is unavailable.

    The assets are generated by ``tools/make_icon.py`` and shipped next to the
    executable; a user who deletes them still gets a working window, just
    without the branding.  ``master`` must be passed when more than one Tk
    interpreter exists (a ``PhotoImage`` belongs to exactly one), which is why
    the application always passes itself.
    """
    try:
        if not path.is_file():
            return None
        return tk.PhotoImage(master=master, file=str(path))
    except Exception:  # noqa: BLE001 - cosmetic only, never break the window
        return None


#: 武器 / 防具两个页签：(大类, 页签标题)。
EQUIPMENT_TAB_KINDS = (("武器", "武器"), ("防具", "防具"))

#: 四个筛选轴在界面上的名字（顺序就是从左到右的排列顺序）。
EQUIPMENT_FILTER_AXES = ("small", "kind", "grace", "school")
EQUIPMENT_FILTER_LABELS = {
    "small": "类型",
    "kind": "种类",
    "grace": "恩宠/套装",
    "school": "武士/忍者",
}


class _quiet_dialogs:
    """批量应用期间把每个字段自己的确认/提示框静音（统一的一次确认已经问过）。"""

    def __enter__(self) -> None:
        self._saved = (messagebox.askokcancel, messagebox.showinfo,
                       messagebox.showwarning)
        messagebox.askokcancel = lambda *a, **k: True
        messagebox.showinfo = lambda *a, **k: None
        # 批量应用时某一字段"没有改动"不该再弹一次（统一确认里已经列过）。
        messagebox.showwarning = lambda *a, **k: None

    def __exit__(self, *exc: object) -> bool:
        (messagebox.askokcancel, messagebox.showinfo,
         messagebox.showwarning) = self._saved
        return False


class EquipmentTab(ttk.Frame):
    """武器 / 防具页签（同一个组件实例化两次，只有 ``big`` 不同）。

    与饰品页签同构：左边记录列表 + 四轴级联筛选，右边词条槽 / 等级 / +值 / 稀有度 /
    无中生有。差异只在**规则来源**：

    * 候选词条来自这件装备自己的词条池（远程武器 = 远程表，其余武器 = 近战表，
      防具 = 防具表），并且只列出「装备种类」标签相容的词条 —— 判定直接调用引擎的
      :func:`editor.equipment_affix_allowed`，界面里不重写一套规则；
    * 固定词条（目录标着同名固定，或存档标识带 0x4000）只读；恩宠/套装不在这排槽里改，
  用右下角的【恩宠 / 套装】栏替换；
    * 等级 / +值 / 稀有度 的上限取自 :func:`editor.class_limits_for_record` 与
      ``limits``（等级 180、+值按大类、稀有度按大类），不再写死。

    写入路径与其它页签共用：改动先落在内存里的 ``app.decrypted``，
    最后由窗口底部的「写入存档」写回副本。
    """

    def __init__(self, master: tk.Widget, app: "AccessoryEditorApp", *,
                 big: str, title: str) -> None:
        super().__init__(master, padding=(2, 2))
        self.app = app
        self.big = big
        self.title = title
        self.item_db = app.equipment_item_db
        #: 本大类的物品种类（标签 -> 条目），筛选与「无中生有」都用它。
        self.kind_choices = {
            entry.label: entry for entry in self.item_db.all() if entry.big == big
        }
        self.views: list[EquipmentView] = []
        self.selected: int | None = None
        self._pool = None
        self._pool_error = ""
        self._candidates: tuple[str, ...] = (EMPTY_LABEL,)
        self._candidate_entries: dict[str, object] = {}
        self._candidate_ids: frozenset[int] = frozenset()
        self._build()

    # ------------------------------------------------------------- 构 建
    def _build(self) -> None:
        mid = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        mid.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=4)

        left = ttk.Frame(mid)
        ttk.Label(left, text=f"{self.title}记录（选择后编辑右侧词条槽）").pack(anchor=tk.W)
        # 四个筛选轴：类型（小类）/ 种类（具体物品）/ 恩宠·套装 / 武士·忍者。
        filter_row = ttk.Frame(left)
        filter_row.pack(fill=tk.X, pady=(2, 2))
        self.filter_vars: dict[str, tk.StringVar] = {}
        self.filter_combos: dict[str, ttk.Combobox] = {}
        for axis in EQUIPMENT_FILTER_AXES:
            ttk.Label(filter_row, text=f"{self._axis_label(axis)}:").pack(side=tk.LEFT)
            var = tk.StringVar(value=ALL_FILTER)
            combo = ttk.Combobox(filter_row, state="readonly", width=16,
                                 textvariable=var, values=(ALL_FILTER,))
            combo.pack(side=tk.LEFT, padx=2)
            combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_tree())
            self.filter_vars[axis] = var
            self.filter_combos[axis] = combo
        ttk.Button(filter_row, text="清除筛选",
                   command=self.clear_filters).pack(side=tk.LEFT, padx=2)

        self.tree = ttk.Treeview(
            left, columns=("small", "grace", "level", "plus", "rarity"),
            show="tree headings", height=16,
        )
        self.tree.heading("#0", text="记录")
        self.tree.heading("small", text=f"{EQUIPMENT_FILTER_LABELS['small']}（只读）")
        self.tree.heading("grace", text=EQUIPMENT_FILTER_LABELS["grace"])
        self.tree.heading("level", text="等级")
        self.tree.heading("plus", text="+值")
        self.tree.heading("rarity", text="品质")
        self.tree.column("small", width=64, anchor=tk.W)
        self.tree.column("grace", width=150, anchor=tk.W)
        self.tree.column("level", width=52, anchor=tk.CENTER)
        self.tree.column("plus", width=44, anchor=tk.CENTER)
        self.tree.column("rarity", width=76, anchor=tk.CENTER)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._on_selected())
        self.filter_status_var = tk.StringVar(
            value="筛选会级联：四个轴互相收窄，选了任意一个，其余三个只列出还有记录的取值")
        ttk.Label(left, textvariable=self.filter_status_var, foreground="#666666",
                  wraplength=420, justify=tk.LEFT).pack(anchor=tk.W, pady=(2, 0))
        mid.add(left, weight=2)

        right_scroll = ttk.Frame(mid)
        right = self.app._scrollable(right_scroll, width=540, register=False)
        ttk.Label(right, text="词条槽（下拉选择；也可在某一槽里输入关键词，"
                              "只缩小该槽的下拉列表）").pack(anchor=tk.W)
        self.search_status_var = tk.StringVar(value=self._search_hint())
        ttk.Label(right, textvariable=self.search_status_var, foreground="#1a4f8f",
                  wraplength=520, justify=tk.LEFT).pack(anchor=tk.W)
        slot_frame = ttk.Frame(right)
        slot_frame.pack(fill=tk.BOTH, expand=True)
        self.slot_combos: list[ttk.Combobox] = []
        self.slot_labels: list[tk.StringVar] = []
        self.value_vars: list[tk.StringVar] = []
        self.value_entries: list[ttk.Entry] = []
        for index in range(EFFECT_COUNT):
            box = ttk.Frame(slot_frame)
            box.pack(fill=tk.X, pady=(2, 4))
            top = ttk.Frame(box)
            top.pack(fill=tk.X)
            ttk.Label(top, text=f"槽{index + 1}:", width=5).pack(side=tk.LEFT)
            combo = ttk.Combobox(top, width=46, values=(EMPTY_LABEL,))
            combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
            combo.bind("<<ComboboxSelected>>",
                       lambda _event, slot=index: self._on_slot_picked(slot))
            combo.bind("<KeyRelease>",
                       lambda _event, slot=index: self._on_slot_typed(slot))
            combo.bind("<Return>",
                       lambda _event, slot=index: self._on_slot_return(slot))
            self.slot_combos.append(combo)
            self.slot_labels.append(tk.StringVar(value=""))
            bottom = ttk.Frame(box)
            bottom.pack(fill=tk.X)
            ttk.Label(bottom, text="数值:", width=5).pack(side=tk.LEFT)
            value_var = tk.StringVar(value="")
            entry = ttk.Entry(bottom, textvariable=value_var, width=7)
            entry.pack(side=tk.LEFT)
            self.value_vars.append(value_var)
            self.value_entries.append(entry)
            ttk.Label(bottom, textvariable=self.slot_labels[index],
                      foreground="#666666", wraplength=360,
                      justify=tk.LEFT).pack(side=tk.LEFT, padx=6)

        self._build_note(right)
        self._build_level(right)
        self._build_plus(right)
        self._build_rarity(right)
        self._build_grace(right)
        self._build_actions(right)
        self._build_create(right)

        self.item_var = tk.StringVar(
            value=f"选择一条{self.title}记录后，这里会显示它是什么装备。")
        ttk.Label(right, textvariable=self.item_var, foreground="#1a4f8f",
                  wraplength=520, justify=tk.LEFT).pack(anchor=tk.W, pady=(6, 0))
        self.detail_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.detail_var, foreground="#1a4f8f",
                  wraplength=520, justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))
        mid.add(right_scroll, weight=3)
        # This tab wires its own scrolling column (see ``register=False`` above),
        # now that every child of the column exists.
        self.app._bind_wheel_to_children(right, right.master)

    def _build_note(self, right: ttk.Frame) -> None:
        note = (
            f"{self.title}词条来自《仁王3词条装备库v2.21》的对应词条表，"
            "非表内词条一律拒绝。\n"
            "候选表按装备选：远程武器（弓 / 火枪 / 大炮）只看远程词条表，"
            "其余武器只看近战词条表，防具看防具词条表；并且只列出源表标着"
            "「装备种类」相容的词条 —— 例如只标「近战」的词条不会出现在弓的列表里。\n"
            "同名固定词条是该件装备固有的一部分，**禁止修改**（显示为「固定，不可修改」）；"
            "★（星号）词条可以改。恩宠/套装词条不在这排槽里改 —— "
            "选中记录后用右下角的【恩宠 / 套装】栏替换。\n"
            f"等级上限 {limits.LEVEL_CAP}；+值上限按大类（武器 / 防具都是 0..30，"
            f"上限表见 limits.py）；稀有度上限 {limits.rarity_cap(self.big)}"
            "（品质字段 +0x30）。\n"
            f"稀有度的游戏内颜色：{RARITY_COLOR_HINT}"
            "；橙色（5）在当前周目（三周目 / 本体+DLC1）不可达，DLC2 之后才可能开放。\n"
            f"上限来历：{limits.describe_origin()}\n"
            "仅供测试学习用，不要用于联机影响游戏平衡。"
        )
        ttk.Label(right, text=note, foreground="#666666", wraplength=520,
                  justify=tk.LEFT).pack(anchor=tk.W, pady=(6, 0))

    def _build_level(self, right: ttk.Frame) -> None:
        self.level_frame = ttk.LabelFrame(right, text=f"等级（上限 {limits.LEVEL_CAP}）",
                                          padding=(6, 4))
        self.level_frame.pack(fill=tk.X, pady=(6, 0))
        row = ttk.Frame(self.level_frame)
        row.pack(fill=tk.X)
        ttk.Label(row, text="改成:").pack(side=tk.LEFT)
        self.level_var = tk.StringVar(value="")
        self.level_entry = ttk.Entry(row, textvariable=self.level_var, width=8)
        self.level_entry.pack(side=tk.LEFT, padx=4)
        # 单独按钮已撤下：统一由【应用修改】一次应用。控件对象保留给既有的
        # enable/disable 逻辑与测试复用，不再显示在界面上。
        self.level_button = ttk.Button(row, text="应用等级", command=self.apply_level)
        self.level_status_var = tk.StringVar(
            value=f"选择一条{self.title}记录后，这里会显示它的等级。")
        ttk.Label(self.level_frame, textvariable=self.level_status_var,
                  foreground="#666666", wraplength=520,
                  justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))
        self._set_level_enabled(False)

    def _build_plus(self, right: ttk.Frame) -> None:
        caps = self._caps_for_big()
        self.plus_frame = ttk.LabelFrame(
            right, text=f"+值（0..{caps}，字段 +0x0A）", padding=(6, 4))
        self.plus_frame.pack(fill=tk.X, pady=(6, 0))
        row = ttk.Frame(self.plus_frame)
        row.pack(fill=tk.X)
        ttk.Label(row, text="改成:").pack(side=tk.LEFT)
        self.plus_var = tk.StringVar(value="")
        self.plus_entry = ttk.Entry(row, textvariable=self.plus_var, width=8)
        self.plus_entry.pack(side=tk.LEFT, padx=4)
        self.plus_button = ttk.Button(row, text="应用 +值", command=self.apply_plus)
        self.plus_status_var = tk.StringVar(
            value=f"选择一条{self.title}记录后，这里会显示它的 +值。")
        ttk.Label(self.plus_frame, textvariable=self.plus_status_var,
                  foreground="#666666", wraplength=520,
                  justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))
        self._set_plus_enabled(False)

    def _build_rarity(self, right: ttk.Frame) -> None:
        """稀有度（品质）修改：与等级 / +值 完全同构的一行。

        当前值用 ``records.record_rarity`` 的口径显示，范围按**记录所属大类**取
        （武器 / 防具 0..4）；序号对应的游戏内颜色一并显示，说明里给出完整对照。
        """
        self.rarity_frame = ttk.LabelFrame(
            right, text=f"稀有度（0..{limits.rarity_cap(self.big)}，字段 +0x30）",
            padding=(6, 4))
        self.rarity_frame.pack(fill=tk.X, pady=(6, 0))
        row = ttk.Frame(self.rarity_frame)
        row.pack(fill=tk.X)
        ttk.Label(row, text="改成:").pack(side=tk.LEFT)
        self.rarity_var = tk.StringVar(value="")
        self.rarity_entry = ttk.Entry(row, textvariable=self.rarity_var, width=8)
        self.rarity_entry.pack(side=tk.LEFT, padx=4)
        self.rarity_button = ttk.Button(row, text="应用稀有度",
                                        command=self.apply_rarity)
        self.rarity_status_var = tk.StringVar(
            value=f"选择一条{self.title}记录后，这里会显示它的稀有度。")
        ttk.Label(self.rarity_frame, textvariable=self.rarity_status_var,
                  foreground="#666666", wraplength=520,
                  justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))
        self._set_rarity_enabled(False)

    def _build_grace(self, right: ttk.Frame) -> None:
        self.grace_frame = ttk.LabelFrame(right, text="恩宠 / 套装（可替换）",
                                          padding=(6, 4))
        self.grace_frame.pack(fill=tk.X, pady=(6, 0))
        row = ttk.Frame(self.grace_frame)
        row.pack(fill=tk.X)
        ttk.Label(row, text="改成:").pack(side=tk.LEFT)
        self.grace_var = tk.StringVar(value="")
        # 只列**恩宠**族（GraceDb.labels() = 恩宠 + 上位恩宠，共 21 条，与饰品页签同一
        # 来源）：用户规则是套装槽锁死不可替换、恩宠槽只能换成另一个恩宠，所以下拉里
        # 根本不该出现套装，从源头上就选不到。
        self.grace_values = self.app.grace_db.labels()
        self.grace_combo = ttk.Combobox(row, state="readonly", width=40,
                                        textvariable=self.grace_var,
                                        values=self.grace_values)
        self.grace_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self.grace_button = ttk.Button(row, text="应用恩宠/套装",
                                       command=self.apply_grace)
        self.grace_status_var = tk.StringVar(
            value=f"选择一条{self.title}记录后，这里会显示它带的恩宠/套装词条。")
        ttk.Label(self.grace_frame, textvariable=self.grace_status_var,
                  foreground="#666666", wraplength=520,
                  justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))
        self._set_grace_enabled(False)

    def _build_actions(self, right: ttk.Frame) -> None:
        """【预览改动】+【应用修改】：本记录的所有改动一次算完。

        词条、等级、+值、稀有度、恩宠全部由这一个按钮应用，不再逐项点小按钮。
        """
        self.actions_frame = ttk.Frame(right)
        self.actions_frame.pack(fill=tk.X, pady=(6, 0))
        self.preview_button = ttk.Button(self.actions_frame, text="预览改动",
                                         command=self.preview_edits)
        self.preview_button.pack(side=tk.LEFT, padx=2)
        self.apply_button = ttk.Button(self.actions_frame, text="应用修改",
                                       command=self.apply_all)
        self.apply_button.pack(side=tk.LEFT, padx=2)
        ttk.Label(self.actions_frame,
                  text="词条、等级、+值、稀有度、恩宠一次应用（只写真正改过的项）",
                  foreground="#666666").pack(side=tk.LEFT, padx=8)

    def _build_create(self, right: ttk.Frame) -> None:
        self.create_frame = ttk.LabelFrame(
            right, text="无中生有（实验性：需要进游戏实测确认）", padding=(6, 4))
        self.create_frame.pack(fill=tk.X, pady=(6, 0))
        row = ttk.Frame(self.create_frame)
        row.pack(fill=tk.X)
        ttk.Label(row, text="种类:").pack(side=tk.LEFT)
        self.create_kind_combo = ttk.Combobox(
            row, state="readonly", width=28,
            values=tuple(self.kind_choices))
        self.create_kind_combo.pack(side=tk.LEFT, padx=4)
        ttk.Label(row, text="等级:").pack(side=tk.LEFT)
        self.create_level_var = tk.StringVar(value=str(MAX_ITEM_LEVEL))
        ttk.Entry(row, textvariable=self.create_level_var,
                  width=6).pack(side=tk.LEFT, padx=2)
        ttk.Label(row, text="+值:").pack(side=tk.LEFT)
        self.create_plus_var = tk.StringVar(value="0")
        ttk.Entry(row, textvariable=self.create_plus_var,
                  width=4).pack(side=tk.LEFT, padx=2)
        self.create_button = ttk.Button(row, text="新建到空槽", command=self.create_item)
        self.create_button.pack(side=tk.LEFT, padx=4)
        self.create_status_var = tk.StringVar(
            value="实验性功能：需要进游戏实测确认后才算数。用法：在右侧槽位挑好词条，"
                  "选种类并点「新建到空槽」。模板优先取同种类；没有同种类时改用"
                  "同类型（小类）记录并清空不适用的槽，原因会写在这里。")
        ttk.Label(self.create_frame, textvariable=self.create_status_var,
                  foreground="#666666", wraplength=560,
                  justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))

    # ------------------------------------------------------- 列 表 / 筛 选
    def populate(self, views) -> None:
        """Replace the list (called on load and after every edit)."""
        self.views = list(views)
        self.refresh_tree()
        known = {view.slot_index for view in self.views}
        if self.selected not in known:
            self.selected = None
            self._reset_slot_widgets()
            self.item_var.set(f"选择一条{self.title}记录后，这里会显示它是什么装备。")
            self.detail_var.set("")
            self._set_level_enabled(False)
            self._set_plus_enabled(False)
            self._reset_rarity_widgets()

    def set_note(self, message: str) -> None:
        """Report a listing problem without raising (fail soft, per tab)."""
        self.filter_status_var.set(message)

    def reload(self) -> None:
        """Re-list this tab's records from the bytes already in memory."""
        if self.app.decrypted is None:
            self.populate(())
            return
        keep = self.selected
        views = list_equipment(self.app.decrypted, big=self.big,
                               layout=self.app.layout, item_db=self.item_db)
        self.views = list(views)
        self.refresh_tree()
        if keep is not None and any(v.slot_index == keep for v in self.views):
            self.selected = keep
            self.tree.selection_set(str(keep))
            self._on_selected()

    def clear_filters(self) -> None:
        for var in self.filter_vars.values():
            var.set(ALL_FILTER)
        self.refresh_tree()

    def _axis_label(self, axis: str) -> str:
        """筛选轴的名字（武士/忍者 这一轴按本大类实际的取值显示）。"""
        if axis == "school":
            schools = self.item_db.schools(self.big)
            if schools:
                return "/".join(schools)
        return EQUIPMENT_FILTER_LABELS[axis]

    def _view_value(self, axis: str, view: EquipmentView) -> str:
        """这条记录在某个筛选轴上的取值。"""
        if axis == "small":
            return view.small
        if axis == "kind":
            entry = self.item_db.lookup(view.record_type)
            return entry.label if entry is not None else f"{view.record_type:#06x}"
        if axis == "grace":
            return self.grace_name(view)
        return view.school

    def _static_values(self, axis: str) -> tuple[str, ...]:
        """该轴的候选取值：类型 / 种类 / 武士忍者 来自物品总目录，恩宠来自恩宠表。"""
        if axis == "small":
            return self.item_db.small_classes(self.big)
        if axis == "kind":
            return tuple(self.kind_choices)
        if axis == "school":
            return self.item_db.schools(self.big)
        # 恩宠/套装：只列出本页记录真正带着的那些，名字与 ``_view_value``
        # 用的是同一个来源（``GraceDb.describe``），否则筛选项和记录对不上。
        return tuple(sorted({self.grace_name(view) for view in self.views
                             if self.grace_name(view)}))

    def _passes(self, axis: str, view: EquipmentView) -> bool:
        choice = self.filter_vars[axis].get()
        if choice in ("", ALL_FILTER):
            return True
        return self._view_value(axis, view) == choice

    def _shown_views(self) -> list[EquipmentView]:
        return [view for view in self.views
                if all(self._passes(axis, view) for axis in EQUIPMENT_FILTER_AXES)]

    def _cascade_filter_values(self) -> None:
        """每个轴只列出**其余三个轴**仍然允许的取值。"""
        for axis in EQUIPMENT_FILTER_AXES:
            others = [name for name in EQUIPMENT_FILTER_AXES if name != axis]
            pool = [view for view in self.views
                    if all(self._passes(name, view) for name in others)]
            allowed = {self._view_value(axis, view) for view in pool}
            values = [ALL_FILTER] + [value for value in self._static_values(axis)
                                     if value in allowed]
            choice = self.filter_vars[axis].get()
            if choice and choice != ALL_FILTER and choice not in values:
                # 保留当前选择（并说明当前没有记录），而不是把它悄悄重置掉。
                values.append(f"{choice}（当前无记录）")
            self.filter_combos[axis].configure(values=tuple(values))

    def refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        shown = self._shown_views()
        self._cascade_filter_values()
        for view in shown:
            self.tree.insert(
                "", "end", iid=str(view.slot_index),
                text=f"#{view.slot_index} {view.item_name}",
                values=(view.small, self.grace_name(view), view.level,
                        str(view.plus_value),
                        rarity_label(view.rarity, view.rarity_name)),
            )
        total = len(self.views)
        if all(self.filter_vars[axis].get() in ("", ALL_FILTER)
               for axis in EQUIPMENT_FILTER_AXES):
            self.filter_status_var.set(f"共 {total} 条{self.title}记录（未筛选）")
        else:
            self.filter_status_var.set(f"筛选后 {len(shown)} / {total} 条{self.title}记录")

    def grace_name(self, view: EquipmentView) -> str:
        """这条记录带的恩宠/套装名（没有就空串）。"""
        index = self.grace_slot(view)
        if index is None:
            return ""
        return self.app.grace_db.describe(view.effects[index].effect_id)

    def grace_slot(self, view: EquipmentView) -> int | None:
        """恩宠/套装词条所在的槽（词条 id 在恩宠表里才算）。"""
        for index, effect in enumerate(view.effects):
            if not effect.is_empty and self.app.grace_db.describe(effect.effect_id):
                return index
        return None

    # ------------------------------------------------------------ 词 条 槽
    def _caps_for_big(self) -> int:
        """+值上限：按大类查随包表（武器 / 防具 30），查不到退回文档值。"""
        return equipmentdb.plus_cap_for_big(self.big) or MAX_RECORD_PLUS

    def pool_for(self, view: EquipmentView):
        """这件装备的词条池（表 + 装备种类标签）。"""
        return self.app.equipment_pools.get(view.pool)

    def _set_candidates(self, view: EquipmentView | None) -> None:
        """候选词条 = 本池词条中「装备种类标签相容」且**可以手写**的那些。

        同名固定词条（表中 ``is_fixed``）不列进候选：引擎明确拒绝手写它们
        （只能由「改种类」自动带入），列出来只会让用户选了再被拒。占着槽位的
        固定词条照旧显示成「固定，不可修改」。
        """
        self._pool = self.pool_for(view) if view is not None else None
        self._pool_error = ""
        if view is None or self._pool is None:
            self._candidates = (EMPTY_LABEL,)
            self._candidate_entries = {}
            self._candidate_ids = frozenset()
            if view is not None:
                self._pool_error = (f"{view.big}的词条表不可用："
                                    f"{self.app.equipment_error or '未随包提供'}")
            return
        entries = [entry for entry in self._pool.db.all()
                   if not entry.is_fixed
                   and equipment_affix_allowed(self._pool, view.item, entry.effect_id)]
        self._candidates = (EMPTY_LABEL,) + tuple(entry.label for entry in entries)
        self._candidate_entries = {entry.label: entry for entry in entries}
        self._candidate_ids = frozenset(entry.effect_id for entry in entries)

    def _search_hint(self) -> str:
        if self.selected is None:
            return "选择一条记录后，这里会列出它自己的候选词条。"
        count = max(len(self._candidates) - 1, 0)
        pool = self._pool.label if self._pool is not None else "（词条表不可用）"
        return (f"这件装备可以手写的候选词条 {count} 条（{pool}表，已按装备种类标签过滤；"
                "同名固定词条不在候选里，它只能由「改种类」带入）；"
                "在某一槽里输入关键词只缩小该槽的下拉列表，别的槽不受影响。"
                "空格分隔多个关键词＝必须同时包含（如「星 恢复」）")

    def _show_slot_value(self, index: int, value: int | None, *,
                         editable: bool) -> None:
        """把一个槽的数值框整体刷成当前记录的取值，并决定它能不能改。

        切记录时**每个分支都必须走这里**：以前只有「固定词条」「恩宠」两个分支写了
        数值框，普通词条 / 空槽 / 不存在的槽都留着上一条记录的值 —— 于是切到新记录后
        词条变了、数值框还是旧的（用户实测的 bug）。
        """
        self.value_vars[index].set("" if value is None else str(value))
        self.value_entries[index].state(["!disabled"] if editable else ["disabled"])

    def _reset_slot_widgets(self) -> None:
        self._set_candidates(None)
        for index in range(EFFECT_COUNT):
            self.slot_combos[index].configure(values=(EMPTY_LABEL,))
            self.slot_combos[index].set("")
            self.slot_labels[index].set("")
            self._show_slot_value(index, None, editable=False)
        self.search_status_var.set("选择一条记录后，这里会列出它自己的候选词条。")

    def _reset_slot_lists(self) -> None:
        for combo in self.slot_combos:
            combo.configure(values=self._candidates)
        self.search_status_var.set(self._search_hint())

    def _search_candidates(self, keyword: str):
        """在本池里按关键词搜索，再按装备种类标签过滤（顺序与饰品页签一致）。"""
        if self._pool is None:
            return []
        return [entry for entry in self._pool.db.search(keyword.strip())
                if entry.effect_id in self._candidate_ids
                and entry.label in self._candidate_entries]

    def _filter_slot(self, index: int, keyword: str) -> int:
        combo = self.slot_combos[index]
        keyword = keyword.strip()
        if not keyword:
            combo.configure(values=self._candidates)
            return max(len(self._candidates) - 1, 0)
        matches = self._search_candidates(keyword)
        combo.configure(values=(EMPTY_LABEL,) + tuple(e.label for e in matches))
        return len(matches)

    def _on_slot_typed(self, index: int) -> None:
        typed = self.slot_combos[index].get()
        if typed in self._candidate_entries or typed == EMPTY_LABEL:
            return
        count = self._filter_slot(index, typed)
        if not typed.strip():
            self.search_status_var.set(self._search_hint())
        elif count:
            self.search_status_var.set(
                f"槽{index + 1}: 「{typed.strip()}」匹配 {count} 条，"
                "展开该槽的下拉列表选择（其它槽不受影响）")
        else:
            self.search_status_var.set(
                f"槽{index + 1}: 「{typed.strip()}」在本装备的候选词条里没有匹配 —— "
                "换个更短的关键词，或直接写词条 id（如 0x0b32）")

    def _on_slot_return(self, index: int) -> None:
        typed = self.slot_combos[index].get().strip()
        if not typed or typed == EMPTY_LABEL or typed in self._candidate_entries:
            return
        matches = self._search_candidates(typed)
        if len(matches) == 1:
            self.slot_combos[index].set(matches[0].label)
            self._on_slot_picked(index)
            self.slot_combos[index].configure(values=self._candidates)
            self.search_status_var.set(f"槽{index + 1}: 已选中 {matches[0].label}")
        elif matches:
            self._filter_slot(index, typed)
            self.search_status_var.set(
                f"槽{index + 1}: 「{typed}」匹配 {len(matches)} 条，"
                "请从该槽的下拉列表里选一条")
        else:
            self._filter_slot(index, typed)
            self.search_status_var.set(
                f"槽{index + 1}: 「{typed}」在本装备的候选词条里没有匹配")

    def _on_slot_picked(self, index: int) -> None:
        """Pick a value: show the affix's own value span and whether it may be typed.

        换词条后数值立刻跟着新词条走（用户规则）：有区间 → 可改；只有唯一取值 →
        只显示该值且不可编辑。
        """
        if self.selected is None:
            return
        text = self.slot_combos[index].get()
        if not text or text == EMPTY_LABEL:
            self.slot_labels[index].set("")
            self._show_slot_value(index, None, editable=False)
            return
        entry = self._candidate_entries.get(text)
        if entry is None:
            self.slot_labels[index].set("")
            return
        editable = affix_value_editable(entry)
        self._show_slot_value(index, entry.value, editable=editable)
        span = (f"可改区间 {entry.describe_value_range()}" if editable
                else entry.describe_value_range())
        self.slot_labels[index].set(
            span if editable else f"{span} ← 固定值，数值不可改（词条可换）")

    @staticmethod
    def _entry_from_id_text(text: str, db):
        for base in (16, 10):
            try:
                value = int(text, base)
            except ValueError:
                continue
            entry = db.lookup(value)
            if entry is not None:
                return entry
        return None

    def _resolve_slot_text(self, index: int) -> str:
        combo = self.slot_combos[index]
        if "disabled" in combo.state():
            return ""
        typed = combo.get().strip()
        if not typed or typed == EMPTY_LABEL or typed in self._candidate_entries:
            return typed
        matches = self._search_candidates(typed)
        if len(matches) == 1:
            combo.set(matches[0].label)
            return matches[0].label
        if self._pool is not None:
            by_id = self._entry_from_id_text(typed, self._pool.db)
            if by_id is not None and by_id.label in self._candidate_entries:
                combo.set(by_id.label)
                return by_id.label
        raise EditorError(
            f"槽{index + 1} 的文本不是这件装备能用的词条：{typed!r}"
            f"（候选里匹配 {len(matches)} 条，请从该槽下拉列表里选一条）")

    def _slot_value(self, index: int):
        text = self.value_vars[index].get().strip()
        if not text:
            return None
        try:
            return int(text, 10)
        except ValueError as error:
            raise EditorError(f"槽{index + 1} 的数值必须是整数，实际 {text!r}") from error

    # ------------------------------------------------------------ 选 中
    def _selected_view(self) -> EquipmentView | None:
        return next((view for view in self.views
                     if view.slot_index == self.selected), None)

    def _on_selected(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        self.selected = int(selection[0])
        view = self._selected_view()
        if view is None:
            return
        self._set_candidates(view)
        self._reset_slot_lists()
        grace = self.grace_slot(view)
        pool = self._pool
        for index in range(EFFECT_COUNT):
            combo = self.slot_combos[index]
            if index >= len(view.effects):
                combo.configure(values=(EMPTY_LABEL,))
                combo.set(EMPTY_LABEL)
                apply_empty_slot_state(combo, self.slot_labels[index],
                                       self.value_entries[index],
                                       self.value_vars[index])
                continue
            effect = view.effects[index]
            if effect.is_empty:
                combo.set(EMPTY_LABEL)
                apply_empty_slot_state(combo, self.slot_labels[index],
                                       self.value_entries[index],
                                       self.value_vars[index])
                continue
            entry = pool.db.lookup(effect.effect_id) if pool is not None else None
            # 先查恩宠/套装名表：名表条目的 0x40 是它自己的标记，不是"同名固定词条"，
            # 所以套装/恩宠绝不能落进下面的"固定，不可修改"分支（用户实测案例：#1206
            # 的槽5 是 0xd363 加贺百万石的荣华，被显示成"非本池词条（固定，不可修改）"）。
            grace_entry = self.app.grace_db.lookup(effect.effect_id)
            if grace_entry is not None:
                family = grace_family_of(grace_entry.category)
                named = self.app.grace_db.describe(effect.effect_id)
                kind = grace_entry.category
                combo.state(["disabled"])
                self._show_slot_value(index, effect.value, editable=False)
                if family == "套装":
                    text_value = f"{effect.effect_id:#06x} {named}（不可替换）"
                    combo.configure(values=(text_value,))
                    combo.set(text_value)
                    self.slot_labels[index].set(
                        f"数值={effect.value} 标识={effect.metadata:#010x}"
                        f" ← {kind}：套装与物品种类强绑定，任何替换都会被拒绝")
                else:
                    text_value = f"{effect.effect_id:#06x} {named}（{kind}）"
                    combo.configure(values=(text_value,))
                    combo.set(f"{effect.effect_id:#06x} {named}（用下方恩宠/套装栏替换）")
                    self.slot_labels[index].set(
                        f"数值={effect.value} ← {kind}（{kind}）："
                        "用下方【恩宠 / 套装】栏替换（恩宠只能换恩宠）")
                continue
            if view.slot_is_fixed(index, pool):
                label = entry.label if entry is not None else                     f"{effect.effect_id:#06x}（非本池词条）"
                combo.configure(values=(f"{label}（固定，不可修改）",))
                combo.set(f"{label}（固定，不可修改）")
                combo.state(["disabled"])
                self._show_slot_value(index, effect.value, editable=False)
                self.slot_labels[index].set(
                    f"数值={effect.value} 标识={effect.metadata:#010x}"
                    " ← 固定词条，词条与数值都不能改")
                continue
            if index == grace:
                named = self.app.grace_db.describe(effect.effect_id)
                combo.configure(values=(f"{effect.effect_id:#06x} {named}",))
                combo.set(f"{effect.effect_id:#06x} {named}（用下方恩宠/套装栏替换）")
                combo.state(["disabled"])
                self._show_slot_value(index, effect.value, editable=False)
                self.slot_labels[index].set(
                    f"数值={effect.value} ← 恩宠/套装词条：用下方【恩宠 / 套装】栏替换")
                continue
            if entry is not None:
                combo.state(["!disabled"])
                combo.set(entry.label)
                # A star slot is editable on purpose (★ is never a fixed affix).
                marks = ("★ 词条（可改）" if view.slot_is_star(index) else "可改")
                editable = affix_value_editable(entry)
                self._show_slot_value(index, effect.value, editable=editable)
                span = (f"可改区间 {entry.describe_value_range()}" if editable
                        else entry.describe_value_range())
                self.slot_labels[index].set(
                    f"数值={effect.value} 标识={effect.metadata:#010x}"
                    f"（{span}）· {marks}"
                    + ("" if editable else " · 固定值，数值不可改（词条可换）"))
            else:
                combo.state(["!disabled"])
                outside = f"{effect.effect_id:#06x}（非本池词条）"
                combo.configure(values=(EMPTY_LABEL, outside))
                combo.set(outside)
                self._show_slot_value(index, effect.value,
                                      editable=affix_value_editable(entry))
                self.slot_labels[index].set(
                    f"数值={effect.value} 标识={effect.metadata:#010x}"
                    " ← 这条不在本装备的词条表里；换掉它才能改这一槽")
        limits = class_limits_for_record(view.record_type, item_db=self.item_db)
        self.item_var.set(
            f"记录 #{view.slot_index}：{self.item_db.lookup(view.record_type).label} "
            f"（{view.big}/{view.category}/{view.small}）"
            f"  Lv{view.level} {view.rarity_name}  词条池 {view.pool_label}"
        )
        self.level_var.set(str(view.level))
        self.plus_var.set(str(view.plus_value))
        if limits is not None:
            self.level_frame.configure(text=f"等级（上限 {limits.max_level}）")
            self.plus_frame.configure(text=f"+值（0..{limits.max_plus}，字段 +0x0A）")
            self.level_status_var.set(
                f"当前 Lv{view.level}；{limits.describe()}")
            self.plus_status_var.set(
                f"当前 +{view.plus_value}（字段 +0x0A，只写这两个字节）")
        self._set_level_enabled(True)
        self._set_plus_enabled(True)
        self._refresh_rarity_state(view)
        self._refresh_grace_state(view)
        self.detail_var.set("")

    # ------------------------------------------------------- 收 集 / 应 用
    def _pending_edit(self, index: int, view: EquipmentView):
        """把一个槽的控件状态变成一个改动；没有改动返回 ``None``。"""
        try:
            text = self._resolve_slot_text(index)
        except EditorError as error:
            raise UiEditError(str(error)) from error
        current = view.effects[index]
        if not text:
            return None
        if text == EMPTY_LABEL:
            if current.is_empty:
                return None
            return {"slot_index": index, "effect_id": EMPTY_EFFECT_ID}
        entry = self._candidate_entries.get(text)
        if entry is None:
            return None  # 表外/未选中的文本：没有改动可收集
        if entry.effect_id == current.effect_id:
            typed = self._slot_value(index)
            if typed is None or typed == current.value:
                return None
            return {"slot_index": index, "effect_id": entry.effect_id, "value": typed}
        typed = self._slot_value(index)
        if typed is not None and typed == current.value                 and not entry.allows_value(typed):
            typed = None  # 预填的是旧词条的数值，对新词条没有意义
        return {"slot_index": index, "effect_id": entry.effect_id,
                "value": entry.value if typed is None else typed}

    def _editable_slots(self, view: EquipmentView) -> tuple[int, ...]:
        """能改的槽：既不是固定词条，也不是恩宠/套装那一槽。"""
        grace = self.grace_slot(view)
        pool = self.pool_for(view)
        return tuple(index for index in range(len(view.effects))
                     if index != grace and not view.slot_is_fixed(index, pool))

    def current_edits(self) -> tuple[dict[str, int], ...]:
        view = self._selected_view()
        if view is None:
            return ()
        edits: list[dict[str, int]] = []
        skipped: list[str] = []
        for index in self._editable_slots(view):
            try:
                edit = self._pending_edit(index, view)
            except UiEditError as error:
                # 一格里是表外词条只影响它自己：记下来，其余槽的改动照常收集，
                # 不要因为一格而把整条记录的改动全丢掉（用户实测反馈）。
                skipped.append(f"槽{index + 1}（{error}）")
                continue
            if edit is not None:
                edits.append(dict(edit, record_index=view.slot_index))
        if skipped:
            messagebox.showwarning(
                "提示",
                "这些槽保持原样、没有收集改动：" + "；".join(skipped)
                + "。其它槽的改动照常应用。")
        return tuple(edits)

    def describe_edits(self, view: EquipmentView,
                       edits: tuple[dict[str, int], ...]) -> str:
        """改动预览：逐槽说清楚「从什么改成什么」。"""
        names: list[str] = []
        for edit in edits:
            index = int(edit["slot_index"])
            current = view.effects[index]
            new_id = int(edit["effect_id"])
            if new_id == EMPTY_EFFECT_ID:
                new_name = EMPTY_LABEL
            else:
                entry = (self._candidate_entries.get(self.slot_combos[index].get())
                         or (self._pool.db.lookup(new_id) if self._pool else None))
                new_name = entry.label if entry is not None else f"{new_id:#06x}"
            old_name = (self._pool.db.lookup(current.effect_id).label
                        if self._pool is not None
                        and self._pool.db.lookup(current.effect_id) is not None
                        else (EMPTY_LABEL if current.is_empty
                              else f"{current.effect_id:#06x}"))
            value = f"，数值 {current.value} → {edit['value']}" if "value" in edit else ""
            names.append(f"槽{index + 1}: {old_name} → {new_name}{value}")
        return "；".join(names)

    def _require_view(self) -> EquipmentView | None:
        """当前选中的记录；没有就给出**准确**的提示并返回 ``None``。

        「还没读取数据」和「读了但没选记录」是两件事，合并成一句话会把用户
        引向错误的操作（见 MSG_NEED_DATA / MSG_NEED_SELECTION）。
        """
        if self.app.decrypted is None:
            messagebox.showwarning("提示", MSG_NEED_DATA)
            return None
        view = self._selected_view()
        if view is None:
            messagebox.showwarning("提示", MSG_NEED_SELECTION)
            return None
        return view

    def preview_edits(self) -> None:
        view = self._require_view()
        if view is None:
            return
        edits = self.current_edits()
        if not edits:
            messagebox.showwarning("提示", "当前没有检测到改动")
            return
        try:
            plan_equipment_edits(self.app.decrypted, edits,
                                 item_db=self.item_db,
                                 pools=self.app.equipment_pools,
                                 grace_db=self.app.grace_db,
                                 layout=self.app.layout)
        except Exception as error:  # noqa: BLE001 - 预览也要给出拒绝原因
            self.detail_var.set(f"预览被拒绝：{error}")
            messagebox.showerror("预览被拒绝", str(error))
            return
        text = self.describe_edits(view, edits)
        self.detail_var.set(f"预览（尚未写入）：{text}")
        self.app._status(f"{self.title}记录 #{view.slot_index} 预览：{text}")

    def apply_edits(self) -> None:
        view = self._require_view()
        if view is None:
            return
        edits = self.current_edits()
        if not edits:
            messagebox.showwarning("提示", "当前没有检测到改动")
            return
        target = view.slot_index
        try:
            plan_equipment_edits(self.app.decrypted, edits, item_db=self.item_db,
                                 pools=self.app.equipment_pools,
                                 grace_db=self.app.grace_db, layout=self.app.layout)
            self.app.decrypted = apply_equipment_edits(
                self.app.decrypted, edits, item_db=self.item_db,
                pools=self.app.equipment_pools, grace_db=self.app.grace_db,
                layout=self.app.layout)
        except Exception as error:  # noqa: BLE001 - 通过对话框反馈
            messagebox.showerror("错误", str(error))
            return
        self.detail_var.set(f"已应用（内存中，尚未写入存档）：{self.describe_edits(view, edits)}")
        self.app._status(f"{self.title}记录 #{target} 的修改已应用到内存数据（尚未写入存档）")
        self.reload()

    # --------------------------------------------------- 等级 / +值 / 新建
    def _set_level_enabled(self, enabled: bool) -> None:
        state = ["!disabled"] if enabled else ["disabled"]
        self.level_entry.state(state)
        self.level_button.state(state)

    def _set_plus_enabled(self, enabled: bool) -> None:
        state = ["!disabled"] if enabled else ["disabled"]
        self.plus_entry.state(state)
        self.plus_button.state(state)

    # -- 稀 有 度 ----------------------------------------------------------
    def _set_rarity_enabled(self, enabled: bool) -> None:
        state = ["!disabled"] if enabled else ["disabled"]
        self.rarity_entry.state(state)
        self.rarity_button.state(state)

    def _reset_rarity_widgets(self) -> None:
        """没选记录：稀有度框清空并置灰，**不留上一条的值**。"""
        self.rarity_var.set("")
        self.rarity_status_var.set(
            f"选择一条{self.title}记录后，这里会显示它的稀有度。")
        self._set_rarity_enabled(False)

    def _refresh_rarity_state(self, view: EquipmentView) -> None:
        """把稀有度框刷成**这条记录**的值与可用范围（切记录时逐字段重置）。"""
        cap = self.app.rarity_cap_for_record(view.record_type)
        color = rarity_color_of(view.rarity)
        colored = f"（{color}）" if color else ""
        if cap is None:
            # 表外 id：引擎也会拒绝（fail closed），这里先说清楚，不猜一个上限。
            self.rarity_var.set("")
            self.rarity_frame.configure(
                text=f"稀有度（{view.record_type:#06x} 不在物品总目录里）")
            self.rarity_status_var.set(
                f"不可改：记录 #{view.slot_index} 的种类 {view.record_type:#06x} "
                "不在物品总目录（data/equipment_items.json）里，无法确定稀有度上限。")
            self._set_rarity_enabled(False)
            return
        self.rarity_var.set(str(view.rarity))
        self.rarity_frame.configure(text=f"稀有度（0..{cap}，字段 +0x30）")
        self.rarity_status_var.set(
            f"当前 {view.rarity}{colored}（{view.rarity_name}）。范围 0..{cap}"
            f"（按大类「{self.big}」）；颜色对照 {RARITY_COLOR_HINT}；"
            f"橙色（5）在当前周目不可达。{limits.describe_origin()}")
        self._set_rarity_enabled(True)

    def _snapshot_widgets(self) -> dict:
        """用户此刻在各控件里填的内容（批量应用期间用它在每步之前复原）。"""
        return {
            "slots": [combo.get() for combo in self.slot_combos],
            "values": [var.get() for var in self.value_vars],
            "level": self.level_var.get(),
            "plus": self.plus_var.get(),
            "rarity": self.rarity_var.get(),
            "grace": self.grace_var.get(),
        }

    def _restore_widgets(self, snapshot: dict) -> None:
        for combo, text in zip(self.slot_combos, snapshot["slots"]):
            combo.set(text)
        for var, text in zip(self.value_vars, snapshot["values"]):
            var.set(text)
        self.level_var.set(snapshot["level"])
        self.plus_var.set(snapshot["plus"])
        self.rarity_var.set(snapshot["rarity"])
        self.grace_var.set(snapshot["grace"])

    def _pending_changes(self) -> tuple[list[str], list]:
        """本次要应用的改动：文字说明 + 对应动作（只收真正改过的项）。"""
        view = self._selected_view()
        if view is None:
            return [], []
        parts: list[str] = []
        calls: list = []
        try:
            edits = self.current_edits()
        except UiEditError as error:
            messagebox.showwarning("提示", str(error))
            return [], []
        if edits:
            parts.append(self.describe_edits(view, edits))
            calls.append(self.apply_edits)
        level_text = self.level_var.get().strip()
        if level_text:
            try:
                level = int(level_text, 10)
            except ValueError:
                level = None
            if level is not None and level != view.level:
                parts.append(f"等级 {view.level} → {level}")
                calls.append(self.apply_level)
        plus_text = self.plus_var.get().strip()
        if plus_text:
            try:
                plus = int(plus_text, 10)
            except ValueError:
                plus = None
            if plus is not None and plus != view.plus_value:
                parts.append(f"+值 {view.plus_value} → {plus}")
                calls.append(self.apply_plus)
        rarity_text = self.rarity_var.get().strip()
        if rarity_text:
            try:
                rarity = int(rarity_text, 10)
            except ValueError:
                rarity = None
            if rarity is not None and rarity != view.rarity:
                parts.append(f"稀有度 {view.rarity} → {rarity}")
                calls.append(self.apply_rarity)
        current_grace = self.grace_name(view)
        chosen_grace = self.grace_var.get().strip()
        if chosen_grace and chosen_grace != current_grace:
            parts.append(f"恩宠/套装 {current_grace or '（无）'} → {chosen_grace}")
            calls.append(self.apply_grace)
        return parts, calls

    def apply_all(self) -> None:
        """统一入口：词条 + 等级 + +值 + 稀有度 + 恩宠，一次确认、一次应用。"""
        if self._require_view() is None:
            return
        parts, calls = self._pending_changes()
        if not calls:
            messagebox.showwarning("提示", "当前没有检测到改动")
            return
        if not messagebox.askokcancel(
            "确认应用修改",
            "将应用以下改动（内存中，尚未写入存档）：\n\n· "
            + "\n· ".join(parts)
            + "\n\n· 只改这些字段，其余字节不动；点【写入存档】前会自动备份。",
            icon="warning",
        ):
            return
        # 每一步 apply 结束都会 reload（把输入框刷成记录当前值），所以调用前先把
        # 用户填写的快照放回去，否则后一步的输入会被前一步的刷新冲掉。
        snapshot = self._snapshot_widgets()
        with _quiet_dialogs():
            for call in calls:
                self._restore_widgets(snapshot)
                call()
        self.app._status(
            f"{self.title}记录 #{self.selected} 的改动已应用到内存数据（尚未写入存档）")

    def apply_rarity(self) -> None:
        view = self._require_view()
        if view is None:
            return
        try:
            rarity = int(self.rarity_var.get().strip(), 10)
        except ValueError:
            messagebox.showwarning("提示", "稀有度必须是整数")
            return
        cap = self.app.rarity_cap_for_record(view.record_type)
        cap_text = "（这条记录的种类不在物品总目录里，引擎会拒绝）" if cap is None \
            else f"0..{cap}"
        if not messagebox.askokcancel(
            "确认修改稀有度",
            f"把{self.title}记录 #{view.slot_index} 的稀有度改成 {rarity}？\n\n"
            "· 只写入品质字段（+0x30 的低 4 位；写成 0 时才顺带清 +0x31 的低 4 位），"
            "词条、等级、+值、标识都不动；\n"
            f"· 合法范围 {cap_text}（按大类取，上限表见 limits.py）；\n"
            f"· 颜色对照 {RARITY_COLOR_HINT}；橙色（5）在当前周目不可达；\n"
            "· 存档写入前会自动备份，出问题可以用「恢复备份」恢复。",
            icon="warning",
        ):
            return
        try:
            plan = plan_rarity_edit(self.app.decrypted, view.slot_index, rarity,
                                    affix_db=self.app.affix_db,
                                    known_ids=self.app.known_ids,
                                    layout=self.app.layout)
            self.app.decrypted = apply_rarity_edits(self.app.decrypted, [plan])
        except Exception as error:  # noqa: BLE001 - 通过对话框反馈
            messagebox.showerror("错误", str(error))
            return
        self.detail_var.set(f"{plan.describe()}（内存中，尚未写入存档）")
        self.app._status(
            f"{self.title}记录 #{view.slot_index} 的稀有度改为 {plan.new_value}")
        self.reload()

    def apply_level(self) -> None:
        view = self._require_view()
        if view is None:
            return
        try:
            level = int(self.level_var.get().strip(), 10)
        except ValueError:
            messagebox.showwarning("提示", "等级必须是整数")
            return
        if not messagebox.askokcancel(
            "确认修改等级",
            f"把{self.title}记录 #{view.slot_index} 的等级改成 {level}？\n\n"
            "· 只写入等级字段（+0x06/+0x08），词条数值不会被改写；\n"
            "· 请谨慎修改：改完请进游戏确认显示与属性是否正常；\n"
            "· 存档写入前会自动备份，出问题可以用「恢复备份」恢复。",
            icon="warning",
        ):
            return
        try:
            plan = plan_level_edit(self.app.decrypted, view.slot_index, level,
                                   affix_db=self.app.affix_db,
                                   known_ids=self.app.known_ids,
                                   layout=self.app.layout)
            self.app.decrypted = apply_level_edits(self.app.decrypted, [plan])
        except Exception as error:  # noqa: BLE001 - 通过对话框反馈
            messagebox.showerror("错误", str(error))
            return
        self.detail_var.set(f"{plan.describe()}（内存中，尚未写入存档）")
        self.app._status(f"{self.title}记录 #{view.slot_index} 的等级改为 {plan.new_level}")
        self.reload()

    def apply_plus(self) -> None:
        view = self._require_view()
        if view is None:
            return
        try:
            plus = int(self.plus_var.get().strip(), 10)
        except ValueError:
            messagebox.showwarning("提示", "+值必须是整数")
            return
        limits = class_limits_for_record(view.record_type, item_db=self.item_db)
        cap = limits.max_plus if limits is not None else MAX_RECORD_PLUS
        if not messagebox.askokcancel(
            "确认修改 +值",
            f"把{self.title}记录 #{view.slot_index} 的 +值改成 {plus}？\n\n"
            "· 只写入 +值 字段（+0x0A）与校验和，词条、等级、标识都不动；\n"
            f"· 合法范围 0..{cap}（按大类收集的上限）；\n"
            "· 存档写入前会自动备份，出问题可以用「恢复备份」恢复。",
            icon="warning",
        ):
            return
        try:
            plan = plan_plus_edit(self.app.decrypted, view.slot_index, plus,
                                  affix_db=self.app.affix_db,
                                  known_ids=self.app.known_ids,
                                  layout=self.app.layout)
            self.app.decrypted = apply_plus_edits(self.app.decrypted, [plan])
        except Exception as error:  # noqa: BLE001 - 通过对话框反馈
            messagebox.showerror("错误", str(error))
            return
        self.detail_var.set(f"{plan.describe()}（内存中，尚未写入存档）")
        self.app._status(f"{self.title}记录 #{view.slot_index} 的 +值改为 {plan.new_value}")
        self.reload()

    # --------------------------------------------------------- 恩 宠 / 套 装
    def _set_grace_enabled(self, enabled: bool) -> None:
        state = ["!disabled"] if enabled else ["disabled"]
        self.grace_combo.state(state)
        self.grace_button.state(state)

    def _refresh_grace_state(self, view: EquipmentView) -> None:
        availability = equipment_grace_availability(
            view, grace_db=self.app.grace_db, pool=self.pool_for(view))
        self.grace_availability = availability
        if not availability.allowed:
            self.grace_status_var.set(f"不可改：{availability.reason}")
            self.grace_combo.set("")
            self._set_grace_enabled(False)
            return
        self.grace_status_var.set(
            f"当前：{availability.describe_current()}"
            f"（槽{int(availability.slot_index) + 1}）。只能换成另一个恩宠"
            "（恩宠 10 条 + 上位恩宠 11 条）；套装与物品种类强绑定，不能替换。"
            "写入只改这一槽的词条 id 与数值。")
        self._set_grace_enabled(True)
        for index, label in enumerate(self.grace_values):
            if availability.current_id is not None and \
                    label.startswith(f"{availability.current_id:#06x} "):
                self.grace_combo.current(index)
                break

    def apply_grace(self) -> None:
        view = self._require_view()
        if view is None:
            return
        text_value = self.grace_combo.get().strip()
        if not text_value:
            messagebox.showwarning("提示", "请先在下拉里选择要换成的恩宠")
            return
        try:
            try:
                grace_id = int(text_value.split(" ", 1)[0], 16)
            except ValueError as error:
                raise GraceEditError(
                    f"下拉里的值不是恩宠/套装 id：{text_value}") from error
            if self.app.grace_db.lookup(grace_id) is None:
                raise GraceEditError(
                    f"{text_value.split(' ', 1)[0]} 不在恩宠/套装名表里")
            availability = equipment_grace_availability(
                view, grace_db=self.app.grace_db, pool=self.pool_for(view))
            if not availability.allowed:
                raise GraceEditError(availability.reason)
        except (GraceEditError, EditorError) as error:
            messagebox.showerror("错误", str(error))
            return
        name = self.app.grace_db.describe(grace_id)
        if not messagebox.askokcancel(
            "确认修改恩宠",
            f"把{self.title}记录 #{view.slot_index} 的恩宠换成 {name}？\n\n"
            "· 只允许恩宠换恩宠；套装与物品种类强绑定，不能替换。\n"
            "· 只写这一槽的词条 id 与数值，槽里的其它标识（种类码、固定/★、byte 11）"
            "原样保留；\n"
            "· 一件装备只能有一个恩宠/套装；\n"
            "· 存档写入前会自动备份，出问题可以用「恢复备份」恢复。",
            icon="warning",
        ):
            return
        try:
            self.app.decrypted = apply_equipment_grace_edit(
                self.app.decrypted, view.slot_index, grace_id,
                grace_db=self.app.grace_db, item_db=self.item_db,
                pools=self.app.equipment_pools, known_ids=self.app.known_ids,
                layout=self.app.layout)
        except Exception as error:  # noqa: BLE001 - 通过对话框反馈
            messagebox.showerror("错误", str(error))
            return
        self.detail_var.set(
            f"记录 #{view.slot_index} 槽{int(availability.slot_index) + 1} 的"
            f"恩宠/套装改为 {name}（内存中，尚未写入存档）")
        self.app._status(f"{self.title}记录 #{view.slot_index} 的恩宠/套装改为 {name}")
        self.reload()

    def _chosen_effects(self) -> list[dict[str, int]]:
        effects: list[dict[str, int]] = []
        for index in range(EFFECT_COUNT):
            text = self.slot_combos[index].get().strip()
            entry = self._candidate_entries.get(text)
            if entry is None:
                continue
            edit: dict[str, int] = {"slot_index": index, "effect_id": entry.effect_id}
            value = self._slot_value(index)
            if value is not None:
                edit["value"] = value
            effects.append(edit)
        return effects

    def create_item(self) -> None:
        if self.app.decrypted is None:
            messagebox.showwarning("提示", "请先读取存档")
            return
        combo = self.create_kind_combo
        item = self.kind_choices.get(combo.get())
        if item is None:
            messagebox.showwarning("提示", "请先选择要新建的种类")
            return
        try:
            level = int(self.create_level_var.get().strip(), 10)
            plus = int(self.create_plus_var.get().strip() or "0", 10)
            effects = self._chosen_effects()
        except (ValueError, EditorError) as error:
            messagebox.showwarning("提示", str(error) or "等级 / +值必须是整数")
            return
        try:
            plan = plan_create_equipment(
                self.app.decrypted, record_type=item.item_id, level=level, plus=plus,
                effects=effects, item_db=self.item_db,
                pools=self.app.equipment_pools, grace_db=self.app.grace_db,
                layout=self.app.layout,
            )
        except CreationError as error:
            self.create_status_var.set(str(error))
            messagebox.showerror("无法新建", str(error))
            return
        detail = [plan.describe()]
        if plan.cleared_slots:
            detail.append("模板带入但已清空的槽："
                          + "、".join(f"槽{index + 1}" for index in plan.cleared_slots))
        else:
            detail.append("模板带入的槽全部可用，没有清空任何槽")
        if not messagebox.askokcancel(
            "确认新建",
            f"新建一件 {item.label}（Lv{level} +{plan.plus_value}）放进空槽？\n\n"
            + "\n".join("· " + line for line in detail)
            + "\n\n· 这是实验性功能：造出来的装备还没有在游戏里核对过，"
              "请先备份、进游戏确认后再继续。\n"
              "· 写入前会自动备份；背包已满时会拒绝。",
            icon="warning",
        ):
            return
        try:
            self.app.decrypted = apply_equipment_creations(self.app.decrypted, [plan])
        except CreationError as error:
            self.create_status_var.set(str(error))
            messagebox.showerror("无法新建", str(error))
            return
        self.create_status_var.set("\n".join(detail))
        self.app._status(f"已新建 {plan.describe()}（尚未写入存档）")
        self.reload()
        if any(view.slot_index == plan.slot_index for view in self.views):
            self.tree.selection_set(str(plan.slot_index))
            self._on_selected()

    def set_error(self, message: str) -> None:
        """目录/词条表缺失时把页签标成只读（fail closed）。"""
        self.create_status_var.set(message)
        self.create_button.state(["disabled"])
        self.preview_button.state(["disabled"])
        self.apply_button.state(["disabled"])



class AccessoryEditorApp(tk.Tk):
    """Main window: save selection, record list, affix slots, write actions."""

    def __init__(self, config: EditorConfig | None = None) -> None:
        super().__init__()
        self.config = config if config is not None else load_config()
        self.state_root = self.config.resolved_backup_root()
        self.title(f"{TITLE} · v{version_info().version}")
        # The editor column is tall (词条槽 + 等级 + +值 + 种类 + 新建), so the
        # default window is sized for it and every column scrolls as a fallback.
        self.geometry("1280x900")
        self.minsize(1040, 660)

        # Keep references: a PhotoImage that no widget holds is garbage collected
        # and the image silently disappears.
        self._images: dict[str, tk.PhotoImage] = {}
        self._apply_window_icon()

        self.affix_db = AffixDb()
        # 恩宠/套装 name table: display only (see affixdb.GraceDb).  A missing or
        # stale file must not stop the editor from running, it only costs names.
        self.grace_db = GraceDb.best_effort()
        self.item_db = ItemDb.best_effort()
        # Label -> entry maps for the filter/词条 comboboxes and for resolving what
        # the user picked (需求 3/4): a combo label is the entry's stable label.
        self.ALL_FILTER = ALL_FILTER
        self.item_choices = {entry.label: entry for entry in self.item_db.all()}
        self.grace_choices = {entry.name: entry for entry in self.grace_db.all()}
        self.affix_by_label = {entry.label: entry for entry in self.affix_db.all()}
        # 魂核 use their own affix pool and item table; a missing file disables the
        # 魂核 tab instead of failing the app (fail closed: no data, no edits).
        self.soul_db_error = ""
        try:
            self.soul_db = AffixDb(load_soul_catalog())
        except AffixError as error:
            self.soul_db = AffixDb([])
            self.soul_db_error = str(error)
        self.soul_item_db = ItemDb.best_effort(default_soul_items_path())
        # 武器 / 防具页签：物品种类表（物品总目录全类别）+ 三个词条池。
        # 缺文件时页签只读并说明原因，而不是让整个窗口起不来（fail closed）。
        self.equipment_item_db = equipmentdb.load_equipment_item_db()
        self.equipment_error = self.equipment_item_db.error
        self.equipment_pools: dict[str, object] = {}
        for pool_key in (equipmentdb.POOL_MELEE, equipmentdb.POOL_RANGED,
                         equipmentdb.POOL_ARMOR):
            try:
                self.equipment_pools[pool_key] = equipmentdb.load_pool(pool_key)
            except Exception as error:  # noqa: BLE001 - 缺表只影响对应页签
                self.equipment_error = self.equipment_error or str(error)
        self.equipment_tabs: list[EquipmentTab] = []
        self.soul_views: list[SoulCoreView] = []
        #: (canvas, inner frame) per scrollable editor column (饰品 / 魂核).
        self.scroll_columns: list[tuple[tk.Canvas, ttk.Frame]] = []
        self.selected_soul: int | None = None
        self.soul_layout = None
        self.soul_known_ids: frozenset[int] = frozenset()
        self.grace_availability = None
        self._backend_note = ""
        self.crypto = self._build_crypto()
        self.saves: list[SaveDescriptor] = []
        self.accessory_views: list[AccessoryView] = []
        self.other_views: list[AccessoryView] = []
        # The array the listing came from: edits must reuse exactly this one.
        self.layout: InventoryLayout | None = None
        self.decrypted: bytes | None = None
        self.selected_save: SaveDescriptor | None = None
        self.selected_accessory: int | None = None
        self.checksum_ok = False
        self.worker_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._game_status_at = 0.0

        self._build_ui()
        # 统一【应用修改】之后，饰品 / 魂核的逐字段按钮不再显示；控件对象保留，
        # 因为既有的启用/禁用逻辑与部分测试仍在引用它们（这里只从布局里撤下）。
        for button in (getattr(self, "level_button", None),
                       getattr(self, "plus_button", None),
                       getattr(self, "rarity_button", None),
                       getattr(self, "grace_button", None),
                       getattr(self, "soul_level_button", None),
                       getattr(self, "soul_rarity_button", None)):
            if button is not None:
                button.pack_forget()
        self.after(80, self._poll_worker)
        self.after(200, self._poll_game_status)
        self.refresh_saves()

    def _build_crypto(self) -> SaveCrypto:
        """Use the configured backend, degrading to pure Python when missing."""
        backend = self.config.crypto_backend()
        if backend.note:
            self._backend_note = backend.note
        return SaveCrypto(backend.executable, prefer_python=backend.prefer_python)

    # -------------------------------------------------------------- branding

    def _apply_window_icon(self) -> None:
        """Set the taskbar/title-bar icon from ``assets/app.ico``.

        ``iconbitmap`` gives Windows the full multi-resolution icon (so the
        taskbar picks a crisp size); ``iconphoto`` is the fallback for Tk builds
        that cannot read an ``.ico``.
        """
        icon = paths.icon_path()
        try:
            if icon.is_file():
                self.iconbitmap(default=str(icon))
                return
        except Exception:  # noqa: BLE001 - cosmetic only
            pass
        logo = _load_image(paths.logo_path(32), self)
        if logo is not None:
            self._images["window"] = logo
            try:
                self.iconphoto(True, logo)
            except Exception:  # noqa: BLE001 - cosmetic only
                pass

    def _header_logo(self) -> None:
        """Logo + wordmark row above the save selector."""
        header = ttk.Frame(self, padding=(8, 8, 8, 0))
        header.pack(fill=tk.X)

        logo = _load_image(paths.logo_path(64), self)
        if logo is not None:
            self._images["header"] = logo
            ttk.Label(header, image=logo).pack(side=tk.LEFT)
        else:
            ttk.Label(header, text="勾玉", font=("Yu Mincho", 20)).pack(side=tk.LEFT)

        text = ttk.Frame(header, padding=(10, 0, 0, 0))
        text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(text, text=TITLE, font=("Microsoft YaHei UI", 13, "bold"),
                  foreground="#1b2233").pack(anchor=tk.W)
        ttk.Label(text, text=SUBTITLE, foreground="#666666").pack(anchor=tk.W)

    # ------------------------------------------------------------------ UI

    def _scrollable(self, parent: tk.Widget, *, width: int,
                    register: bool = True) -> ttk.Frame:
        """A vertically scrolling viewport inside ``parent``; returns its content.

        pack() clips whatever does not fit, which silently hid the lower rows of
        the editor column on a short window, so the column scrolls instead.
        ``register=False`` leaves ``scroll_columns`` (the 饰品 / 魂核 columns the
        window wires at the end of :meth:`_build_ui`) untouched: a self-contained
        tab wires its own column with :meth:`_bind_wheel_to_children`.
        """
        canvas = tk.Canvas(parent, highlightthickness=0, borderwidth=0, width=width)
        bar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        bar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        inner = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def resize(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(window, width=canvas.winfo_width())

        def wheel(event) -> None:
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

        inner.bind("<Configure>", resize)
        canvas.bind("<Configure>", resize)
        canvas.bind("<MouseWheel>", wheel)
        inner.bind("<MouseWheel>", wheel)
        if register:
            self.scroll_columns.append((canvas, inner))
        return inner

    def _bind_wheel_to_children(self, widget: tk.Widget, canvas: tk.Canvas) -> None:
        """Route the wheel to the column's canvas while the pointer is inside it."""
        def wheel(event) -> None:
            canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

        for child in widget.winfo_children():
            child.bind("<MouseWheel>", wheel, add="+")
            self._bind_wheel_to_children(child, canvas)

    def _build_ui(self) -> None:
        self._header_logo()

        top = ttk.Frame(self, padding=(8, 6))
        top.pack(side=tk.TOP, fill=tk.X)

        ttk.Label(top, text="存档:").pack(side=tk.LEFT)
        self.save_combo = ttk.Combobox(top, state="readonly", width=60)
        self.save_combo.pack(side=tk.LEFT, padx=4)
        self.save_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_save_selected())
        ttk.Button(top, text="刷新", command=self.refresh_saves).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="读取数据", command=self.load_accessories).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="备份存档", command=self.backup_save).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="恢复备份", command=self.restore_save).pack(side=tk.LEFT, padx=2)

        # Everything below the record list is packed from the *bottom* edge, in
        # reverse visual order, before the expanding middle section.  pack()
        # hands out space in call order, so a bar packed with side=BOTTOM keeps
        # its height no matter how small the window is; previously a long status
        # text in the same row pushed 应用修改/写入存档 off the right edge and a
        # short window squeezed them to zero height.
        ttk.Label(self, text=DISCLAIMER, foreground="#b30000",
                  justify=tk.LEFT).pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=4)

        footer = ttk.Frame(self, padding=(8, 2))
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        self.version_var = tk.StringVar(value=self._version_short())
        ttk.Label(footer, textvariable=self.version_var, foreground="#555555",
                  justify=tk.LEFT, font=("Consolas", 8)).pack(side=tk.LEFT)
        ttk.Button(footer, text="版本信息",
                   command=self.show_version_info).pack(side=tk.RIGHT, padx=4)

        ttk.Separator(self).pack(side=tk.BOTTOM, fill=tk.X)

        # Always visible (not only in the confirmation dialog): the write target
        # is the save on disk, so the game must not hold that save in memory.
        self.write_requirement_var = tk.StringVar(value=WRITE_REQUIREMENT_SHORT)
        ttk.Label(self, textvariable=self.write_requirement_var, foreground="#b03030",
                  wraplength=960, justify=tk.LEFT).pack(side=tk.BOTTOM, fill=tk.X,
                                                        padx=8, pady=(2, 0))

        # Live game-process state: the one condition that decides whether a write
        # is even allowed, so it is reported continuously instead of only after a
        # refused write.
        self.game_status_var = tk.StringVar(value=GAME_STATUS_UNKNOWN)
        self.game_status_label = ttk.Label(self, textvariable=self.game_status_var,
                                           foreground="#666666")
        self.game_status_label.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(2, 0))

        # Writing while the game runs is only safe from its title screen: the game
        # has not loaded the save into memory yet, so it cannot overwrite the file.
        # Requiring this box (auto-cleared after every write) turns "the game is
        # running" from a refusal into an explicit, checked acknowledgement.
        self.title_screen_var = tk.BooleanVar(value=False)
        self.title_screen_check = ttk.Checkbutton(
            self, variable=self.title_screen_var, takefocus=True,
            text="我确认：游戏正在运行，但停留在标题界面（尚未载入存档）→ 允许写入",
            command=self._on_title_screen_toggled,
        )
        self.title_screen_check.pack(side=tk.BOTTOM, fill=tk.X, padx=8)

        controls = ttk.Frame(self, padding=(8, 2))
        controls.pack(side=tk.BOTTOM, fill=tk.X)
        self.controls = controls
        self.dry_run_var = tk.BooleanVar(value=self.config.default_dry_run)
        ttk.Checkbutton(controls, text="仅演练（不写回）",
                        variable=self.dry_run_var).pack(side=tk.LEFT, padx=6)
        self.verify_var = tk.BooleanVar(value=self.config.default_verify)
        ttk.Checkbutton(controls, text="写入校验",
                        variable=self.verify_var).pack(side=tk.LEFT, padx=6)
        self.apply_button = ttk.Button(controls, text="应用修改",
                                       command=self.apply_current_tab_edits)
        self.apply_button.pack(side=tk.LEFT, padx=2)
        self.write_button = ttk.Button(controls, text="写入存档", command=self.write_save)
        self.write_button.pack(side=tk.LEFT, padx=2)

        # Status line for the last action gets its own row (wrapping, so a long
        # message cannot displace any control) and the record-table detail goes to
        # a second, quieter row.
        self.status_var = tk.StringVar(
            value=self._backend_note if self._backend_note else "就绪"
        )
        self.status_label = ttk.Label(self, textvariable=self.status_var,
                                     foreground="#0366d6", wraplength=960,
                                     justify=tk.LEFT)
        self.status_label.pack(side=tk.BOTTOM, fill=tk.X, padx=8)
        self.table_var = tk.StringVar(value="")
        self.table_label = ttk.Label(self, textvariable=self.table_var,
                                     foreground="#666666", wraplength=960,
                                     justify=tk.LEFT)
        self.table_label.pack(side=tk.BOTTOM, fill=tk.X, padx=8)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self.accessory_tab = ttk.Frame(self.notebook, padding=(2, 2))
        self.soul_tab = ttk.Frame(self.notebook, padding=(2, 2))
        self.notebook.add(self.accessory_tab, text="饰品")
        self.notebook.add(self.soul_tab, text="魂核（魂之核）")
        # 武器 / 防具：与饰品页签同一套操作（列表 -> 逐槽编辑 -> 预览 -> 应用
        # -> 写入副本），只是候选词条按这件装备自己的词条池与装备种类标签过滤。
        for big, label in EQUIPMENT_TAB_KINDS:
            tab = EquipmentTab(self.notebook, self, big=big, title=label)
            if self.equipment_error or not self.equipment_item_db.is_loaded:
                tab.set_error(f"{label}页签只读：随包的物品/词条表不可用"
                              f"（{self.equipment_error or '未随包提供'}）")
            self.notebook.add(tab, text=label)
            self.equipment_tabs.append(tab)

        mid = ttk.Panedwindow(self.accessory_tab, orient=tk.HORIZONTAL)
        mid.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=4)

        left = ttk.Frame(mid)
        ttk.Label(left, text="饰品记录（选择后编辑右侧词条槽）").pack(anchor=tk.W)
        # 需求(3): 读取后按种类 / 恩宠·套装筛选。
        filter_row = ttk.Frame(left)
        filter_row.pack(fill=tk.X, pady=(2, 2))
        ttk.Label(filter_row, text="筛选 种类:").pack(side=tk.LEFT)
        self.kind_filter_var = tk.StringVar(value=self.ALL_FILTER)
        self.kind_filter_combo = ttk.Combobox(
            filter_row, state="readonly", width=24, textvariable=self.kind_filter_var,
            values=(self.ALL_FILTER,) + self.item_db.labels())
        self.kind_filter_combo.pack(side=tk.LEFT, padx=2)
        ttk.Label(filter_row, text="恩宠/套装:").pack(side=tk.LEFT)
        self.grace_filter_var = tk.StringVar(value=self.ALL_FILTER)
        self.grace_filter_combo = ttk.Combobox(
            filter_row, state="readonly", width=20, textvariable=self.grace_filter_var,
            values=(self.ALL_FILTER,) + tuple(entry.name for entry in self.grace_db.all()))
        self.grace_filter_combo.pack(side=tk.LEFT, padx=2)
        ttk.Button(filter_row, text="清除筛选",
                   command=self.clear_filters).pack(side=tk.LEFT, padx=2)
        ttk.Label(filter_row, text="武士/忍者:").pack(side=tk.LEFT)
        self.school_filter_var = tk.StringVar(value=self.ALL_FILTER)
        self.school_filter_combo = ttk.Combobox(
            filter_row, state="readonly", width=12, textvariable=self.school_filter_var,
            values=(self.ALL_FILTER,) + self._school_filter_values())
        self.school_filter_combo.pack(side=tk.LEFT, padx=2)
        self.school_filter_combo.bind("<<ComboboxSelected>>",
                                      lambda _event: self._refresh_accessory_tree())
        self.kind_filter_combo.bind("<<ComboboxSelected>>",
                                    lambda _event: self._refresh_accessory_tree())
        self.grace_filter_combo.bind("<<ComboboxSelected>>",
                                     lambda _event: self._refresh_accessory_tree())
        self.tree = ttk.Treeview(
            left, columns=("level", "plus", "rarity", "type"), show="tree headings",
            height=16,
        )
        self.tree.heading("#0", text="记录")
        self.tree.heading("level", text="等级")
        self.tree.heading("plus", text="+值")
        self.tree.heading("rarity", text="品质")
        self.tree.heading("type", text="种类（只读）")
        self.tree.column("level", width=52, anchor=tk.CENTER)
        self.tree.column("plus", width=44, anchor=tk.CENTER)
        self.tree.column("rarity", width=76, anchor=tk.CENTER)
        self.tree.column("type", width=190, anchor=tk.W)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._on_accessory_selected())
        self.filter_status_var = tk.StringVar(
            value="筛选会级联：选了 种类 后，恩宠/套装 只列出该种类里出现过的；反之亦然")
        ttk.Label(left, textvariable=self.filter_status_var, foreground="#666666",
                  wraplength=380, justify=tk.LEFT).pack(anchor=tk.W, pady=(2, 0))
        mid.add(left, weight=2)

        right_scroll = ttk.Frame(mid)
        right = self._scrollable(right_scroll, width=540)
        ttk.Label(right, text="词条槽（下拉选择；也可在某一槽里输入关键词，"
                              "只缩小该槽的下拉列表）").pack(anchor=tk.W)
        # 需求(4), 每槽独立：关键词过滤发生在**该槽自己的**下拉列表上。
        self.search_status_var = tk.StringVar(value=self._affix_search_hint())
        self.search_status_label = ttk.Label(
            right, textvariable=self.search_status_var, foreground="#1a4f8f",
            wraplength=520, justify=tk.LEFT)
        self.search_status_label.pack(anchor=tk.W)
        slot_frame = ttk.Frame(right)
        slot_frame.pack(fill=tk.BOTH, expand=True)
        self.slot_combos: list[ttk.Combobox] = []
        self.slot_labels: list[tk.StringVar] = []
        self.value_vars: list[tk.StringVar] = []
        self.value_entries: list[ttk.Entry] = []
        for index in range(EFFECT_COUNT):
            # Two lines per slot: 槽N + the combo on top, 数值 + the hint below it.
            # A single packed row used to push the 数值 box out of the visible area.
            box = ttk.Frame(slot_frame)
            box.pack(fill=tk.X, pady=(2, 4))
            top = ttk.Frame(box)
            top.pack(fill=tk.X)
            ttk.Label(top, text=f"槽{index + 1}:", width=5).pack(side=tk.LEFT)
            combo = ttk.Combobox(top, width=46, values=self.affix_db.labels())
            combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
            combo.bind("<<ComboboxSelected>>",
                       lambda _event, slot=index: self._on_slot_picked(slot))
            combo.bind("<KeyRelease>",
                       lambda _event, slot=index: self._on_slot_typed(slot))
            combo.bind("<Return>",
                       lambda _event, slot=index: self._on_slot_return(slot))
            self.slot_combos.append(combo)
            self.slot_labels.append(tk.StringVar(value=""))
            bottom = ttk.Frame(box)
            bottom.pack(fill=tk.X)
            ttk.Label(bottom, text="数值:", width=5).pack(side=tk.LEFT)
            value_var = tk.StringVar(value="")
            entry = ttk.Entry(bottom, textvariable=value_var, width=7)
            entry.pack(side=tk.LEFT)
            self.value_vars.append(value_var)
            self.value_entries.append(entry)
            ttk.Label(bottom, textvariable=self.slot_labels[index],
                      foreground="#666666", wraplength=360,
                      justify=tk.LEFT).pack(side=tk.LEFT, padx=6)

        note = ttk.Label(
            right,
            text=("说明：词条选择来自《仁王3词条装备库v2.21》饰品词条表，"
                  "非表内词条一律拒绝。选择词条会写入该词条的 ID 与标称数值；"
                  "标识(metadata) 位不会被改写，因为其在存档中的编码尚未核实。\n"
                  "同名固定词条（表里标着「(同名固定)」、存档标识第 9 字节带 0x40 位）"
                  "是该饰品固有的一部分，**禁止修改**——它随「种类」决定，"
                  "改种类时会自动同步。\n"
                  "每件饰品的最后一个词条通常是「恩宠」或「套装/专属套装」词条："
                  "恩宠（xxx的恩宠）可以用下面的【恩宠】栏改成另一个恩宠；"
                  "套装/专属套装（如 怨恨盖世）按规则不允许改动。\n"
                  "「种类」一栏由《仁王3词条装备库v2.21》物品总目录的饰品条目解析得到。"
                  f"等级可以改（上限 {limits.LEVEL_CAP}）；「+值」也可以改（字段 +0x0A，"
                  "已由游戏内实测确认：存档字节与物品卡显示的 +13/+18/+19 一致，"
                  f"合法范围 0..{MAX_RECORD_PLUS}，只写这一个字段）。"),
            foreground="#666666", wraplength=520, justify=tk.LEFT,
        )
        note.pack(anchor=tk.W, pady=(6, 0))

        self.level_frame = ttk.LabelFrame(right, text=f"等级（上限 {limits.LEVEL_CAP}）",
                                          padding=(6, 4))
        self.level_frame.pack(fill=tk.X, pady=(6, 0))
        level_row = ttk.Frame(self.level_frame)
        level_row.pack(fill=tk.X)
        ttk.Label(level_row, text="改成:").pack(side=tk.LEFT)
        self.level_var = tk.StringVar(value="")
        self.level_entry = ttk.Entry(level_row, textvariable=self.level_var, width=8)
        self.level_entry.pack(side=tk.LEFT, padx=4)
        self.level_button = ttk.Button(level_row, text="应用等级",
                                       command=self.apply_level_to_selection)
        self.level_button.pack(side=tk.LEFT, padx=2)
        self.level_status_var = tk.StringVar(
            value="选择一条饰品记录后，这里会显示它的等级是否可以修改。")
        self.level_status_label = ttk.Label(
            self.level_frame, textvariable=self.level_status_var,
            foreground="#666666", wraplength=520, justify=tk.LEFT,
        )
        self.level_status_label.pack(anchor=tk.W, pady=(4, 0))
        self._set_level_enabled(False)

        # +值: the item card's "+N", confirmed in game to be record byte +0x0A.
        self.plus_frame = ttk.LabelFrame(
            right, text=f"+值（0..{MAX_RECORD_PLUS}，字段 +0x0A）", padding=(6, 4))
        self.plus_frame.pack(fill=tk.X, pady=(6, 0))
        plus_row = ttk.Frame(self.plus_frame)
        plus_row.pack(fill=tk.X)
        ttk.Label(plus_row, text="改成:").pack(side=tk.LEFT)
        self.plus_var = tk.StringVar(value="")
        self.plus_entry = ttk.Entry(plus_row, textvariable=self.plus_var, width=8)
        self.plus_entry.pack(side=tk.LEFT, padx=4)
        self.plus_button = ttk.Button(plus_row, text="应用 +值",
                                      command=self.apply_plus_to_selection)
        self.plus_button.pack(side=tk.LEFT, padx=2)
        self.plus_status_var = tk.StringVar(
            value="选择一条饰品记录后，这里会显示它的 +值。")
        self.plus_status_label = ttk.Label(
            self.plus_frame, textvariable=self.plus_status_var,
            foreground="#666666", wraplength=520, justify=tk.LEFT,
        )
        self.plus_status_label.pack(anchor=tk.W, pady=(4, 0))
        self._set_plus_enabled(False)

        # 稀有度（品质）: 字段 +0x30；范围按记录所属大类取（饰品 0..4）。
        self.rarity_frame = ttk.LabelFrame(
            right, text=f"稀有度（0..{limits.rarity_cap('饰品')}，字段 +0x30）",
            padding=(6, 4))
        self.rarity_frame.pack(fill=tk.X, pady=(6, 0))
        rarity_row = ttk.Frame(self.rarity_frame)
        rarity_row.pack(fill=tk.X)
        ttk.Label(rarity_row, text="改成:").pack(side=tk.LEFT)
        self.rarity_var = tk.StringVar(value="")
        self.rarity_entry = ttk.Entry(rarity_row, textvariable=self.rarity_var, width=8)
        self.rarity_entry.pack(side=tk.LEFT, padx=4)
        self.rarity_button = ttk.Button(rarity_row, text="应用稀有度",
                                        command=self.apply_rarity_to_selection)
        self.rarity_button.pack(side=tk.LEFT, padx=2)
        self.rarity_status_var = tk.StringVar(
            value="选择一条饰品记录后，这里会显示它的稀有度。")
        self.rarity_status_label = ttk.Label(
            self.rarity_frame, textvariable=self.rarity_status_var,
            foreground="#666666", wraplength=520, justify=tk.LEFT,
        )
        self.rarity_status_label.pack(anchor=tk.W, pady=(4, 0))
        self._set_rarity_enabled(False)

        self.kind_frame = ttk.LabelFrame(right, text="种类（同分类互换）", padding=(6, 4))
        self.kind_frame.pack(fill=tk.X, pady=(6, 0))
        kind_row = ttk.Frame(self.kind_frame)
        kind_row.pack(fill=tk.X)
        ttk.Label(kind_row, text="改成:").pack(side=tk.LEFT)
        self.kind_combo = ttk.Combobox(kind_row, state="readonly", width=40,
                                       values=self.item_db.labels())
        self.kind_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self.kind_button = ttk.Button(kind_row, text="应用种类",
                                      command=self.apply_kind_to_selection)
        self.kind_button.pack(side=tk.LEFT, padx=2)
        self.kind_status_var = tk.StringVar(
            value="选择一条饰品记录后，这里会显示它能换成哪些同分类的种类。")
        self.kind_status_label = ttk.Label(
            self.kind_frame, textvariable=self.kind_status_var,
            foreground="#666666", wraplength=520, justify=tk.LEFT,
        )
        self.kind_status_label.pack(anchor=tk.W, pady=(4, 0))
        self.kind_choices: dict[str, object] = {}
        self._set_kind_enabled(False)

        # 需求(5): 无中生有 —— 用当前槽位里选好的词条新建一件，写进空槽。
        self.create_frame = ttk.LabelFrame(
            right, text="无中生有（新建一件放进空槽）", padding=(6, 4))
        self.create_frame.pack(fill=tk.X, pady=(6, 0))
        create_row = ttk.Frame(self.create_frame)
        create_row.pack(fill=tk.X)
        ttk.Label(create_row, text="种类:").pack(side=tk.LEFT)
        self.create_kind_combo = ttk.Combobox(create_row, state="readonly", width=30,
                                              values=self.item_db.labels())
        self.create_kind_combo.pack(side=tk.LEFT, padx=4)
        ttk.Label(create_row, text="等级:").pack(side=tk.LEFT)
        self.create_level_var = tk.StringVar(value=str(MAX_ITEM_LEVEL))
        ttk.Entry(create_row, textvariable=self.create_level_var,
                  width=6).pack(side=tk.LEFT, padx=2)
        self.create_button = ttk.Button(create_row, text="新建到空槽",
                                        command=self.create_item)
        self.create_button.pack(side=tk.LEFT, padx=4)
        self.create_status_var = tk.StringVar(
            value="用法：先选中一件同类饰品作为模板，再在右侧槽位里挑好词条，"
                  "然后在这里选种类并点「新建到空槽」。")
        ttk.Label(self.create_frame, textvariable=self.create_status_var,
                  foreground="#666666", wraplength=560,
                  justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))

        self.grace_frame = ttk.LabelFrame(right, text="恩宠（末位槽）", padding=(6, 4))
        self.grace_frame.pack(fill=tk.X, pady=(6, 0))
        grace_row = ttk.Frame(self.grace_frame)
        grace_row.pack(fill=tk.X)
        ttk.Label(grace_row, text="改成:").pack(side=tk.LEFT)
        self.grace_combo = ttk.Combobox(grace_row, state="readonly", width=40,
                                        values=self.grace_db.labels())
        self.grace_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self.grace_button = ttk.Button(grace_row, text="应用恩宠",
                                       command=self.apply_grace_to_selection)
        self.grace_button.pack(side=tk.LEFT, padx=2)
        self.grace_status_var = tk.StringVar(
            value="选择一条饰品记录后，这里会显示它的末位恩宠是否可改。")
        self.grace_status_label = ttk.Label(
            self.grace_frame, textvariable=self.grace_status_var,
            foreground="#666666", wraplength=520, justify=tk.LEFT,
        )
        self.grace_status_label.pack(anchor=tk.W, pady=(4, 0))
        self._set_grace_enabled(False)

        # 种类 (the item itself): same-category swaps only, with the target kind's
        # fixed affix copied from a real sample in this save.
        self.item_var = tk.StringVar(
            value="选择一条饰品记录后，这里会显示它是什么饰品。")
        self.item_label = ttk.Label(right, textvariable=self.item_var,
                                    foreground="#1a4f8f", wraplength=520,
                                    justify=tk.LEFT)
        self.item_label.pack(anchor=tk.W, pady=(6, 0))

        mid.add(right_scroll, weight=3)

        self._build_soul_tab()
        # Both editor columns exist now: let the wheel scroll whichever one the
        # pointer is over (the scrollbar works regardless).
        for canvas, inner in self.scroll_columns:
            self._bind_wheel_to_children(inner, canvas)

    # ------------------------------------------------- 筛选 / 关键词 / 新建
    def clear_filters(self) -> None:
        """需求(3): drop every filter and show all accessories again."""
        self.kind_filter_var.set(self.ALL_FILTER)
        self.grace_filter_var.set(self.ALL_FILTER)
        self.school_filter_var.set(self.ALL_FILTER)
        self._refresh_accessory_tree()

    #: 「武士 / 忍者」在物品总目录里的类别是 武士饰品 / 忍者饰品。
    _SCHOOL_SUFFIX = "饰品"

    def _school_of(self, view: AccessoryView) -> str:
        """该记录属于武士还是忍者（取自物品总目录的类别）。"""
        entry = self.item_db.lookup(view.record_type)
        category = (getattr(entry, "category", "") or "") if entry is not None else ""
        return category.replace(self._SCHOOL_SUFFIX, "").strip()

    def _school_filter_values(self) -> tuple[str, ...]:
        """筛选下拉的取值：物品总目录里出现过的类别（武士 / 忍者 …）。"""
        values: list[str] = []
        for entry in self.item_db.all():
            label = (getattr(entry, "category", "") or "").replace(
                self._SCHOOL_SUFFIX, "").strip()
            if label and label not in values:
                values.append(label)
        return tuple(values)

    def _school_filter_passes(self, view: AccessoryView) -> bool:
        choice = self.school_filter_var.get()
        if choice in ("", self.ALL_FILTER):
            return True
        return self._school_of(view) == choice

    def _kind_filter_passes(self, view: AccessoryView) -> bool:
        choice = self.kind_filter_var.get()
        if choice in ("", self.ALL_FILTER):
            return True
        entry = self.item_choices.get(choice)
        return entry is not None and entry.item_id == view.record_type

    def _grace_filter_passes(self, view: AccessoryView) -> bool:
        choice = self.grace_filter_var.get()
        if choice in ("", self.ALL_FILTER):
            return True
        entry = self.grace_choices.get(choice)
        if entry is None:
            return False
        return any(effect.effect_id == entry.effect_id
                   for effect in view.occupied_effects)

    def _grace_slot_indexes(self, view) -> frozenset[int]:
        """Slots that really hold an 恩宠/套装 affix (id known to 词条总目录).

        :meth:`AccessoryView.grace_slots` can only say "a trailing slot outside the
        affix table", which also covers an unknown-but-ordinary affix such as 0x30fe.
        Those must stay editable in their own slot — the 恩宠/套装 table is what makes
        a slot a grace slot, so it is what decides here.
        """
        return frozenset(
            index for index in view.grace_slots(self.affix_db)
            if index < len(view.effects)
            and self.grace_db.describe(view.effects[index].effect_id)
        )

    def _cascade_filter_values(self) -> None:
        """需求(3): each filter offers only what the *other two* still allow.

        With 种类 = 八尺琼勾玉[武士] picked, the 恩宠/套装 list shrinks to the graces
        those records actually carry and 武士/忍者 shrinks to what is left (and every
        other pair behaves the same way), so a combination that cannot exist is not
        offered in the first place.  A current choice that is no longer
        offered is kept in the list and left selected, so an empty result is explained
        by the filter the user set instead of being silently reset.
        """
        if not hasattr(self, "kind_filter_combo") or not self.accessory_views:
            return
        kind_pool = [view for view in self.accessory_views
                     if self._grace_filter_passes(view)
                     and self._school_filter_passes(view)]
        grace_pool = [view for view in self.accessory_views
                      if self._kind_filter_passes(view)
                      and self._school_filter_passes(view)]
        school_pool = [view for view in self.accessory_views
                       if self._kind_filter_passes(view)
                       and self._grace_filter_passes(view)]
        kinds = {view.record_type for view in kind_pool}
        graces = {effect.effect_id for view in grace_pool
                  for effect in view.occupied_effects}
        kind_values = [self.ALL_FILTER] + [
            label for label, entry in self.item_choices.items() if entry.item_id in kinds]
        grace_values = [self.ALL_FILTER] + [
            name for name, entry in self.grace_choices.items()
            if entry.effect_id in graces]
        schools = {self._school_of(view) for view in school_pool}
        school_values = [self.ALL_FILTER] + [
            label for label in self._school_filter_values() if label in schools]
        choice = self.kind_filter_var.get()
        if choice and choice != self.ALL_FILTER and choice not in kind_values:
            kind_values.append(f"{choice}（当前无记录）")
        self.kind_filter_combo.configure(values=kind_values)
        choice = self.grace_filter_var.get()
        if choice and choice != self.ALL_FILTER and choice not in grace_values:
            grace_values.append(f"{choice}（当前无记录）")
        self.grace_filter_combo.configure(values=grace_values)
        choice = self.school_filter_var.get()
        if choice and choice != self.ALL_FILTER and choice not in school_values:
            school_values.append(f"{choice}（当前无记录）")
        self.school_filter_combo.configure(values=school_values)

    def _refresh_accessory_tree(self) -> None:
        """Re-fill the tree from ``self.accessory_views`` honouring the filters."""
        self.tree.delete(*self.tree.get_children())
        shown = [view for view in self.accessory_views
                 if self._kind_filter_passes(view) and self._grace_filter_passes(view)
                 and self._school_filter_passes(view)]
        self._cascade_filter_values()
        for view in shown:
            # 种类 = the item this record is, named from 物品总目录 when available
            # (display only).  Falls back to the raw id plus the catalog evidence.
            label = self._kind_text(view)
            if view.catalog_hits is not None:
                label += f" 词条命中 {view.catalog_hits}"
            self.tree.insert(
                "", "end", iid=str(view.slot_index),
                text=f"#{view.slot_index} @ {view.offset:#x}",
                # Tk normalises a numeric-looking cell ("+5" comes back as "5"), so
                # the meaning rides on the column header and the cell is a plain number.
                values=(view.level, str(view.plus_value),
                        rarity_label(view.rarity, view.rarity_name), label),
            )
        total = len(self.accessory_views)
        if self.kind_filter_var.get() in ("", self.ALL_FILTER) and \
                self.grace_filter_var.get() in ("", self.ALL_FILTER):
            self.filter_status_var.set(f"共 {total} 件饰品（未筛选）")
        else:
            self.filter_status_var.set(f"筛选后 {len(shown)} / {total} 件饰品")

    def _kind_text(self, view: AccessoryView) -> str:
        """``八尺琼勾玉[武士]`` when the item table knows the id, else the id."""
        named = self.item_db.describe(view.record_type)
        return named or f"{view.kind_name} {view.record_type:#06x}"

    def _slot_choice(self, index: int):
        """The catalog entry behind one slot combo (``None`` for 空/未选)."""
        text = self._resolve_slot_text(index)
        if not text or text == EMPTY_LABEL:
            return None
        entry = self.affix_by_label.get(text)
        if entry is None:
            # Fixed slots show a suffixed label; they are never editable anyway.
            raise EditorError(f"槽{index + 1} 的词条不在当前词条库内，请重新选择")
        return entry

    def _fill_value_boxes(self, view, affix_db: AffixDb,
                          boxes: list[tk.StringVar]) -> None:
        """Put each slot's current 数值 in its box and say what the span is.

        需求(2): the box is pre-filled with what the save holds, and the slot label
        shows the legal span from the workbook, so an out-of-range value is visible
        before it is written (the engine refuses it anyway).
        """
        for index, effect in enumerate(view.effects):
            if index >= len(boxes):
                break
            if effect.is_empty:
                boxes[index].set("")
                continue
            boxes[index].set(str(effect.value))
            entry = affix_db.lookup(effect.effect_id)
            if entry is not None and not view.slot_is_fixed(index, affix_db):
                self.slot_labels[index].set(
                    f"数值={effect.value} 标识={effect.metadata:#010x} "
                    f"（可改区间 {entry.describe_value_range()}）")
        # 记录自带的槽位可能少于控件数（武器恒 5 槽、防具 4..6 槽）：尾部必须清空，
        # 否则会留下上一条记录的数值 —— 切记录时看起来就是"数值框没跟着换"。
        for index in range(len(view.effects), len(boxes)):
            boxes[index].set("")

    def _selected_accessory(self) -> AccessoryView | None:
        if self.selected_accessory is None:
            return None
        return next((view for view in self.accessory_views
                     if view.slot_index == self.selected_accessory), None)

    def _affix_search_hint(self) -> str:
        return (f"共 {len(self.affix_db)} 条合法词条；在某一槽里输入关键词只缩小"
                "该槽的下拉列表，别的槽不受影响。空格分隔多个关键词＝必须同时包含"
                "（如「星 恢复」）")

    def _filter_slot(self, index: int, keyword: str) -> int:
        """Point **one** slot's dropdown at the matches; return how many there are.

        This is deliberately per slot: the old 搜索词条 rewrote every slot's
        ``values`` in one go, and a ``readonly`` ttk.Combobox silently blanks its
        display when the current value leaves ``values`` — which is exactly how an
        already-chosen affix (or a 固定 slot's suffixed label) vanished from the
        boxes until the record was re-selected.
        """
        combo = self.slot_combos[index]
        keyword = keyword.strip()
        if not keyword:
            combo.configure(values=self.affix_db.labels())
            return len(self.affix_db)
        matches = self.affix_db.search(keyword)
        combo.configure(values=(EMPTY_LABEL,)
                        + tuple(entry.label for entry in matches))
        return len(matches)

    def _on_slot_typed(self, index: int) -> None:
        """Live, slot-local keyword filter: 需求(4) without touching other slots."""
        typed = self.slot_combos[index].get()
        if typed in self.affix_by_label or typed == EMPTY_LABEL:
            return  # an exact pick from the list: leave the list alone
        count = self._filter_slot(index, typed)
        if not typed.strip():
            self.search_status_var.set(self._affix_search_hint())
        elif count:
            self.search_status_var.set(
                f"槽{index + 1}: 「{typed.strip()}」匹配 {count} 条，"
                "展开该槽的下拉列表选择（其它槽不受影响）")
        else:
            self.search_status_var.set(
                f"槽{index + 1}: 「{typed.strip()}」没有匹配到任何词条 —— "
                "换个更短的关键词，或直接写词条 id（如 0x0b32）")

    def _on_slot_return(self, index: int) -> None:
        """Enter takes the match when the typed keyword is unambiguous."""
        typed = self.slot_combos[index].get().strip()
        if not typed or typed == EMPTY_LABEL or typed in self.affix_by_label:
            return
        matches = self.affix_db.search(typed)
        if len(matches) == 1:
            self.slot_combos[index].set(matches[0].label)
            self._on_slot_picked(index)
            self.slot_combos[index].configure(values=self.affix_db.labels())
            self.search_status_var.set(
                f"槽{index + 1}: 已选中 {matches[0].label}")
        elif matches:
            self._filter_slot(index, typed)
            self.search_status_var.set(
                f"槽{index + 1}: 「{typed}」匹配 {len(matches)} 条，"
                "请从该槽的下拉列表里选一条")
        else:
            self._filter_slot(index, typed)
            self.search_status_var.set(
                f"槽{index + 1}: 「{typed}」没有匹配到任何词条")

    def _reset_affix_lists(self) -> None:
        """Every slot gets the full catalog back (called when a record is picked)."""
        for combo in self.slot_combos:
            combo.configure(values=self.affix_db.labels())
        self.search_status_var.set(self._affix_search_hint())

    @staticmethod
    def _entry_from_id_text(text: str, db):
        """Resolve a typed id like the CLI does: ``0x646b``, ``646b`` or ``25707``."""
        for base in (16, 10):
            try:
                value = int(text, base)
            except ValueError:
                continue
            entry = db.lookup(value)
            if entry is not None:
                return entry
        return None

    def _resolve_slot_text(self, index: int) -> str:
        """The slot's text, resolved to a shipped label when it is unambiguous."""
        if "disabled" in self.slot_combos[index].state():
            return ""  # a disabled box only ever shows a 固定 slot: nothing to edit
        typed = self.slot_combos[index].get().strip()
        if not typed or typed == EMPTY_LABEL or typed in self.affix_by_label:
            return typed
        matches = self.affix_db.search(typed)
        if len(matches) == 1:
            self.slot_combos[index].set(matches[0].label)
            return matches[0].label
        by_id = self._entry_from_id_text(typed, self.affix_db)
        if by_id is not None:
            self.slot_combos[index].set(by_id.label)
            return by_id.label
        raise EditorError(
            f"槽{index + 1} 的文本不是词条表里的词条：{typed!r}"
            f"（关键词匹配 {len(matches)} 条，请从该槽下拉列表里选一条）")

    def _slot_value(self, index: int):
        """The 数值 the user typed for one slot, or ``None`` to use the catalog's."""
        text = self.value_vars[index].get().strip()
        if not text:
            return None
        try:
            return int(text, 10)
        except ValueError as error:
            raise EditorError(
                f"槽{index + 1} 的数值必须是整数，实际 {text!r}") from error

    def create_item(self) -> None:
        """需求(5): create a new item from the slots' current choices."""
        if self.decrypted is None:
            messagebox.showwarning("提示", "请先读取存档")
            return
        chosen = self.create_kind_combo.get()
        item = self.item_choices.get(chosen)
        if item is None:
            messagebox.showwarning("提示", "请先选择要新建的种类")
            return
        try:
            level = int(self.create_level_var.get().strip(), 10)
        except ValueError:
            messagebox.showwarning("提示", "等级必须是整数")
            return
        effects = []
        try:
            for index in range(EFFECT_COUNT):
                entry = self._slot_choice(index)
                if entry is None:
                    continue
                edit: dict[str, int] = {"slot_index": index,
                                        "effect_id": entry.effect_id}
                value = self._slot_value(index)
                if value is not None:
                    edit["value"] = value
                effects.append(edit)
        except EditorError as error:
            messagebox.showwarning("提示", str(error))
            return
        template = self._selected_accessory()
        if template is None or template.record_type != item.item_id:
            messagebox.showwarning(
                "提示",
                f"新建 {item.label} 前，请先在左侧选中一件**同种类**的饰品作为模板：\n"
                "新物品的固定词条与逐件字段必须从同种类的真实样本复制，"
                "本工具不会凭空猜这些字节。")
            return
        if not messagebox.askokcancel(
            "确认新建",
            f"用模板 #{template.slot_index} 新建一件 {item.label}（Lv{level}）"
            "放进第一个空槽？\n\n"
            "· 固定词条按模板带入，不能在这里改动；\n"
            "· 普通词条按你在右侧选好的内容写入，数值必须在词条区间内；\n"
            "· 背包已满时会拒绝；写入前会自动备份。",
            icon="warning",
        ):
            return
        try:
            report = find_free_slots(self.decrypted, layout=self.layout)
            plan = plan_creation(
                self.decrypted, record_type=item.item_id, level=level,
                effects=effects, affix_db=self.affix_db, item_db=self.item_db,
                known_ids=self.known_ids, layout=self.layout,
            )
            self.decrypted = apply_creations(self.decrypted, [plan])
        except EditorError as error:
            self.create_status_var.set(str(error))
            messagebox.showerror("无法新建", str(error))
            return
        self.create_status_var.set(
            f"{plan.describe(self.item_db)}（新建前空位 {report.free_count} 个）")
        self._status(f"已新建 {plan.describe(self.item_db)}（尚未写入存档）")
        self._refresh_after_edit(plan.slot_index)

    def _build_soul_tab(self) -> None:
        """The 魂核 tab: same three edits, but on the 魂核 catalog.

        A 魂核 has no 恩宠/套装 affix, so there is no 恩宠 row here — an id outside
        the 魂核 pool is refused by the engine rather than special-cased in the UI.
        """
        ttk.Label(
            self.soul_tab,
            text=("魂核（魂核没有恩宠/套装词条；同名固定词条不可修改，"
                  "改种类时按新种类的真实样本自动同步）"),
            foreground="#1a4f8f",
        ).pack(anchor=tk.W, padx=8, pady=(4, 0))
        mid = ttk.Panedwindow(self.soul_tab, orient=tk.HORIZONTAL)
        mid.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=4)

        left = ttk.Frame(mid)
        ttk.Label(left, text="魂核记录（选择后编辑右侧词条槽）").pack(anchor=tk.W)
        soul_filter = ttk.Frame(left)
        soul_filter.pack(fill=tk.X, pady=(2, 2))
        ttk.Label(soul_filter, text="筛选 种类:").pack(side=tk.LEFT)
        self.soul_kind_filter_var = tk.StringVar(value=ALL_FILTER)
        self.soul_kind_filter_combo = ttk.Combobox(
            soul_filter, state="readonly", width=30, textvariable=self.soul_kind_filter_var,
            values=(ALL_FILTER,) + self.soul_item_db.labels())
        self.soul_kind_filter_combo.pack(side=tk.LEFT, padx=2)
        ttk.Button(soul_filter, text="清除筛选",
                   command=self.clear_soul_filter).pack(side=tk.LEFT, padx=2)
        self.soul_kind_filter_combo.bind("<<ComboboxSelected>>",
                                         lambda _event: self._refresh_soul_tree())
        self.soul_filter_status_var = tk.StringVar(value="")
        ttk.Label(left, textvariable=self.soul_filter_status_var,
                  foreground="#666666", wraplength=380,
                  justify=tk.LEFT).pack(anchor=tk.W, pady=(2, 0))
        self.soul_tree = ttk.Treeview(
            left, columns=("level", "rarity", "type"), show="tree headings",
            height=16,
        )
        self.soul_tree.heading("#0", text="记录")
        self.soul_tree.heading("level", text="等级")
        self.soul_tree.heading("rarity", text="品质")
        self.soul_tree.heading("type", text="种类")
        self.soul_tree.column("#0", width=70, anchor=tk.CENTER)
        self.soul_tree.column("level", width=70, anchor=tk.CENTER)
        self.soul_tree.column("rarity", width=76, anchor=tk.CENTER)
        self.soul_tree.column("type", width=210, anchor=tk.W)
        self.soul_tree.pack(fill=tk.BOTH, expand=True)
        self.soul_tree.bind("<<TreeviewSelect>>",
                            lambda _event: self._on_soul_selected())
        mid.add(left, weight=2)

        right_scroll = ttk.Frame(mid)
        right = self._scrollable(right_scroll, width=540)
        ttk.Label(right, text="魂核词条槽（下拉选择；也可在某一槽里输入关键词，"
                              "只缩小该槽的下拉列表）").pack(anchor=tk.W)
        self.soul_search_status_var = tk.StringVar(value=self._soul_search_hint())
        self.soul_search_status_label = ttk.Label(
            right, textvariable=self.soul_search_status_var, foreground="#1a4f8f",
            wraplength=520, justify=tk.LEFT)
        self.soul_search_status_label.pack(anchor=tk.W)
        slot_frame = ttk.Frame(right)
        slot_frame.pack(fill=tk.BOTH, expand=True)
        self.soul_slot_combos: list[ttk.Combobox] = []
        self.soul_slot_labels: list[tk.StringVar] = []
        self.soul_value_vars: list[tk.StringVar] = []
        self.soul_value_entries: list[ttk.Entry] = []
        # Label -> entry, exactly like the 饰品 rows: a combo only ever holds a
        # shipped label, so an unknown label means "not a legal affix" and is
        # skipped instead of being written.
        self.soul_choices = {entry.label: entry for entry in self.soul_db.all()}
        self.soul_item_choices = {entry.label: entry
                                  for entry in self.soul_item_db.all()}
        for index in range(EFFECT_COUNT):
            box = ttk.Frame(slot_frame)
            box.pack(fill=tk.X, pady=(2, 4))
            top = ttk.Frame(box)
            top.pack(fill=tk.X)
            ttk.Label(top, text=f"槽{index + 1}:", width=5).pack(side=tk.LEFT)
            combo = ttk.Combobox(top, width=40, values=self.soul_db.labels())
            combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
            combo.bind("<<ComboboxSelected>>",
                       lambda _event, slot=index: self._on_soul_slot_picked(slot))
            combo.bind("<KeyRelease>",
                       lambda _event, slot=index: self._on_soul_slot_typed(slot))
            combo.bind("<Return>",
                       lambda _event, slot=index: self._on_soul_slot_return(slot))
            self.soul_slot_combos.append(combo)
            self.soul_slot_labels.append(tk.StringVar(value=""))
            bottom = ttk.Frame(box)
            bottom.pack(fill=tk.X)
            ttk.Label(bottom, text="数值:", width=5).pack(side=tk.LEFT)
            value_var = tk.StringVar(value="")
            soul_entry = ttk.Entry(bottom, textvariable=value_var, width=7)
            soul_entry.pack(side=tk.LEFT)
            self.soul_value_entries.append(soul_entry)
            self.soul_value_vars.append(value_var)
            ttk.Label(bottom, textvariable=self.soul_slot_labels[index],
                      foreground="#666666", wraplength=360,
                      justify=tk.LEFT).pack(side=tk.LEFT, padx=6)
        ttk.Button(right, text="应用魂核词条",
                   command=self.apply_soul_edits_to_selection).pack(anchor=tk.W,
                                                                   pady=(4, 0))

        level_frame = ttk.LabelFrame(right, text=f"魂核等级（上限 {limits.LEVEL_CAP}）",
                                     padding=(6, 4))
        level_frame.pack(fill=tk.X, pady=(6, 0))
        level_row = ttk.Frame(level_frame)
        level_row.pack(fill=tk.X)
        ttk.Label(level_row, text="改成:").pack(side=tk.LEFT)
        self.soul_level_var = tk.StringVar(value="")
        self.soul_level_entry = ttk.Entry(level_row, textvariable=self.soul_level_var,
                                          width=8)
        self.soul_level_entry.pack(side=tk.LEFT, padx=4)
        self.soul_level_button = ttk.Button(level_row, text="应用魂核等级",
                                            command=self.apply_soul_level_to_selection)
        self.soul_level_button.pack(side=tk.LEFT, padx=2)
        self.soul_level_status_var = tk.StringVar(
            value="选择一条魂核记录后，这里会显示它的等级是否可以修改。")
        ttk.Label(level_frame, textvariable=self.soul_level_status_var,
                  foreground="#666666", wraplength=520,
                  justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))

        # 稀有度（品质）：魂核的上限比其它大类低（3 = 特大名器），所以范围单独显示。
        rarity_frame = ttk.LabelFrame(
            right, text=f"魂核稀有度（0..{limits.rarity_cap('魂核')}，字段 +0x30）",
            padding=(6, 4))
        rarity_frame.pack(fill=tk.X, pady=(6, 0))
        rarity_row = ttk.Frame(rarity_frame)
        rarity_row.pack(fill=tk.X)
        ttk.Label(rarity_row, text="改成:").pack(side=tk.LEFT)
        self.soul_rarity_var = tk.StringVar(value="")
        self.soul_rarity_entry = ttk.Entry(rarity_row, textvariable=self.soul_rarity_var,
                                           width=8)
        self.soul_rarity_entry.pack(side=tk.LEFT, padx=4)
        self.soul_rarity_button = ttk.Button(rarity_row, text="应用魂核稀有度",
                                             command=self.apply_soul_rarity_to_selection)
        self.soul_rarity_button.pack(side=tk.LEFT, padx=2)
        self.soul_rarity_status_var = tk.StringVar(
            value="选择一条魂核记录后，这里会显示它的稀有度。")
        ttk.Label(rarity_frame, textvariable=self.soul_rarity_status_var,
                  foreground="#666666", wraplength=520,
                  justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))
        self._set_soul_rarity_enabled(False)

        kind_frame = ttk.LabelFrame(right, text="魂核种类（同分类互换）", padding=(6, 4))
        kind_frame.pack(fill=tk.X, pady=(6, 0))
        kind_row = ttk.Frame(kind_frame)
        kind_row.pack(fill=tk.X)
        ttk.Label(kind_row, text="改成:").pack(side=tk.LEFT)
        self.soul_kind_combo = ttk.Combobox(kind_row, state="readonly", width=36,
                                            values=self.soul_item_db.labels())
        self.soul_kind_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self.soul_kind_button = ttk.Button(kind_row, text="应用魂核种类",
                                           command=self.apply_soul_kind_to_selection)
        self.soul_kind_button.pack(side=tk.LEFT, padx=2)
        self.soul_kind_status_var = tk.StringVar(
            value="选择一条魂核记录后，这里会显示它能换成哪些同类魂核。")
        ttk.Label(kind_frame, textvariable=self.soul_kind_status_var,
                  foreground="#666666", wraplength=520,
                  justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))
        self.soul_kind_choices: dict[str, object] = {}
        self.soul_by_label = {entry.label: entry for entry in self.soul_db.all()}

        soul_create = ttk.LabelFrame(right, text="无中生有（新建一个魂核放进空槽）",
                                     padding=(6, 4))
        soul_create.pack(fill=tk.X, pady=(6, 0))
        soul_create_row = ttk.Frame(soul_create)
        soul_create_row.pack(fill=tk.X)
        ttk.Label(soul_create_row, text="种类:").pack(side=tk.LEFT)
        self.create_soul_combo = ttk.Combobox(
            soul_create_row, state="readonly", width=28,
            values=self.soul_item_db.labels())
        self.create_soul_combo.pack(side=tk.LEFT, padx=4)
        ttk.Label(soul_create_row, text="等级:").pack(side=tk.LEFT)
        self.create_soul_level_var = tk.StringVar(value=str(MAX_ITEM_LEVEL))
        ttk.Entry(soul_create_row, textvariable=self.create_soul_level_var,
                  width=6).pack(side=tk.LEFT, padx=2)
        ttk.Button(soul_create_row, text="新建到空槽",
                   command=self.create_soul_core).pack(side=tk.LEFT, padx=4)
        self.create_soul_status_var = tk.StringVar(
            value="用法：先选中一个同类魂核作为模板，再挑好右侧词条，然后在这里新建。")
        ttk.Label(soul_create, textvariable=self.create_soul_status_var,
                  foreground="#666666", wraplength=560,
                  justify=tk.LEFT).pack(anchor=tk.W, pady=(4, 0))
        # Without this the whole 魂核 editor column never appears in its panedwindow:
        # the tree showed up, the 词条槽/等级/种类/新建 rows did not.
        mid.add(right_scroll, weight=3)
        self._set_soul_controls(False)
        if self.soul_db_error:
            self.soul_kind_status_var.set(
                f"魂核词条库不可用：{self.soul_db_error}（本页只读）")

    def _apply_soul_slot_states(self) -> None:
        """数值框按「是不是固定词条槽」逐个决定：固定的不能填（词条与数值都不能改）。"""
        view = self._selected_soul()
        for index, entry in enumerate(self.soul_value_entries):
            fixed = view is not None and view.slot_is_fixed(index, self.soul_db)
            entry.state(["disabled"] if fixed else ["!disabled"])

    def _set_soul_controls(self, enabled: bool) -> None:
        state = ["!disabled"] if enabled else ["disabled"]
        if enabled:
            # 不能无脑启用：固定词条槽的数值框必须保持禁用，否则用户能填却改不了。
            self._apply_soul_slot_states()
        else:
            for entry in self.soul_value_entries:
                entry.state(state)
            # 没选记录：稀有度框清空并置灰，**不留上一条的值**。
            self.soul_rarity_var.set("")
            self.soul_rarity_status_var.set(
                "选择一条魂核记录后，这里会显示它的稀有度。")
            self._set_soul_rarity_enabled(False)
        self.soul_level_entry.state(state)
        self.soul_level_button.state(state)
        self.soul_kind_combo.state(["!disabled", "readonly"] if enabled
                                   else ["disabled"])
        self.soul_kind_button.state(state)
        self.create_button.state(state)
        self.create_soul_combo.state(["!disabled", "readonly"] if enabled
                                     else ["disabled"])

    def _populate_soul_cores(self) -> None:
        """Fill the 魂核 tree from the same loaded save bytes."""
        self.soul_tree.delete(*self.soul_tree.get_children())
        self.soul_views = []
        if self.decrypted is None or not len(self.soul_db) or \
                not self.soul_item_db.is_loaded:
            for index in range(EFFECT_COUNT):
                self.soul_slot_combos[index].set("")
                self.soul_slot_labels[index].set("")
                self.soul_value_vars[index].set("")
                self.soul_value_entries[index].state(["disabled"])
            return
        known_ids = soul_catalog_ids(self.soul_db)
        try:
            layout = inspect_layout(self.decrypted, known_ids=known_ids)
        except RecordError:
            return
        self.soul_layout = layout
        self.soul_known_ids = known_ids
        views = list_soul_cores(self.decrypted, soul_db=self.soul_db,
                                 soul_item_db=self.soul_item_db, layout=layout,
                                 known_ids=known_ids)
        self.soul_views = list(views)
        self._refresh_soul_tree()

    def _refresh_soul_tree(self) -> None:
        """需求(3): the 魂核 tree honours its 种类 filter."""
        self.soul_tree.delete(*self.soul_tree.get_children())
        choice = self.soul_kind_filter_var.get()
        chosen = self.soul_item_choices.get(choice)
        shown = [view for view in self.soul_views
                 if not view.unidentified
                 and (chosen is None or view.record_type == chosen.item_id)]
        for view in shown:
            self.soul_tree.insert(
                "", tk.END, iid=str(view.slot_index), text=f"#{view.slot_index}",
                values=(f"Lv{view.level}",
                        rarity_label(view.rarity, view.rarity_name),
                        view.describe_item(self.soul_item_db)),
            )
        total = sum(1 for view in self.soul_views if not view.unidentified)
        if chosen is None:
            self.soul_filter_status_var.set(f"共 {total} 个魂核（未筛选）")
        else:
            self.soul_filter_status_var.set(f"筛选后 {len(shown)} / {total} 个魂核")

    def clear_soul_filter(self) -> None:
        self.soul_kind_filter_var.set(ALL_FILTER)
        self._refresh_soul_tree()

    def _soul_search_hint(self) -> str:
        return (f"共 {len(self.soul_db)} 条合法魂核词条；在某一槽里输入关键词只缩小"
                "该槽的下拉列表，别的槽不受影响。空格分隔多个关键词＝必须同时包含")

    def _filter_soul_slot(self, index: int, keyword: str) -> int:
        """Per-slot filter for 魂核 (same reasoning as :meth:`_filter_slot`)."""
        combo = self.soul_slot_combos[index]
        keyword = keyword.strip()
        if not keyword:
            combo.configure(values=self.soul_db.labels())
            return len(self.soul_db)
        matches = self.soul_db.search(keyword)
        combo.configure(values=(EMPTY_LABEL,)
                        + tuple(entry.label for entry in matches))
        return len(matches)

    def _on_soul_slot_typed(self, index: int) -> None:
        typed = self.soul_slot_combos[index].get()
        if typed in self.soul_by_label or typed == EMPTY_LABEL:
            return
        count = self._filter_soul_slot(index, typed)
        if not typed.strip():
            self.soul_search_status_var.set(self._soul_search_hint())
        elif count:
            self.soul_search_status_var.set(
                f"槽{index + 1}: 「{typed.strip()}」匹配 {count} 条魂核词条，"
                "展开该槽的下拉列表选择（其它槽不受影响）")
        else:
            self.soul_search_status_var.set(
                f"槽{index + 1}: 「{typed.strip()}」没有匹配到任何魂核词条 —— "
                "换个更短的关键词，或直接写词条 id")

    def _on_soul_slot_return(self, index: int) -> None:
        typed = self.soul_slot_combos[index].get().strip()
        if not typed or typed == EMPTY_LABEL or typed in self.soul_by_label:
            return
        matches = self.soul_db.search(typed)
        if len(matches) == 1:
            self.soul_slot_combos[index].set(matches[0].label)
            self._on_soul_slot_picked(index)
            self.soul_slot_combos[index].configure(values=self.soul_db.labels())
            self.soul_search_status_var.set(
                f"槽{index + 1}: 已选中 {matches[0].label}")
        elif matches:
            self._filter_soul_slot(index, typed)
            self.soul_search_status_var.set(
                f"槽{index + 1}: 「{typed}」匹配 {len(matches)} 条，"
                "请从该槽的下拉列表里选一条")
        else:
            self._filter_soul_slot(index, typed)
            self.soul_search_status_var.set(
                f"槽{index + 1}: 「{typed}」没有匹配到任何魂核词条")

    def _reset_soul_lists(self) -> None:
        for combo in self.soul_slot_combos:
            combo.configure(values=self.soul_db.labels())
        self.soul_search_status_var.set(self._soul_search_hint())

    def _resolve_soul_slot_text(self, index: int) -> str:
        if "disabled" in self.soul_slot_combos[index].state():
            return ""
        typed = self.soul_slot_combos[index].get().strip()
        if not typed or typed == EMPTY_LABEL or typed in self.soul_by_label:
            return typed
        matches = self.soul_db.search(typed)
        if len(matches) == 1:
            self.soul_slot_combos[index].set(matches[0].label)
            return matches[0].label
        by_id = self._entry_from_id_text(typed, self.soul_db)
        if by_id is not None:
            self.soul_slot_combos[index].set(by_id.label)
            return by_id.label
        raise EditorError(
            f"槽{index + 1} 的文本不是魂核词条库里的词条：{typed!r}"
            f"（关键词匹配 {len(matches)} 条，请从该槽下拉列表里选一条）")

    def _soul_slot_choice(self, index: int):
        text = self._resolve_soul_slot_text(index)
        if not text or text == EMPTY_LABEL:
            return None
        entry = self.soul_by_label.get(text)
        if entry is None:
            raise EditorError(f"槽{index + 1} 的词条不在魂核词条库内，请重新选择")
        return entry

    def create_soul_core(self) -> None:
        """需求(5) for 魂核: create a new core from the current selections."""
        if self.decrypted is None:
            messagebox.showwarning("提示", "请先读取存档")
            return
        chosen = self.soul_item_choices.get(self.create_soul_combo.get())
        if chosen is None:
            messagebox.showwarning("提示", "请先选择要新建的魂核种类")
            return
        try:
            level = int(self.create_soul_level_var.get().strip(), 10)
        except ValueError:
            messagebox.showwarning("提示", "等级必须是整数")
            return
        view = self._selected_soul()
        if view is None or view.record_type != chosen.item_id:
            messagebox.showwarning(
                "提示",
                f"新建 {chosen.label} 前，请先在左侧选中一个**同种类**魂核作为模板："
                "固定词条与逐件字段必须从真实样本复制。")
            return
        effects = []
        try:
            for index in range(EFFECT_COUNT):
                entry = self._soul_slot_choice(index)
                if entry is None:
                    continue
                edit: dict[str, int] = {"slot_index": index,
                                        "effect_id": entry.effect_id}
                text = self.soul_value_vars[index].get().strip()
                if text:
                    edit["value"] = int(text, 10)
                effects.append(edit)
        except (EditorError, ValueError) as error:
            messagebox.showwarning("提示", f"魂核词条/数值有误：{error}")
            return
        if not messagebox.askokcancel(
            "确认新建魂核",
            f"用模板 #{view.slot_index} 新建一个 {chosen.label}（Lv{level}）"
            "放进第一个空槽？\n\n"
            "· 固定词条按模板带入，不能在这里改动；\n"
            "· 数值必须在魂核词条区间内；背包已满时会拒绝；写入前会自动备份。",
            icon="warning",
        ):
            return
        try:
            report = find_free_slots(self.decrypted, layout=self.soul_layout)
            plan = plan_creation(
                self.decrypted, record_type=chosen.item_id, level=level,
                effects=effects, affix_db=self.soul_db,
                item_db=self.soul_item_db, known_ids=self.soul_known_ids,
                layout=self.soul_layout, soul=True,
            )
            self.decrypted = apply_creations(self.decrypted, [plan])
        except EditorError as error:
            self.create_soul_status_var.set(str(error))
            messagebox.showerror("无法新建", str(error))
            return
        self.create_soul_status_var.set(
            f"{plan.describe(self.soul_item_db)}（新建前空位 {report.free_count} 个）")
        self._status(f"已新建 {plan.describe(self.soul_item_db)}（尚未写入存档）")
        self._refresh_after_edit(plan.slot_index)

    def _selected_soul(self) -> SoulCoreView | None:
        if self.selected_soul is None:
            return None
        for view in self.soul_views:
            if view.slot_index == self.selected_soul:
                return view
        return None

    def _on_soul_selected(self) -> None:
        selection = self.soul_tree.selection()
        if not selection:
            return
        self.selected_soul = int(selection[0])
        view = self._selected_soul()
        if view is None:
            return
        self._reset_soul_lists()
        self._fill_value_boxes(view, self.soul_db, self.soul_value_vars)
        for index, effect in enumerate(view.effects):
            self.soul_value_entries[index].state(["!disabled"])
            if effect.is_empty:
                # 空槽位在当前周目不可修改（见 limits.EMPTY_SLOT_EDITABLE）。
                self.soul_slot_combos[index].set(EMPTY_LABEL)
                apply_empty_slot_state(self.soul_slot_combos[index],
                                       self.soul_slot_labels[index],
                                       self.soul_value_entries[index],
                                       self.soul_value_vars[index])
                continue
            entry = self.soul_db.lookup(effect.effect_id)
            label = entry.label if entry else f"{effect.effect_id:#06x} (非表内词条)"
            if view.slot_is_fixed(index, self.soul_db):
                self.soul_slot_combos[index].set(f"{label}（固定，不可修改）")
                self.soul_slot_combos[index].state(["disabled"])
                self.soul_value_vars[index].set(str(effect.value))
                self.soul_value_entries[index].state(["disabled"])
                self.soul_slot_labels[index].set(
                    f"数值={effect.value} 标识={effect.metadata:#010x}"
                    " ← 固定词条，词条与数值都不能改")
                continue
            self.soul_slot_combos[index].set(label)
            self.soul_slot_labels[index].set(
                f"数值={effect.value} 标识={effect.metadata:#010x}")
            self.soul_slot_combos[index].state(["!disabled"])
            # 数值框能不能改跟着槽里这条词条走（唯一取值 → 只显示）。
            self.soul_value_entries[index].state(
                ["!disabled"] if affix_value_editable(entry) else ["disabled"])
        self.soul_level_var.set(str(view.level))
        self.soul_level_status_var.set(
            f"当前 Lv{view.level}（范围 1..{MAX_ITEM_LEVEL}）。"
            "只改等级字段，词条数值不随等级变化；请谨慎修改并进游戏确认。"
        )
        self._refresh_soul_rarity_state(view)
        allowed = view.level_mirror == view.level
        self.soul_level_entry.state(["!disabled"] if allowed else ["disabled"])
        self.soul_level_button.state(["!disabled"] if allowed else ["disabled"])
        samples = collect_kind_samples(self.decrypted, affix_db=self.soul_db,
                                        known_ids=self.soul_known_ids,
                                        layout=self.soul_layout)
        choices = []
        for entry in self.soul_item_db.all():
            if entry.item_id == view.record_type:
                continue
            sample = samples.get(entry.item_id)
            if sample is None or sample.ambiguous:
                continue
            choices.append(entry)
        self.soul_kind_choices = {entry.label: entry for entry in choices}
        self.soul_kind_combo.configure(values=tuple(self.soul_kind_choices))
        self.soul_kind_combo.set("")
        self.soul_kind_status_var.set(
            f"当前 {view.describe_item(self.soul_item_db)}；"
            f"存档里可换的同类魂核 {len(choices)} 个（只列出已有实例、"
            "固定词条唯一可复制的种类）。")
        self._set_soul_controls(True)
        self.soul_kind_combo.state(["!disabled", "readonly"] if choices
                                   else ["disabled"])
        self.soul_kind_button.state(["!disabled"] if choices else ["disabled"])

    def _soul_edit_for(self, index: int, view: SoulCoreView) -> dict[str, int] | None:
        """Build the pending 魂核 edit for one slot (``None`` when unchanged)."""
        try:
            text = self._resolve_soul_slot_text(index)
        except EditorError as error:
            raise UiEditError(str(error)) from error
        current = view.effects[index]
        if text == EMPTY_LABEL:
            if current.is_empty:
                return None
            return {"slot_index": index, "effect_id": EMPTY_EFFECT_ID,
                    "value": 0, "metadata": 0}
        entry = self.soul_choices.get(text)
        if entry is None:
            return None
        wanted = entry.value
        text_value = self.soul_value_vars[index].get().strip()
        if text_value:
            try:
                wanted = int(text_value, 10)
            except ValueError as error:
                raise EditorError(
                    f"槽{index + 1} 的数值必须是整数，实际 {text_value!r}") from error
        if entry.effect_id == current.effect_id and wanted == current.value:
            return None
        return {"slot_index": index, "effect_id": entry.effect_id,
                "value": wanted, "metadata": current.metadata}

    def apply_soul_edits_to_selection(self) -> None:
        if self.decrypted is None:
            messagebox.showwarning("提示", MSG_NEED_DATA)
            return
        view = self._selected_soul()
        if view is None:
            messagebox.showwarning("提示", MSG_NEED_SELECTION)
            return
        edits = []
        for index in range(EFFECT_COUNT):
            if view.slot_is_fixed(index, self.soul_db):
                continue  # 固定词条: see _current_edits
            try:
                edit = self._soul_edit_for(index, view)
            except Exception as error:  # noqa: BLE001 - surfaced through the GUI
                messagebox.showwarning("提示", str(error))
                return
            if edit is not None:
                edits.append(dict(edit, record_index=view.slot_index))
        if not edits:
            messagebox.showwarning("提示", "当前没有检测到改动")
            return
        target = view.slot_index
        try:
            self.decrypted = apply_soul_edits(
                self.decrypted, tuple(edits), soul_db=self.soul_db,
                soul_item_db=self.soul_item_db, known_ids=self.soul_known_ids,
                layout=self.soul_layout)
        except Exception as error:  # noqa: BLE001 - surfaced through the GUI
            messagebox.showerror("错误", str(error))
            return
        self._refresh_after_edit(target)

    def apply_soul_level_to_selection(self) -> None:
        if self.decrypted is None:
            messagebox.showwarning("提示", MSG_NEED_DATA)
            return
        view = self._selected_soul()
        if view is None:
            messagebox.showwarning("提示", MSG_NEED_SELECTION)
            return
        try:
            level = int(self.soul_level_var.get().strip(), 10)
        except ValueError:
            messagebox.showwarning("提示", f"等级必须是整数：{self.soul_level_var.get()!r}")
            return
        target = view.slot_index
        if not messagebox.askokcancel(
            "确认修改魂核等级",
            f"把魂核记录 #{target} 的等级改成 {level}？\n\n"
            "· 只写入等级字段（+0x06/+0x08），词条数值不会被改写；\n"
            "· 请谨慎修改：改完请进游戏确认显示与属性是否正常；\n"
            "· 存档写入前会自动备份。",
            icon="warning",
        ):
            return
        try:
            plan = plan_level_edit(self.decrypted, target, level,
                                    affix_db=self.soul_db,
                                    known_ids=self.soul_known_ids,
                                    layout=self.soul_layout)
            self.decrypted = apply_level_edits(self.decrypted, [plan])
        except Exception as error:  # noqa: BLE001 - surfaced through the GUI
            messagebox.showerror("错误", str(error))
            return
        self._refresh_after_edit(target)

    # -- 魂核稀有度 ---------------------------------------------------------
    def _set_soul_rarity_enabled(self, enabled: bool) -> None:
        state = ["!disabled"] if enabled else ["disabled"]
        self.soul_rarity_entry.state(state)
        self.soul_rarity_button.state(state)

    def _refresh_soul_rarity_state(self, view: SoulCoreView) -> None:
        """Enable the 稀有度 row for the selected 魂核 and show its value/range.

        每次选中一条魂核都会调到这里，值和范围都重设 —— 上一条的值不会残留。
        """
        cap = self.rarity_cap_for_record(view.record_type)
        color = rarity_color_of(view.rarity)
        colored = f"（{color}）" if color else ""
        if cap is None:
            self.soul_rarity_var.set("")
            self.soul_rarity_status_var.set(
                f"不可改：魂核记录 #{view.slot_index} 的种类 {view.record_type:#06x} "
                "不在物品总目录（data/equipment_items.json）里，无法确定稀有度上限。")
            self._set_soul_rarity_enabled(False)
            return
        self.soul_rarity_var.set(str(view.rarity))
        self.soul_rarity_status_var.set(
            f"当前 {view.rarity}{colored}（{view.rarity_name}）。范围 0..{cap}"
            f"（魂核比其它大类低一档）；颜色对照 {RARITY_COLOR_HINT}；"
            f"橙色（5）在当前周目不可达。{limits.describe_origin()}")
        self._set_soul_rarity_enabled(True)

    def apply_soul_rarity_to_selection(self) -> None:
        """Change the selected 魂核's 稀有度 (memory only; 写入存档 commits)."""
        if self.decrypted is None:
            messagebox.showwarning("提示", MSG_NEED_DATA)
            return
        view = self._selected_soul()
        if view is None:
            messagebox.showwarning("提示", MSG_NEED_SELECTION)
            return
        text = self.soul_rarity_var.get().strip()
        try:
            rarity = int(text, 10)
        except ValueError:
            messagebox.showwarning("提示", f"稀有度必须是整数：{text!r}")
            return
        target = view.slot_index
        cap = self.rarity_cap_for_record(view.record_type)
        cap_text = "（这条记录的种类不在物品总目录里，引擎会拒绝）" if cap is None \
            else f"0..{cap}"
        if not messagebox.askokcancel(
            "确认修改魂核稀有度",
            f"把魂核记录 #{target} 的稀有度改成 {rarity}？\n\n"
            "· 只写入品质字段（+0x30 的低 4 位；写成 0 时才顺带清 +0x31 的低 4 位），"
            "词条、等级、标识都不动；\n"
            f"· 合法范围 {cap_text}（魂核上限比其它大类低）；\n"
            f"· 颜色对照 {RARITY_COLOR_HINT}；橙色（5）在当前周目不可达；\n"
            "· 存档写入前会自动备份。",
            icon="warning",
        ):
            return
        try:
            plan = plan_rarity_edit(self.decrypted, target, rarity,
                                    affix_db=self.soul_db,
                                    known_ids=self.soul_known_ids,
                                    layout=self.soul_layout)
            self.decrypted = apply_rarity_edits(self.decrypted, [plan])
        except Exception as error:  # noqa: BLE001 - surfaced through the GUI
            messagebox.showerror("错误", str(error))
            return
        self._refresh_after_edit(target)

    def apply_soul_kind_to_selection(self) -> None:
        if self.decrypted is None:
            messagebox.showwarning("提示", MSG_NEED_DATA)
            return
        view = self._selected_soul()
        if view is None:
            messagebox.showwarning("提示", MSG_NEED_SELECTION)
            return
        chosen = self.soul_kind_choices.get(self.soul_kind_combo.get())
        if chosen is None:
            messagebox.showwarning("提示", "请先选择要换成的魂核种类")
            return
        target = view.slot_index
        if not messagebox.askokcancel(
            "确认改魂核种类",
            f"把魂核记录 #{target} 换成 {chosen.label}？\n\n"
            "· 只允许同分类（魂核 ↔ 魂核）互换；\n"
            "· 该魂核的固定词条会从本存档里同种类的真实样本复制；\n"
            "· 普通词条保持原样；改完请进游戏确认；写入前会自动备份。",
            icon="warning",
        ):
            return
        try:
            plan = plan_kind_swap(self.decrypted, target, chosen.item_id,
                                  affix_db=self.soul_db,
                                  item_db=self.soul_item_db,
                                  known_ids=self.soul_known_ids,
                                  layout=self.soul_layout)
            self.decrypted = apply_kind_swaps(self.decrypted, [plan])
        except Exception as error:  # noqa: BLE001 - surfaced through the GUI
            messagebox.showerror("错误", str(error))
            return
        self._refresh_after_edit(target)

    def _refresh_after_edit(self, target: int) -> None:
        """Reload both tabs from the patched memory image, keeping the selection."""
        known_ids = accessory_catalog_ids(self.affix_db)
        layout = inspect_layout(self.decrypted, known_ids=known_ids)
        self._populate_accessories(
            (self.decrypted,
             list_accessories(self.decrypted, layout=layout, known_ids=known_ids),
             self.checksum_ok, layout),
            keep_selection=True,
        )
        self._populate_soul_cores()
        if self.soul_tree.exists(str(target)):
            self.soul_tree.selection_set(str(target))
            self.selected_soul = target
            self._on_soul_selected()
        self._status(f"记录 #{target} 的修改已应用到内存数据（尚未写入存档）")

    # ------------------------------------------------------------- version

    @staticmethod
    def _version_short() -> str:
        """The footer's one-line identity; the 版本信息 dialog has the details."""
        info = version_info()
        built = info.built_at if info.built_at != UNKNOWN else "源码运行"
        return (f"v{info.version} · commit {info.commit}{info.dirty_suffix}"
                f" · 构建 {built} · {info.language}")

    @staticmethod
    def _version_text() -> str:
        """Four required facts: commit tail, program folder, build time, language.

        ``来源`` is the folder the running copy sits in — never the build machine's
        path (that one is only recorded in ``BUILD-INFO.txt``).
        """
        info = version_info()
        built = info.built_at if info.built_at != UNKNOWN else "未构建（源码运行）"
        return (
            f"版本 v{info.version} · commit {info.commit}{info.dirty_suffix}\n"
            f"来源 {paths.application_root()}\n"
            f"加密组件 {info.crypto_exe}\n"
            f"构建时间 {built} · 语言 {info.language}"
        )

    def show_version_info(self) -> None:
        info = version_info()
        details = [version_banner(info), ""]
        if info.commit_full not in ("", UNKNOWN):
            details.append(f"完整 commit: {info.commit_full}")
        details.append(f"分支       : {info.branch}")
        if info.crypto_exe_sha256 not in ("", UNKNOWN):
            details.append(f"组件 SHA-256: {info.crypto_exe_sha256}")
        details.append(f"信息来源   : {info.source}")
        details.append("")
        details.append(f"程序目录   : {resource_root()}")
        details.append(f"配置文件   : {self.config.source or paths.default_config_path()}")
        details.append(f"备份目录   : {self.state_root}")
        if self._backend_note:
            details.append(f"加密后端   : {self._backend_note}")
        report = last_report()
        if report is not None:
            details.append(f"随附文件   : {report.summary()}")
        messagebox.showinfo("版本信息", "\n".join(details))

    # ------------------------------------------------------------- helpers

    def _status(self, text: str) -> None:
        self.status_var.set(text)

    def _poll_game_status(self) -> None:
        """Refresh the game-process line, off the UI thread and rate-limited.

        The check spawns ``tasklist``, so it must not run on the UI thread, and it
        must not touch the status line (that one reports the user's own actions).
        """
        now = time.monotonic()
        if now - self._game_status_at >= GAME_STATUS_INTERVAL_MS / 1000:
            self._game_status_at = now
            self._run_background(self._check_game_status)
        self.after(GAME_STATUS_INTERVAL_MS, self._poll_game_status)

    def _check_game_status(self) -> tuple[str, tuple[str, ...]]:
        return "game_status", tuple(running_game_processes())

    def _show_game_status(self, running: object) -> None:
        names = tuple(str(name) for name in running) if running else ()
        if names:
            self.game_status_var.set(GAME_STATUS_RUNNING.format(names="、".join(names)))
            self.game_status_label.configure(foreground="#b03030")
        else:
            self.game_status_var.set(GAME_STATUS_CLOSED)
            self.game_status_label.configure(foreground="#1a7f37")

    def _run_background(self, function, *args) -> None:
        """Run ``function`` off the UI thread and deliver its result to the queue."""
        def runner() -> None:
            try:
                self.worker_queue.put(("ok", function(*args)))
            except Exception as error:  # noqa: BLE001 - surfaced through the GUI
                self.worker_queue.put(("error", error))

        threading.Thread(target=runner, daemon=True).start()

    def _run_worker(self, function, *args) -> None:
        self._run_background(function, *args)
        self._status("处理中…")

    def _poll_worker(self) -> None:
        try:
            while True:
                kind, payload = self.worker_queue.get_nowait()
                if kind == "ok":
                    self._on_worker_ok(payload)
                else:
                    self._on_worker_error(payload)
        except queue.Empty:
            pass
        self.after(80, self._poll_worker)

    def _on_worker_ok(self, payload: object) -> None:
        if not isinstance(payload, tuple) or not payload:
            return
        tag, value = payload[0], payload[1] if len(payload) > 1 else None
        if tag == "saves":
            self._populate_saves(value)
        elif tag == "accessories":
            self._populate_accessories(value)
        elif tag == "accessories_failed":
            self._report_no_layout(value)
        elif tag == "written" and isinstance(value, dict):
            # The acknowledgement is spent: the next write must confirm again.
            self.title_screen_var.set(False)
            self._status(f"已写入，SHA-256 {value.get('new_sha256', '')}")
            messagebox.showinfo("完成", "修改已写入存档。\n请在游戏中重新加载存档查看效果。")
        elif tag == "dry_run" and isinstance(value, dict):
            self._status("演练完成（未写入）")
            messagebox.showinfo(
                "演练结果",
                "未写入任何文件。\n\n"
                f"校验和 {value.get('checksum_before')} -> {value.get('checksum_after')}",
            )
        elif tag == "backup" and isinstance(value, str):
            self._status(f"备份完成：{value}")
            messagebox.showinfo("备份完成", f"已备份到：\n{value}")
        elif tag == "restored" and isinstance(value, dict):
            self._status(f"已恢复备份，SHA-256 {value.get('new_sha256', '')}")
            matches = value.get("matches_original_save")
            note = ""
            if matches is True:
                note = "\n\n校验：与备份时记录的原文件 SHA-256 完全一致。"
            elif matches is False:
                note = ("\n\n注意：与备份时记录的原文件 SHA-256 不一致"
                        "（可能仅存档尾 8 字节不同）。")
            messagebox.showinfo(
                "恢复完成",
                f"已用备份覆盖存档：\n{value.get('restored_from')}\n\n"
                f"恢复前的存档已另存到：\n{value.get('safety_backup_dir')}"
                + note + "\n\n请在游戏中加载该存档确认。",
            )
            self._invalidate_loaded_save()
            self.title_screen_var.set(False)
        elif tag == "restore_dry_run" and isinstance(value, dict):
            self._status("恢复演练完成（未写入）")
            messagebox.showinfo(
                "恢复演练",
                "未写入任何文件。\n\n"
                f"将恢复自：{value.get('restored_from')}",
            )
        elif tag == "game_status":
            self._show_game_status(value)

    def _invalidate_loaded_save(self) -> None:
        """The file changed underneath us: force a re-read before any write."""
        self.decrypted = None
        self.accessory_views = []
        self.other_views = []
        for tab in getattr(self, "equipment_tabs", []):
            tab.populate(())
        self.layout = None
        self.known_ids = frozenset()
        self.selected_accessory = None
        self.checksum_ok = False
        self.tree.delete(*self.tree.get_children())
        for combo in self.slot_combos:
            combo.set("")
        for label in self.slot_labels:
            label.set("")

    def _on_worker_error(self, error: Exception) -> None:
        self._status("操作失败")
        messagebox.showerror("错误", str(error))

    # ------------------------------------------------------------- actions

    # ------------------------------------------------ 稀有度上限（界面显示）
    def rarity_cap_for_record(self, record_type: int) -> int | None:
        """这条记录所属大类的稀有度上限；**查不到返回 ``None``**（表外 id）。

        与引擎同一口径（:func:`editor.plan_rarity_edit`）：大类来自**统一物品总目录**
        ``data/equipment_items.json``，上限来自 ``limits.RARITY_CAP_BY_BIG``。
        ``None`` = 表外 id，界面据此置灰并说明原因 —— 引擎也会拒绝它，绝不猜数字。
        """
        item = self.equipment_item_db.lookup(record_type)
        if item is None or not item.big:
            return None
        return equipmentdb.rarity_cap_for_big(item.big)

    def refresh_saves(self) -> None:
        self._run_worker(self._load_saves)

    def _load_saves(self) -> tuple[str, tuple[SaveDescriptor, ...]]:
        """Scan the configured save root (an instance method: it reads config)."""
        return "saves", discover_saves(self.config.resolved_save_root())

    def _populate_saves(self, saves: object) -> None:
        self.saves = list(saves) if saves else []
        self.save_combo["values"] = [save.display for save in self.saves]
        if self.saves:
            self.save_combo.current(0)
            self.selected_save = self.saves[0]
            self._status(f"发现 {len(self.saves)} 个存档")
            self.table_var.set("")
        else:
            self.selected_save = None
            self._status("未发现存档")
            self.table_var.set(f"查找位置 {self._search_root_hint()}")

    def _search_root_hint(self) -> Path:
        """Where the scan looked, so an empty list is actionable."""
        configured = self.config.resolved_save_root()
        return Path(configured) if configured else save_root_directory()

    def _on_save_selected(self) -> None:
        index = self.save_combo.current()
        if 0 <= index < len(self.saves):
            self.selected_save = self.saves[index]
            self._status(f"已选择 {self.saves[index].display}")

    def load_accessories(self) -> None:
        """Read the save once for **both** tabs (饰品 + 魂核).

        The button is labelled 读取数据 for exactly that reason: one read fills the
        饰品 page and the 魂核（魂之核） page from the same decrypted bytes.
        """
        if self.selected_save is None:
            messagebox.showwarning("提示", "请先选择存档")
            return
        self._run_worker(self._load_accessories_worker, self.selected_save)

    def _load_accessories_worker(self, save: SaveDescriptor) -> tuple[str, tuple]:
        data = open_save(save, self.crypto)
        checksum_ok = save_checksum_is_valid(data)
        known_ids = accessory_catalog_ids(self.affix_db)
        try:
            layout = inspect_layout(data, known_ids=known_ids)
        except RecordError as error:
            # No array at all: report the diagnosis instead of a silent empty
            # table, so the user can send it back and the layout can be fixed.
            return "accessories_failed", (data, checksum_ok, str(error),
                                          layout_diagnosis(data))
        views = list_accessories(data, layout=layout, known_ids=known_ids)
        return "accessories", (data, views, checksum_ok, layout)

    def _populate_accessories(self, payload: object, *, keep_selection: bool = False) -> None:
        data, views, checksum_ok, layout = payload
        self.decrypted = data
        self.checksum_ok = bool(checksum_ok)
        self.layout = layout
        # The catalog evidence and the located array the records were *listed* with:
        # every later edit reuses these two, so a record index can never resolve to
        # a different item than the one the user selected.
        self.known_ids = accessory_catalog_ids(self.affix_db)
        self._populate_soul_cores()
        self._populate_equipment_tabs()
        # Only records whose affixes really are 饰品词条 are editable accessories;
        # weapons/armour/绘卷 share the same array and are reported separately.
        # ``is_accessory is None`` means no catalog evidence was supplied, so the
        # record is kept (that is the legacy behaviour) instead of being dropped.
        self.accessory_views = [view for view in views if view.is_accessory is not False]
        self.other_views = [view for view in views if view.is_accessory is False]
        # Clearing a record's last 饰品词条 would otherwise make it drop out of the
        # list while the user is still editing it, so the record being edited is
        # always kept visible.
        if keep_selection and self.selected_accessory is not None:
            listed = {view.slot_index for view in self.accessory_views}
            retained = next((view for view in views
                             if view.slot_index == self.selected_accessory
                             and view.slot_index not in listed), None)
            if retained is not None:
                self.accessory_views.append(retained)
        self.tree.delete(*self.tree.get_children())
        self._refresh_accessory_tree()
        known = {view.slot_index for view in self.accessory_views}
        if not keep_selection or self.selected_accessory not in known:
            self.selected_accessory = None
        for index in range(EFFECT_COUNT):
            self.slot_combos[index].set("")
            self.slot_labels[index].set("")
        suffix = "" if self.checksum_ok else "（校验和不一致，请谨慎）"
        others = len(self.other_views)
        other_note = f" · 另有 {others} 条武器/防具/绘卷记录未列出" if others else ""
        if not self.accessory_views:
            self._status(f"记录表里没有含饰品词条的记录{suffix}")
            self.table_var.set(layout.describe())
            messagebox.showinfo(
                "未找到饰品记录",
                "已在存档中找到记录表，但其中没有含饰品词条（词条 id 命中饰品词条库）"
                "的记录。\n\n"
                + layout.describe()
                + f"\n\n记录表内共 {len(views)} 条物品记录，均未命中饰品词条库，"
                  "可能都是武器/防具/绘卷。\n\n"
                  "提示：读取存档文件即可，游戏无需运行。\n"
                  "可在命令行运行 scan 命令查看完整诊断并反馈给作者。",
            )
            return
        souls = len([view for view in self.soul_views if not view.unidentified])
        soul_note = (f" · 魂核 {souls} 个已读好，见上方的『魂核（魂之核）』页签"
                     if souls else "")
        self._status(f"已读取 {len(self.accessory_views)} 件饰品{suffix}"
                     f"{other_note}{soul_note}")
        self.table_var.set(layout.describe())

    def _populate_equipment_tabs(self) -> None:
        """用已经解密在内存里的字节填满 武器 / 防具 两个页签。

        记录只解析一次（``list_equipment``），再按大类分给两个页签；任何一个页签
        出问题都不会影响别的页签，也不会影响饰品 / 魂核的既有流程。
        """
        if not self.equipment_tabs or self.decrypted is None:
            return
        try:
            views = list_equipment(self.decrypted, layout=self.layout,
                                   item_db=self.equipment_item_db)
        except Exception as error:  # noqa: BLE001 - 逐页签报告，不抛出
            for tab in self.equipment_tabs:
                tab.populate(())
                tab.set_note(f"未能列出{tab.big}记录：{error}")
            return
        for tab in self.equipment_tabs:
            try:
                tab.populate([view for view in views if view.big == tab.big])
            except Exception as error:  # noqa: BLE001 - 同上
                tab.populate(())
                tab.set_note(f"未能列出{tab.big}记录：{error}")

    def _report_no_layout(self, payload: object) -> None:
        """Show why nothing could be read, with the raw diagnosis."""
        _data, checksum_ok, message, diagnosis = payload
        self.accessory_views = []
        self.other_views = []
        for tab in getattr(self, "equipment_tabs", []):
            tab.populate(())
        self.layout = None
        self.known_ids = frozenset()
        self.selected_accessory = None
        self.tree.delete(*self.tree.get_children())
        for index in range(EFFECT_COUNT):
            self.slot_combos[index].set("")
            self.slot_labels[index].set("")
        suffix = "" if checksum_ok else "（校验和不一致，请谨慎）"
        self._status(f"未能定位物品记录表{suffix}")
        self.table_var.set("\n".join(describe_diagnosis(diagnosis)))
        messagebox.showwarning(
            "未能定位物品记录表",
            f"{message}\n\n"
            + "\n".join(describe_diagnosis(diagnosis))
            + "\n\n提示：读取存档文件即可，游戏无需运行。\n"
              "请把以上诊断（或用 scan --json 的输出）反馈给作者。",
        )

    def _selected_view(self) -> AccessoryView | None:
        return next(
            (item for item in self.accessory_views
             if item.slot_index == self.selected_accessory),
            None,
        )

    def _on_accessory_selected(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        self.selected_accessory = int(selection[0])
        view = self._selected_view()
        if view is None:
            return
        self._reset_affix_lists()
        grace = self._grace_slot_indexes(view)
        self._fill_value_boxes(view, self.affix_db, self.value_vars)
        for index, effect in enumerate(view.effects):
            self.value_entries[index].state(["!disabled"])
            if effect.is_empty:
                # 空槽位在当前周目不可修改（见 limits.EMPTY_SLOT_EDITABLE）：
                # 下拉与数值框都置灰，并在标签上写明理由。
                self.slot_combos[index].set(EMPTY_LABEL)
                apply_empty_slot_state(self.slot_combos[index],
                                       self.slot_labels[index],
                                       self.value_entries[index],
                                       self.value_vars[index])
                continue
            entry = self.affix_db.lookup(effect.effect_id)
            if view.slot_is_fixed(index, self.affix_db, grace_db=self.grace_db):
                # 同名固定词条: shown, but neither the affix nor its 数值 is
                # editable — it is part of what the item is (and follows 种类
                # automatically when 种类 changes).  The 数值 box keeps showing the
                # save's own value, greyed out and disabled, so it is obvious that
                # there is nothing to type here.
                label = (entry.label if entry
                         else f"{effect.effect_id:#06x} (非表内词条)")
                self.slot_combos[index].set(f"{label}（固定，不可修改）")
                self.slot_combos[index].state(["disabled"])
                self.value_vars[index].set(str(effect.value))
                self.value_entries[index].state(["disabled"])
                self.slot_labels[index].set(
                    f"数值={effect.value} 标识={effect.metadata:#010x}"
                    " ← 固定词条，词条与数值都不能改")
                continue
            if index in grace:
                # 恩宠 / 套装组合 effect: named from the workbook's 词条总目录 when
                # the table is present, otherwise reported honestly as out-of-table.
                # The name comes from 词条总目录, not from the affix catalog, so this
                # box is *not* an affix picker: it is shown disabled (and skipped when
                # edits are collected) and the 恩宠 combo below is the way to change
                # it.  Leaving it enabled is what made 应用修改 complain that the text
                # was "not in the affix table".
                named = self.grace_db.describe(effect.effect_id)
                grace_entry = self.grace_db.lookup(effect.effect_id)
                kind = grace_entry.category if grace_entry is not None else ""
                if grace_family_of(kind) == "套装":
                    # 与武器 / 防具页签同一口径：套装槽锁死，理由说清楚。
                    label = (f"{effect.effect_id:#06x} {named}（不可替换）"
                             if named else
                             f"{effect.effect_id:#06x} 套装词条（表外，不可替换）")
                    self.slot_combos[index].set(label)
                    self.slot_labels[index].set(
                        f"数值={effect.value} 标识={effect.metadata:#010x}"
                        f" ← {kind or '套装'}：套装与物品种类强绑定，"
                        "任何替换都会被拒绝")
                else:
                    label = (f"{effect.effect_id:#06x} {named}" if named
                             else f"{effect.effect_id:#06x} 恩宠/套装词条（表外）")
                    self.slot_combos[index].set(f"{label}（用下方【恩宠】栏替换）")
                    self.slot_labels[index].set(
                        f"数值={effect.value} 标识={effect.metadata:#010x}"
                        f" ← {kind or '恩宠/套装'}词条：用下方【恩宠】栏替换"
                        "（恩宠只能换恩宠）")
                self.slot_combos[index].state(["disabled"])
                self.value_vars[index].set(str(effect.value))
                self.value_entries[index].state(["disabled"])
                continue
            else:
                label = entry.label if entry else f"{effect.effect_id:#06x} (非表内词条)"
                detail = f"数值={effect.value} 标识={effect.metadata:#010x}"
                self.slot_combos[index].state(["!disabled"])
                # 数值框能不能改跟着槽里这条词条走（唯一取值 → 只显示）。
                self.value_entries[index].state(
                    ["!disabled"] if affix_value_editable(entry) else ["disabled"])
            self.slot_combos[index].set(label)
            self.slot_labels[index].set(detail)
        self.item_var.set(
            f"记录 #{view.slot_index}：{view.describe_item(self.item_db)}"
            f"  Lv{view.level} {view.rarity_name}"
        )
        self.level_var.set(str(view.level))
        self._refresh_grace_state(view)
        self._refresh_level_state(view)
        self._refresh_plus_state(view)
        self._refresh_rarity_state(view)
        self._refresh_kind_state(view)

    def _set_grace_enabled(self, enabled: bool) -> None:
        state = ["!disabled"] if enabled else ["disabled"]
        self.grace_combo.state(["!disabled"] if enabled else ["disabled"])
        self.grace_button.state(state)

    def _refresh_grace_state(self, view: AccessoryView) -> None:
        """Show whether this accessory's 恩宠 may be replaced, and why not."""
        availability = grace_edit_availability(view, grace_db=self.grace_db,
                                              affix_db=self.affix_db)
        self.grace_availability = availability
        if availability.allowed:
            self.grace_status_var.set(
                f"当前末位槽 [{availability.slot_index}]："
                f"{availability.describe_current()}。可以选择其他恩宠后点【应用恩宠】。"
            )
            self.grace_status_label.configure(foreground="#1a7f37")
            self._set_grace_enabled(True)
            for index, label in enumerate(self.grace_db.labels()):
                if availability.current_id is not None and \
                        label.startswith(f"{availability.current_id:#06x} "):
                    self.grace_combo.current(index)
                    break
        else:
            self.grace_status_var.set(f"不可改：{availability.reason}")
            self.grace_status_label.configure(foreground="#b03030")
            self.grace_combo.set("")
            self._set_grace_enabled(False)

    def apply_grace_to_selection(self) -> None:
        """Replace the selected accessory's 恩宠 (memory only; 写入存档 commits)."""
        if not self._require_accessory_selection():
            return
        text = self.grace_combo.get()
        if not text:
            messagebox.showwarning("提示", "请先选择要改成的恩宠")
            return
        try:
            grace_id = resolve_grace_id(self.grace_db, text.split(" ", 1)[0])
        except GraceEditError as error:
            messagebox.showwarning("提示", str(error))
            return
        target = self.selected_accessory
        known_ids = accessory_catalog_ids(self.affix_db)
        try:
            self.decrypted = apply_grace_edit(
                self.decrypted, target, grace_id, affix_db=self.affix_db,
                grace_db=self.grace_db, known_ids=known_ids, layout=self.layout,
            )
        except GraceEditError as error:
            # A refused 恩宠 change is a user-input problem, not a crash.
            messagebox.showwarning("恩宠未修改", str(error))
            return
        except Exception as error:  # noqa: BLE001 - surfaced through the GUI
            messagebox.showerror("恩宠未修改", str(error))
            return
        layout = inspect_layout(self.decrypted, known_ids=known_ids)
        self._populate_accessories(
            (self.decrypted,
             list_accessories(self.decrypted, layout=layout, known_ids=known_ids),
             self.checksum_ok, layout),
            keep_selection=True,
        )
        self.tree.selection_set(str(target))
        self.selected_accessory = target
        self._on_accessory_selected()
        self._status(f"记录 #{target} 的恩宠已改为 {self.grace_db.describe(grace_id)}"
                     "（尚未写入存档）")

    def _on_slot_picked(self, index: int) -> None:
        text = self.slot_combos[index].get()
        # Picking another affix refreshes the 数值 box to *that* affix's own value:
        # the previous affix's number is meaningless for the new one (and would be
        # refused by the span gate anyway).  The user can then adjust it in range.
        entry = self.affix_by_label.get(text)
        if entry is None:
            self.value_vars[index].set("")
            self.slot_labels[index].set(
                "" if text == EMPTY_LABEL else f"{text} —— 不是表内词条，不会被写入")
        else:
            self.value_vars[index].set(str(entry.value))
            self.value_entries[index].state(
                ["!disabled"] if affix_value_editable(entry) else ["disabled"])
            self.slot_labels[index].set(
                f"词条 {entry.effect_id:#06x}，合法数值 "
                f"{entry.describe_value_range()}")

    def _on_soul_slot_picked(self, index: int) -> None:
        text = self.soul_slot_combos[index].get()
        entry = self.soul_by_label.get(text)
        self.soul_value_vars[index].set("" if entry is None else str(entry.value))
        self.soul_value_entries[index].state(
            ["!disabled"] if affix_value_editable(entry) else ["disabled"])
        self.soul_slot_labels[index].set(
            "" if entry is None and text == EMPTY_LABEL else
            (f"词条 {entry.effect_id:#06x}，合法数值 {entry.describe_value_range()}"
             if entry else f"{text} —— 不是表内词条，不会被写入"))

    def _pending_edit(self, index: int, current: EffectSlot) -> dict[str, int] | None:
        """Turn one slot widget into an edit, or ``None`` when nothing changed.

        Picking a legal affix writes the affix's 词条代码 into the slot: its
        effect id plus the catalog's nominal value (both live at the same
        relative offsets in the code and in the slot).  ``metadata`` is left
        untouched on purpose -- the slot-level encoding of the 固定/星 flag bits
        is not confirmed against a real save, so the GUI never guesses at it
        (the CLI can still set it explicitly with ``--edit slot:id:value:meta``).
        """
        try:
            text = self._resolve_slot_text(index)
        except EditorError as error:
            raise UiEditError(str(error)) from error
        if not text:
            return None
        if text == EMPTY_LABEL:
            if current.is_empty:
                return None
            return {"slot_index": index, "effect_id": EMPTY_EFFECT_ID}
        try:
            effect_id = int(text.split(" ", 1)[0], 16)
        except ValueError as error:
            raise UiEditError(f"无法解析词条选择：{text!r}") from error
        if effect_id == current.effect_id:
            # Same affix: the 数值 box (需求 4/2) may still ask for another value
            # inside the affix's own span.
            typed = self._slot_value(index)
            if typed is None or typed == current.value:
                return None
            return {"slot_index": index, "effect_id": effect_id, "value": typed}
        entry = self.affix_db.lookup(effect_id)
        if entry is None:
            raise UiEditError(f"词条 {effect_id:#06x} 不在合法的饰品词条表中")
        typed = self._slot_value(index)
        if typed is not None and typed == current.value \
                and not entry.allows_value(typed):
            # The box was pre-filled with the *old* affix's value and the affix
            # changed (programmatic combo changes do not fire <<ComboboxSelected>>):
            # that stale number means nothing for the new affix, so use the new
            # affix's own value instead of failing on the old one.  A number the
            # user typed that is out of range still fails in the engine.
            typed = None
        return {
            "slot_index": index,
            "effect_id": effect_id,
            "value": entry.value if typed is None else typed,
        }

    def _current_edits(self) -> tuple[dict[str, int], ...]:
        """Collect the changed slots for the selected accessory record."""
        if self.selected_accessory is None:
            return ()
        view = self._selected_view()
        if view is None:
            return ()
        edits: list[dict[str, int]] = []
        grace_slots = self._grace_slot_indexes(view)
        skipped: list[str] = []
        for index in range(EFFECT_COUNT):
            if view.slot_is_fixed(index, self.affix_db, grace_db=self.grace_db) or index in grace_slots:
                # 固定词条 is part of what the item is, and an 恩宠/套装 slot is named
                # from 词条总目录: neither is an affix choice, so neither is a pending
                # edit here (the 恩宠 combo is how a grace gets replaced).
                continue
            try:
                edit = self._pending_edit(index, view.effects[index])
            except UiEditError as error:
                # 一格的表外词条只影响它自己（与武器/防具页签同一口径）。
                skipped.append(f"槽{index + 1}（{error}）")
                continue
            if edit is not None:
                edits.append(dict(edit, record_index=self.selected_accessory))
        if skipped:
            messagebox.showwarning(
                "提示",
                "这些槽保持原样、没有收集改动：" + "；".join(skipped)
                + "。其它槽的改动照常应用。")
        return tuple(edits)

    def _current_tab(self) -> tk.Widget | None:
        """当前选中的 notebook 页签控件（取不到就返回 None）。"""
        try:
            return self.notebook.nametowidget(self.notebook.select())
        except (tk.TclError, KeyError):
            return None

    # ------------------------------------------- 统一【应用修改】（饰品 / 魂核页签）
    @staticmethod
    def _widget_values(widgets) -> list[str]:
        return [widget.get() for widget in widgets]

    def _selection_snapshot(self) -> dict:
        """用户此刻在各控件里填的内容（批量应用时每步之前复原）。"""
        return {
            "slots": self._widget_values(self.slot_combos),
            "values": self._widget_values(getattr(self, "value_vars", ())),
            "soul_slots": self._widget_values(getattr(self, "soul_slot_combos", ())),
            "soul_values": self._widget_values(getattr(self, "soul_value_vars", ())),
            "level": self.level_var.get(),
            "plus": self.plus_var.get(),
            "rarity": self.rarity_var.get(),
            "grace": self.grace_combo.get(),
            "soul_level": self.soul_level_var.get(),
            "soul_rarity": self.soul_rarity_var.get(),
        }

    def _selection_restore(self, snapshot: dict) -> None:
        for widget, value in zip(self.slot_combos, snapshot["slots"]):
            widget.set(value)
        for widget, value in zip(getattr(self, "value_vars", ()), snapshot["values"]):
            widget.set(value)
        for widget, value in zip(getattr(self, "soul_slot_combos", ()),
                                 snapshot["soul_slots"]):
            widget.set(value)
        for widget, value in zip(getattr(self, "soul_value_vars", ()),
                                 snapshot["soul_values"]):
            widget.set(value)
        self.level_var.set(snapshot["level"])
        self.plus_var.set(snapshot["plus"])
        self.rarity_var.set(snapshot["rarity"])
        self.grace_combo.set(snapshot["grace"])
        self.soul_level_var.set(snapshot["soul_level"])
        self.soul_rarity_var.set(snapshot["soul_rarity"])

    @staticmethod
    def _as_int(text: str) -> int | None:
        try:
            return int(text.strip(), 10)
        except (TypeError, ValueError, AttributeError):
            return None

    def _accessory_pending(self) -> tuple[list[str], list]:
        """饰品：本次要应用的改动（只列真正改过的字段；词条槽由收集器自己判定）。"""
        view = next((item for item in self.accessory_views
                     if item.slot_index == self.selected_accessory), None)
        if view is None:
            return [], []
        parts: list[str] = ["词条槽的改动（只写真正改过的槽，逐槽见预览）"]
        calls: list = [self.apply_edits_to_selection]
        level = self._as_int(self.level_var.get())
        if level is not None and level != view.level:
            parts.append(f"等级 {view.level} → {level}")
            calls.append(self.apply_level_to_selection)
        plus = self._as_int(self.plus_var.get())
        if plus is not None and plus != view.plus_value:
            parts.append(f"+值 {view.plus_value} → {plus}")
            calls.append(self.apply_plus_to_selection)
        rarity = self._as_int(self.rarity_var.get())
        if rarity is not None and rarity != view.rarity:
            parts.append(f"稀有度 {view.rarity} → {rarity}")
            calls.append(self.apply_rarity_to_selection)
        availability = self.grace_availability
        chosen = self.grace_combo.get().strip()
        if (availability is not None and availability.allowed and chosen
                and availability.current_id is not None
                and not chosen.startswith(f"{availability.current_id:#06x} ")):
            parts.append(f"恩宠 {availability.describe_current()} → "
                         f"{chosen.split(' ', 1)[-1]}")
            calls.append(self.apply_grace_to_selection)
        return parts, calls

    def _soul_pending(self) -> tuple[list[str], list]:
        """魂核：本次要应用的改动（魂核没有 +值 与恩宠槽）。"""
        view = next((item for item in self.soul_views
                     if item.slot_index == self.selected_soul), None)
        if view is None:
            return [], []
        parts: list[str] = ["词条槽的改动（只写真正改过的槽，逐槽见预览）"]
        calls: list = [self.apply_soul_edits_to_selection]
        level = self._as_int(self.soul_level_var.get())
        if level is not None and level != view.level:
            parts.append(f"等级 {view.level} → {level}")
            calls.append(self.apply_soul_level_to_selection)
        rarity = self._as_int(self.soul_rarity_var.get())
        if rarity is not None and rarity != view.rarity:
            parts.append(f"稀有度 {view.rarity} → {rarity}")
            calls.append(self.apply_soul_rarity_to_selection)
        return parts, calls

    def _apply_pending(self, pending, title: str, target, require) -> None:
        """统一【应用修改】：一次确认、一次应用；每步调用前复原用户输入
        （每个字段自己的处理器结束都会刷新控件）。"""
        if not require():
            return
        parts, calls = pending()
        if not calls:
            messagebox.showwarning("提示", "当前没有检测到改动")
            return
        if not messagebox.askokcancel(
            "确认应用修改",
            "将应用以下改动（内存中，尚未写入存档）：\n\n· "
            + "\n· ".join(parts)
            + "\n\n· 只改这些字段，其余字节不动；点【写入存档】前会自动备份。",
            icon="warning",
        ):
            return
        snapshot = self._selection_snapshot()
        with _quiet_dialogs():
            for call in calls:
                self._selection_restore(snapshot)
                call()
        self._status(f"{title}记录 #{target} 的改动已应用到内存数据（尚未写入存档）")

    def apply_all_selection(self) -> None:
        """饰品页签的统一【应用修改】：词条 + 等级 + +值 + 稀有度 + 恩宠。"""
        self._apply_pending(self._accessory_pending, "饰品", self.selected_accessory,
                            self._require_accessory_selection)

    def apply_all_soul(self) -> None:
        """魂核页签的统一【应用修改】：词条 + 等级 + 稀有度。"""
        self._apply_pending(self._soul_pending, "魂核", self.selected_soul,
                            self._require_soul_selection)

    def _require_soul_selection(self) -> bool:
        if self.decrypted is None:
            messagebox.showwarning("提示", MSG_NEED_DATA)
            return False
        if self.selected_soul is None:
            messagebox.showwarning("提示", MSG_NEED_SELECTION)
            return False
        return True

    def apply_current_tab_edits(self) -> None:
        """底部【应用修改】：按**当前页签**分派到该页签自己的处理器。

        以前这里固定调用饰品页签的 :meth:`apply_edits_to_selection`，于是在武器 /
        防具页签里点它会得到「请先读取数据并选择一条记录」——页签明明已经读取了
        数据也选了记录，因为饰品那份选中项当然是空的。
        """
        tab = self._current_tab()
        for equipment_tab in self.equipment_tabs:
            if tab is equipment_tab:
                equipment_tab.apply_all()
                return
        if tab is self.accessory_tab:
            self.apply_all_selection()
            return
        if tab is self.soul_tab:
            self.apply_all_soul()
            return
        messagebox.showwarning(
            "提示", "请用当前页签自己的【应用修改】按钮应用这一页的改动。")

    def _require_accessory_selection(self) -> bool:
        """饰品页签的选中记录是否就绪；不然给出准确提示（两句分开）。"""
        if self.decrypted is None:
            messagebox.showwarning("提示", MSG_NEED_DATA)
            return False
        if self.selected_accessory is None:
            messagebox.showwarning("提示", MSG_NEED_SELECTION)
            return False
        return True

    def apply_edits_to_selection(self) -> None:
        if not self._require_accessory_selection():
            return
        target = self.selected_accessory
        edits = self._current_edits()
        if not edits:
            messagebox.showwarning("提示", "当前没有检测到改动")
            return
        known_ids = accessory_catalog_ids(self.affix_db)
        try:
            # Same catalog evidence and same located array as the listing, so a
            # record index cannot resolve to a different item while editing.
            self.decrypted = apply_edits(self.decrypted, edits, grace_db=self.grace_db,
                                         affix_db=self.affix_db,
                                         known_ids=known_ids,
                                         layout=self.layout)
        except Exception as error:  # noqa: BLE001 - surfaced through the GUI
            messagebox.showerror("错误", str(error))
            return
        # Refresh the tree from the patched memory image so the display matches
        # exactly what a write would persist.  The on-disk checksum is stale by
        # definition until commit, so keep the checksum verdict from load time.
        layout = inspect_layout(self.decrypted, known_ids=known_ids)
        self._populate_accessories(
            (self.decrypted,
             list_accessories(self.decrypted, layout=layout, known_ids=known_ids),
             self.checksum_ok, layout),
            keep_selection=True,
        )
        # Restore the selection explicitly: <<TreeviewSelect>> is not guaranteed
        # to fire synchronously, and the slot widgets must mirror the new state.
        self.tree.selection_set(str(target))
        self.selected_accessory = target
        self._on_accessory_selected()
        self._status(f"记录 #{target} 的修改已应用到内存数据（尚未写入存档）")

    # -- 等级 ---------------------------------------------------------------
    def _set_level_enabled(self, enabled: bool) -> None:
        self.level_entry.state(["!disabled"] if enabled else ["disabled"])
        self.level_button.state(["!disabled"] if enabled else ["disabled"])

    # -- +值 ----------------------------------------------------------------
    def _set_plus_enabled(self, enabled: bool) -> None:
        self.plus_entry.state(["!disabled"] if enabled else ["disabled"])
        self.plus_button.state(["!disabled"] if enabled else ["disabled"])

    def _refresh_plus_state(self, view: AccessoryView) -> None:
        """Enable the +值 row for the selected record and show its current value."""
        self.plus_var.set(str(view.plus_value))
        self.plus_status_var.set(
            f"当前 +{view.plus_value}（字段 +0x0A，只写这两个字节；"
            f"合法范围 0..{MAX_RECORD_PLUS}——这是实测到的取值范围，"
            "游戏自身的上限没有可核对的依据）。"
            "该字段已由游戏内实测确认：物品卡显示的 +13/+18/+19 与存档字节一致。"
        )
        self.plus_status_label.configure(foreground="#1a7f37")
        self._set_plus_enabled(True)

    def _refresh_level_state(self, view: AccessoryView) -> None:
        """Enable the 等级 row only for a record whose level fields agree."""
        if view.level_mirror != view.level:
            self.level_status_var.set(
                f"不可改：记录 #{view.slot_index} 的等级字段不一致"
                f"（+0x06={view.level}，+0x08={view.level_mirror}），已拒绝改写"
            )
            self.level_status_label.configure(foreground="#b03030")
            self._set_level_enabled(False)
            return
        self.level_status_var.set(
            f"当前 Lv{view.level}（合法范围 1..{MAX_ITEM_LEVEL}；"
            f"游戏可序列化的上限就是 {MAX_ITEM_LEVEL}）。"
            "只改等级本身：存档里同种饰品的词条数值不随等级变化（已实测），"
            "但游戏是否会在读取后按等级重算显示数值无法由存档证明，"
            "所以请谨慎修改，改完进游戏确认。"
        )
        self.level_status_label.configure(foreground="#1a7f37")
        self._set_level_enabled(True)

    # -- 种类 ---------------------------------------------------------------
    def _set_kind_enabled(self, enabled: bool) -> None:
        self.kind_combo.state(["!disabled", "readonly"] if enabled
                              else ["disabled"])
        self.kind_button.state(["!disabled"] if enabled else ["disabled"])

    def _refresh_kind_state(self, view: AccessoryView) -> None:
        """List only the same-中类 kinds this save can actually supply a sample of."""
        if not self.item_db.is_loaded:
            self.kind_status_var.set(
                "不可改：未加载物品种类表（data/accessory_items.json），"
                "无法证明同分类。")
            self.kind_status_label.configure(foreground="#b03030")
            self.kind_combo.set("")
            self._set_kind_enabled(False)
            return
        current = self.item_db.category_of(view.record_type)
        if current is None:
            self.kind_status_var.set(
                f"不可改：当前种类 {view.record_type:#06x} 不在物品种类表内，"
                "无法证明与目标同分类。")
            self.kind_status_label.configure(foreground="#b03030")
            self.kind_combo.set("")
            self._set_kind_enabled(False)
            return
        samples = collect_kind_samples(self.decrypted, affix_db=self.affix_db,
                                        known_ids=self.known_ids,
                                        layout=self.layout)
        choices = []
        for entry in self.item_db.all():
            if entry.category != current or entry.item_id == view.record_type:
                continue
            sample = samples.get(entry.item_id)
            if sample is None or sample.ambiguous:
                continue
            choices.append(entry)
        self.kind_choices = {entry.label: entry for entry in choices}
        self.kind_combo.configure(values=tuple(self.kind_choices))
        self.kind_combo.set("")
        self.kind_status_var.set(
            f"当前 {view.describe_item(self.item_db)}（{current}）；"
            f"存档里可换的同类种类 {len(choices)} 个（只列出本存档已有实例、"
            "且固定词条唯一可复制的种类）。互换会改写种类字段，"
            "并把固定词条按新种类的真实样本同步；普通词条与末位恩宠槽保持不变。"
        )
        self.kind_status_label.configure(foreground="#1a7f37")
        self._set_kind_enabled(bool(choices))

    def apply_kind_to_selection(self) -> None:
        """Swap the selected record's 种类 (memory only; 写入存档 commits)."""
        if not self._require_accessory_selection():
            return
        chosen = self.kind_choices.get(self.kind_combo.get())
        if chosen is None:
            messagebox.showwarning("提示", "请先选择要换成的种类")
            return
        target = self.selected_accessory
        if not messagebox.askokcancel(
            "确认改种类",
            f"把记录 #{target} 换成 {chosen.label}？\n\n"
            "· 只允许同分类（武士饰品↔武士饰品、忍者饰品↔忍者饰品）互换；\n"
            "· 该种类的固定词条会从本存档里同种类的真实样本复制，不是编造的；\n"
            "· 普通词条与末位恩宠/套装槽保持原样；\n"
            "· 改完请进游戏确认；存档写入前会自动备份。",
            icon="warning",
        ):
            return
        try:
            plan = plan_kind_swap(self.decrypted, target, chosen.item_id,
                                  affix_db=self.affix_db, item_db=self.item_db,
                                  known_ids=self.known_ids, layout=self.layout)
            self.decrypted = apply_kind_swaps(self.decrypted, [plan])
        except Exception as error:  # noqa: BLE001 - surfaced through the GUI
            messagebox.showerror("错误", str(error))
            return
        layout = inspect_layout(self.decrypted, known_ids=self.known_ids)
        self._populate_accessories(
            (self.decrypted,
             list_accessories(self.decrypted, layout=layout,
                              known_ids=self.known_ids),
             self.checksum_ok, layout),
            keep_selection=True,
        )
        self.tree.selection_set(str(target))
        self.selected_accessory = target
        self._on_accessory_selected()
        self._status(f"记录 #{target} 的种类已换成 {chosen.name}（尚未写入存档）")

    def apply_level_to_selection(self) -> None:
        """Change the selected record's 等级 (memory only; 写入存档 commits)."""
        if not self._require_accessory_selection():
            return
        text = self.level_var.get().strip()
        try:
            level = int(text, 10)
        except ValueError:
            messagebox.showwarning("提示", f"等级必须是整数：{text!r}")
            return
        target = self.selected_accessory
        if not messagebox.askokcancel(
            "确认修改等级",
            f"把记录 #{target} 的等级改成 {level}？\n\n"
            "· 只写入等级字段（+0x06/+0x08），词条数值不会被改写；\n"
            "· 请谨慎修改：改完请进游戏确认显示与属性是否正常；\n"
            "· 存档写入前会自动备份，出问题可以用「回滚」恢复。",
            icon="warning",
        ):
            return
        known_ids = accessory_catalog_ids(self.affix_db)
        try:
            plan = plan_level_edit(self.decrypted, target, level,
                                   affix_db=self.affix_db, known_ids=known_ids,
                                   layout=self.layout)
            self.decrypted = apply_level_edits(self.decrypted, [plan])
        except Exception as error:  # noqa: BLE001 - surfaced through the GUI
            messagebox.showerror("错误", str(error))
            return
        layout = inspect_layout(self.decrypted, known_ids=known_ids)
        self._populate_accessories(
            (self.decrypted,
             list_accessories(self.decrypted, layout=layout, known_ids=known_ids),
             self.checksum_ok, layout),
            keep_selection=True,
        )
        self.tree.selection_set(str(target))
        self.selected_accessory = target
        self._on_accessory_selected()
        self._status(f"记录 #{target} 的等级已改为 {level}（尚未写入存档）")

    # -- 稀 有 度 -----------------------------------------------------------
    def _set_rarity_enabled(self, enabled: bool) -> None:
        self.rarity_entry.state(["!disabled"] if enabled else ["disabled"])
        self.rarity_button.state(["!disabled"] if enabled else ["disabled"])

    def _refresh_rarity_state(self, view: AccessoryView) -> None:
        """Enable the 稀有度 row for the selected record and show its current value.

        切记录时**这里重设值与范围**（`_on_accessory_selected` 每次都会调），
        所以上一条记录的值不会残留。
        """
        cap = self.rarity_cap_for_record(view.record_type)
        color = rarity_color_of(view.rarity)
        colored = f"（{color}）" if color else ""
        if cap is None:
            self.rarity_var.set("")
            self.rarity_frame.configure(
                text=f"稀有度（{view.record_type:#06x} 不在物品总目录里）")
            self.rarity_status_var.set(
                f"不可改：记录 #{view.slot_index} 的种类 {view.record_type:#06x} "
                "不在物品总目录（data/equipment_items.json）里，无法确定稀有度上限。")
            self.rarity_status_label.configure(foreground="#b03030")
            self._set_rarity_enabled(False)
            return
        self.rarity_var.set(str(view.rarity))
        self.rarity_frame.configure(text=f"稀有度（0..{cap}，字段 +0x30）")
        self.rarity_status_var.set(
            f"当前 {view.rarity}{colored}（{view.rarity_name}）。范围 0..{cap}"
            f"（按大类「饰品」）；颜色对照 {RARITY_COLOR_HINT}；"
            f"橙色（5）在当前周目不可达。{limits.describe_origin()}")
        self.rarity_status_label.configure(foreground="#1a7f37")
        self._set_rarity_enabled(True)

    def apply_rarity_to_selection(self) -> None:
        """Change the selected record's 稀有度 (memory only; 写入存档 commits)."""
        if not self._require_accessory_selection():
            return
        text = self.rarity_var.get().strip()
        try:
            rarity = int(text, 10)
        except ValueError:
            messagebox.showwarning("提示", f"稀有度必须是整数：{text!r}")
            return
        target = self.selected_accessory
        view = self._selected_view()
        cap = self.rarity_cap_for_record(view.record_type) if view else None
        cap_text = "（这条记录的种类不在物品总目录里，引擎会拒绝）" if cap is None \
            else f"0..{cap}"
        if not messagebox.askokcancel(
            "确认修改稀有度",
            f"把记录 #{target} 的稀有度改成 {rarity}？\n\n"
            "· 只写入品质字段（+0x30 的低 4 位；写成 0 时才顺带清 +0x31 的低 4 位），"
            "词条、等级、+值、标识都不动；\n"
            f"· 合法范围 {cap_text}（按大类取，上限表见 limits.py）；\n"
            f"· 颜色对照 {RARITY_COLOR_HINT}；橙色（5）在当前周目不可达；\n"
            "· 存档写入前会自动备份，出问题可以用「回滚」恢复。",
            icon="warning",
        ):
            return
        known_ids = accessory_catalog_ids(self.affix_db)
        try:
            plan = plan_rarity_edit(self.decrypted, target, rarity,
                                    affix_db=self.affix_db, known_ids=known_ids,
                                    layout=self.layout)
            self.decrypted = apply_rarity_edits(self.decrypted, [plan])
        except Exception as error:  # noqa: BLE001 - surfaced through the GUI
            messagebox.showerror("错误", str(error))
            return
        layout = inspect_layout(self.decrypted, known_ids=known_ids)
        self._populate_accessories(
            (self.decrypted,
             list_accessories(self.decrypted, layout=layout, known_ids=known_ids),
             self.checksum_ok, layout),
            keep_selection=True,
        )
        self.tree.selection_set(str(target))
        self.selected_accessory = target
        self._on_accessory_selected()
        self._status(f"记录 #{target} 的稀有度已改为 {rarity}（尚未写入存档）")

    def apply_plus_to_selection(self) -> None:
        """Change the selected record's +值 (memory only; 写入存档 commits)."""
        if not self._require_accessory_selection():
            return
        text = self.plus_var.get().strip()
        try:
            value = int(text, 10)
        except ValueError:
            messagebox.showwarning("提示", f"+值必须是整数：{text!r}")
            return
        target = self.selected_accessory
        if not messagebox.askokcancel(
            "确认修改 +值",
            f"把记录 #{target} 的 +值改成 {value}？\n\n"
            "· 只写入 +值 字段（+0x0A）与校验和，词条、等级、标识都不动；\n"
            f"· 合法范围 0..{MAX_RECORD_PLUS}（实测范围）；\n"
            "· 该字段已在游戏内确认就是物品卡上的 +值，改完请进游戏复核；\n"
            "· 存档写入前会自动备份，出问题可以用「回滚」恢复。",
            icon="warning",
        ):
            return
        known_ids = accessory_catalog_ids(self.affix_db)
        try:
            plan = plan_plus_edit(self.decrypted, target, value,
                                  affix_db=self.affix_db, known_ids=known_ids,
                                  layout=self.layout)
            self.decrypted = apply_plus_edits(self.decrypted, [plan])
        except Exception as error:  # noqa: BLE001 - surfaced through the GUI
            messagebox.showerror("错误", str(error))
            return
        layout = inspect_layout(self.decrypted, known_ids=known_ids)
        self._populate_accessories(
            (self.decrypted,
             list_accessories(self.decrypted, layout=layout, known_ids=known_ids),
             self.checksum_ok, layout),
            keep_selection=True,
        )
        self.tree.selection_set(str(target))
        self.selected_accessory = target
        self._on_accessory_selected()
        self._status(f"记录 #{target} 的 +值已改为 {value}（尚未写入存档）")

    # -- 写入门禁：游戏运行中必须确认“停在标题界面” ------------------------
    def _on_title_screen_toggled(self) -> None:
        """Reflect the acknowledgement in the status line as soon as it changes."""
        if self.title_screen_var.get():
            self._status("已确认游戏停在标题界面——现在允许写入存档"
                         "（每次写入后该确认会自动取消）。")
        else:
            self._status("已取消“停在标题界面”的确认；游戏运行中写入会被拒绝。")

    def _title_screen_confirmed(self) -> bool:
        """True when the game is not running, or the operator ticked the box.

        The game only holds a save in memory after it has been loaded, so writing
        from the title screen cannot be overwritten by it.  Without the tick, a
        running game means refusal — there is no silent override.
        """
        return not running_game_processes() or bool(self.title_screen_var.get())

    def _refuse_running_game(self, what: str) -> bool:
        """Return True when the write must stop, after telling the user why."""
        running = running_game_processes()
        if self._title_screen_confirmed():
            return False
        messagebox.showwarning(
            "游戏正在运行",
            "检测到 " + "、".join(running) + " 正在运行。\n\n"
            f"{what}只能在下面两种情况下进行：\n"
            "  1. 完全退出游戏；或\n"
            "  2. 游戏停在【标题界面】（尚未载入存档），并在窗口底部勾选\n"
            "     「我确认：游戏正在运行，但停留在标题界面」这个确认框。\n\n"
            "在游戏内的存档中写入会被游戏下次保存覆盖，所以这里直接拒绝。",
        )
        self._status(f"已拒绝{what}：游戏正在运行且未勾选“停在标题界面”的确认。")
        return True

    def backup_save(self) -> None:
        if self.selected_save is None:
            messagebox.showwarning("提示", "请先选择存档")
            return
        save = self.selected_save
        crypto = self.crypto

        def worker() -> tuple[str, str]:
            return "backup", str(create_backup(save.path, state_root=self.state_root,
                                               crypto=crypto))

        self._run_worker(worker)

    # ------------------------------------------------------------- restore

    def restore_save(self) -> None:
        """Pick a plaintext backup and put it back into the save file."""
        if self.selected_save is None:
            messagebox.showwarning("提示", "请先选择存档")
            return
        save = self.selected_save
        try:
            entries = list_backups(save.path, self.state_root)
        except (SaveError, ValueError) as error:
            messagebox.showinfo(
                "无法定位备份目录",
                f"该存档路径无法对应到备份目录：\n{save.path}\n\n{error}",
            )
            return
        if not entries:
            messagebox.showinfo(
                "没有可用备份",
                "该存档还没有备份。\n\n"
                f"备份目录：\n{self._backup_directory_hint(save.path)}\n\n"
                "写入存档前会自动备份，也可以用「备份存档」先手动备份一份。",
            )
            return
        chosen = self._choose_backup_dialog(entries)
        if chosen is None:
            return
        self._restore_selected_backup(save, chosen)

    def _backup_directory_hint(self, save_path: Path) -> str:
        """The backup directory for a save, or an explanation when unknowable.

        The path shape (``<state root>/account-<id>/slot-<NN>``) needs the Steam
        account id from the save path, which a hand-copied file elsewhere on disk
        does not carry.
        """
        try:
            return str(backup_directory_for(save_path, self.state_root))
        except (SaveError, ValueError):
            return ("（无法从该路径识别账号/栏位；可在 config/editor.json 中"
                    "用 backup_root 指定备份目录）")

    def _choose_backup_dialog(self, entries: tuple[BackupEntry, ...]) -> BackupEntry | None:
        """Modal list of backups (newest first); returns the picked entry."""
        dialog = tk.Toplevel(self)
        dialog.title("选择要恢复的备份")
        dialog.transient(self)
        dialog.geometry("720x360")
        ttk.Label(
            dialog,
            text=("选择一个备份恢复。备份是解密后的明文，恢复时会重新加密写回存档；\n"
                  "恢复前会自动把当前存档另存一份，因此恢复本身也可以撤销。"),
            justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=10, pady=(10, 4))

        frame = ttk.Frame(dialog)
        frame.pack(fill=tk.BOTH, expand=True, padx=10)
        listbox = tk.Listbox(frame, height=10, activestyle="dotbox")
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=listbox.yview)
        listbox.configure(yscrollcommand=scrollbar.set)
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        for index, entry in enumerate(entries, start=1):
            note = "" if entry.integrity_ok else "  ⚠ 校验不符"
            listbox.insert(tk.END, f"{index:>3}. {entry.when}   "
                                   f"{entry.plain_size / 1024 / 1024:.1f} MB{note}")
        listbox.selection_set(0)
        listbox.see(0)

        ttk.Label(dialog, text=f"备份目录：{entries[0].plain_path.parent}",
                  foreground="#666666", wraplength=680,
                  justify=tk.LEFT).pack(anchor=tk.W, padx=10, pady=(4, 0))
        chosen: list[BackupEntry] = []

        def accept() -> None:
            selection = listbox.curselection()
            if not selection:
                return
            chosen.append(entries[selection[0]])
            dialog.destroy()

        buttons = ttk.Frame(dialog, padding=(10, 8))
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="恢复此备份", command=accept).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side=tk.RIGHT, padx=6)
        listbox.bind("<Double-Button-1>", lambda _event: accept())
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)

        dialog.grab_set()
        self.wait_window(dialog)
        return chosen[0] if chosen else None

    def _restore_selected_backup(self, save: SaveDescriptor, entry: BackupEntry) -> None:
        if self._refuse_running_game("恢复备份"):
            return
        running = running_game_processes()
        warning = ""
        if entry.slot_index is not None and entry.slot_index != save.slot_index:
            warning += (f"\n\n⚠ 该备份来自栏位 {entry.slot_index:02d}，"
                        f"当前存档是栏位 {save.slot_index:02d}——请确认内容无误。")
        if running:
            warning += ("\n\n检测到 " + "、".join(running) + " 正在运行，"
                        "你已确认它停在【标题界面】；"
                        "若其实已载入过存档，请先完全退出游戏，"
                        "否则恢复的内容会被游戏下次保存覆盖。")
        if not messagebox.askyesno(
            "确认恢复",
            f"将用以下备份覆盖当前存档：\n{entry.plain_path}\n"
            f"备份时间：{entry.when}\n\n"
            + SAVE_WRITE_REQUIREMENT + "\n\n"
            "（当前存档会先自动备份一份，可再恢复回来。）\n\n"
            + DISCLAIMER + warning + "\n\n确认继续吗？",
        ):
            return

        dry_run = bool(self.dry_run_var.get())
        verify = bool(self.verify_var.get())
        crypto = self.crypto

        def worker() -> tuple[str, dict]:
            result = restore_backup(
                save, entry, crypto=crypto, state_root=self.state_root,
                dry_run=dry_run, verify=verify, allow_game_running=True,
            )
            return ("restore_dry_run" if result.get("dry_run") else "restored"), result

        self._run_worker(worker)

    def write_save(self) -> None:
        if self.decrypted is None or self.selected_save is None:
            messagebox.showwarning("提示", "请先读取数据")
            return
        if self._refuse_running_game("写入存档"):
            return
        running = running_game_processes()
        warning = ""
        if running:
            warning = (
                "\n\n你已确认游戏停在【标题界面】。\n"
                "如果你其实已经载入过存档（哪怕现在回到标题），请先完全退出游戏，"
                "否则写进去的修改会被游戏下次保存覆盖。"
            )
        if not messagebox.askyesno(
            "确认写入",
            "即将把修改写入存档（会自动备份原存档）。\n\n"
            + SAVE_WRITE_REQUIREMENT + "\n\n"
            + DISCLAIMER + warning + "\n\n确认继续吗？",
        ):
            return

        dry_run = bool(self.dry_run_var.get())
        verify = bool(self.verify_var.get())
        save = self.selected_save
        data = self.decrypted
        crypto = self.crypto

        def worker() -> tuple[str, dict]:
            result = commit_save(
                save, data, crypto=crypto, state_root=self.state_root,
                dry_run=dry_run, verify=verify, allow_game_running=True,
            )
            return ("dry_run" if result.get("dry_run") else "written"), result

        self._run_worker(worker)


def _hide_own_console() -> None:
    """Hide the console a double-clicked frozen build starts with.

    A console-subsystem executable gets a console window whether or not it was
    started from a terminal.  The window is hidden only when this process is the
    sole owner of that console, so running ``Nioh3EquipmentAffixEditor.exe`` from an
    existing shell never hides the user's own terminal.  Best effort by design:
    a failure here must not stop the GUI from opening.
    """
    if not paths.is_frozen() or os.name != "nt":
        return
    try:
        import ctypes  # noqa: PLC0415 - Windows-only, imported on demand

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        process_ids = (ctypes.c_ulong * 4)()
        attached = kernel32.GetConsoleProcessList(process_ids, 4)
        if attached <= 1:
            window = kernel32.GetConsoleWindow()
            if window:
                ctypes.WinDLL("user32", use_last_error=True).ShowWindow(window, 0)
    except Exception:  # noqa: BLE001 - cosmetic only
        return


def main(config: EditorConfig | None = None) -> int:
    if config is None:
        try:
            config = load_config()
        except ConfigError as error:
            messagebox.showerror("配置文件错误", str(error))
            return 2
    _hide_own_console()
    app = AccessoryEditorApp(config)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
