"""Tkinter GUI for Nioh3AccessoryEditor (zero third-party dependencies).

Workflow:
1. 刷新 -> discover saves, decrypt the selected one, scan accessory records.
2. Pick an accessory record in the tree, edit its 7 affix slots with searchable
   legal-affix dropdowns, click 应用修改, then 写入存档.
3. Every write: quiescence re-check, checksum recompute, plaintext backup,
   staged verification, atomic durable replacement, post-write verification.

The window shows the required disclaimer permanently.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from .affixdb import AffixDb
from .editor import (
    AccessoryView,
    SaveDescriptor,
    apply_edits,
    commit_save,
    discover_saves,
    list_accessories,
    open_save,
    save_checksum_is_valid,
)
from .records import EFFECT_COUNT, EMPTY_EFFECT_ID, EffectSlot
from .savefile import SaveCrypto, create_backup, running_game_processes

DISCLAIMER = (
    "仅供测试学习用，不要用于联机影响游戏平衡。\n"
    "仁王3 为单机/纯 PVE 联机游戏，本工具不会影响其他玩家。"
)

TITLE = "仁王3 饰品词条修改器（仅供测试学习用）"
EMPTY_LABEL = "(空)"


class UiEditError(ValueError):
    """Raised when the current widget selection cannot be turned into an edit."""


class AccessoryEditorApp(tk.Tk):
    """Main window: save selection, record list, affix slots, write actions."""

    def __init__(self) -> None:
        super().__init__()
        self.title(TITLE)
        self.geometry("1000x700")
        self.minsize(880, 620)

        self.affix_db = AffixDb()
        self.crypto = SaveCrypto()
        self.saves: list[SaveDescriptor] = []
        self.accessory_views: list[AccessoryView] = []
        self.decrypted: bytes | None = None
        self.selected_save: SaveDescriptor | None = None
        self.selected_accessory: int | None = None
        self.checksum_ok = False
        self.worker_queue: queue.Queue[tuple[str, object]] = queue.Queue()

        self._build_ui()
        self.after(80, self._poll_worker)
        self.refresh_saves()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=(8, 6))
        top.pack(fill=tk.X)

        ttk.Label(top, text="存档:").pack(side=tk.LEFT)
        self.save_combo = ttk.Combobox(top, state="readonly", width=60)
        self.save_combo.pack(side=tk.LEFT, padx=4)
        self.save_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_save_selected())
        ttk.Button(top, text="刷新", command=self.refresh_saves).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="读取饰品", command=self.load_accessories).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="备份存档", command=self.backup_save).pack(side=tk.LEFT, padx=2)

        mid = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        mid.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        left = ttk.Frame(mid)
        ttk.Label(left, text="饰品记录（选择后编辑右侧词条槽）").pack(anchor=tk.W)
        self.tree = ttk.Treeview(
            left, columns=("level", "rarity", "type"), show="tree headings", height=16,
        )
        self.tree.heading("#0", text="记录")
        self.tree.heading("level", text="等级")
        self.tree.heading("rarity", text="品质")
        self.tree.heading("type", text="类型")
        self.tree.column("level", width=52, anchor=tk.CENTER)
        self.tree.column("rarity", width=76, anchor=tk.CENTER)
        self.tree.column("type", width=76, anchor=tk.CENTER)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._on_accessory_selected())
        mid.add(left, weight=2)

        right = ttk.Frame(mid)
        ttk.Label(right, text="词条槽（选择词条后点 应用修改）").pack(anchor=tk.W)
        slot_frame = ttk.Frame(right)
        slot_frame.pack(fill=tk.BOTH, expand=True)
        self.slot_combos: list[ttk.Combobox] = []
        self.slot_labels: list[tk.StringVar] = []
        for index in range(EFFECT_COUNT):
            row = ttk.Frame(slot_frame)
            row.pack(fill=tk.X, pady=1)
            ttk.Label(row, text=f"槽{index + 1}:", width=5).pack(side=tk.LEFT)
            combo = ttk.Combobox(row, state="readonly", width=62,
                                 values=self.affix_db.labels())
            combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
            combo.bind("<<ComboboxSelected>>",
                       lambda _event, slot=index: self._on_slot_picked(slot))
            self.slot_combos.append(combo)
            self.slot_labels.append(tk.StringVar(value=""))
            ttk.Label(row, textvariable=self.slot_labels[index],
                      foreground="#666666").pack(side=tk.LEFT)

        note = ttk.Label(
            right,
            text=("说明：词条选择来自《仁王3词条装备库v2.21》饰品词条表，"
                  "非表内词条一律拒绝。选择词条会写入该词条的 ID 与标称数值；"
                  "标识(metadata) 位不会被改写，因为其在存档中的编码尚未核实。"),
            foreground="#666666", wraplength=520, justify=tk.LEFT,
        )
        note.pack(anchor=tk.W, pady=(6, 0))
        mid.add(right, weight=3)

        bottom = ttk.Frame(self, padding=(8, 4))
        bottom.pack(fill=tk.X)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bottom, textvariable=self.status_var, foreground="#0366d6").pack(side=tk.LEFT)
        self.dry_run_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bottom, text="仅演练（不写回）",
                        variable=self.dry_run_var).pack(side=tk.LEFT, padx=6)
        self.verify_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bottom, text="写入校验",
                        variable=self.verify_var).pack(side=tk.LEFT, padx=6)
        ttk.Button(bottom, text="应用修改",
                   command=self.apply_edits_to_selection).pack(side=tk.LEFT, padx=2)
        ttk.Button(bottom, text="写入存档", command=self.write_save).pack(side=tk.LEFT, padx=2)

        ttk.Separator(self).pack(fill=tk.X)
        ttk.Label(self, text=DISCLAIMER, foreground="#b30000",
                  justify=tk.LEFT).pack(fill=tk.X, padx=8, pady=4)

    # ------------------------------------------------------------- helpers

    def _status(self, text: str) -> None:
        self.status_var.set(text)

    def _run_worker(self, function, *args) -> None:
        def runner() -> None:
            try:
                self.worker_queue.put(("ok", function(*args)))
            except Exception as error:  # noqa: BLE001 - surfaced through the GUI
                self.worker_queue.put(("error", error))

        threading.Thread(target=runner, daemon=True).start()
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
        elif tag == "written" and isinstance(value, dict):
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

    def _on_worker_error(self, error: Exception) -> None:
        self._status("操作失败")
        messagebox.showerror("错误", str(error))

    # ------------------------------------------------------------- actions

    def refresh_saves(self) -> None:
        self._run_worker(self._load_saves)

    @staticmethod
    def _load_saves() -> tuple[str, tuple[SaveDescriptor, ...]]:
        return "saves", discover_saves()

    def _populate_saves(self, saves: object) -> None:
        self.saves = list(saves) if saves else []
        self.save_combo["values"] = [save.display for save in self.saves]
        if self.saves:
            self.save_combo.current(0)
            self.selected_save = self.saves[0]
            self._status(f"发现 {len(self.saves)} 个存档")
        else:
            self.selected_save = None
            self._status("未发现存档")

    def _on_save_selected(self) -> None:
        index = self.save_combo.current()
        if 0 <= index < len(self.saves):
            self.selected_save = self.saves[index]
            self._status(f"已选择 {self.saves[index].display}")

    def load_accessories(self) -> None:
        if self.selected_save is None:
            messagebox.showwarning("提示", "请先选择存档")
            return
        self._run_worker(self._load_accessories_worker, self.selected_save)

    def _load_accessories_worker(self, save: SaveDescriptor) -> tuple[str, tuple]:
        data = open_save(save, self.crypto)
        return "accessories", (data, list_accessories(data),
                              save_checksum_is_valid(data))

    def _populate_accessories(self, payload: object, *, keep_selection: bool = False) -> None:
        data, views, checksum_ok = payload
        self.decrypted = data
        self.checksum_ok = bool(checksum_ok)
        self.accessory_views = list(views)
        self.tree.delete(*self.tree.get_children())
        for view in self.accessory_views:
            self.tree.insert(
                "", "end", iid=str(view.slot_index),
                text=f"#{view.slot_index} @ {view.offset:#x}",
                values=(view.level, view.rarity_name, f"{view.record_type:#06x}"),
            )
        known = {view.slot_index for view in self.accessory_views}
        if not keep_selection or self.selected_accessory not in known:
            self.selected_accessory = None
        for index in range(EFFECT_COUNT):
            self.slot_combos[index].set("")
            self.slot_labels[index].set("")
        suffix = "" if self.checksum_ok else "（校验和不一致，请谨慎）"
        self._status(f"已读取 {len(self.accessory_views)} 条记录{suffix}")

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
        for index, effect in enumerate(view.effects):
            if effect.is_empty:
                self.slot_combos[index].set(EMPTY_LABEL)
                self.slot_labels[index].set("")
            else:
                entry = self.affix_db.lookup(effect.effect_id)
                label = entry.label if entry else f"{effect.effect_id:#06x} (非表内词条)"
                self.slot_combos[index].set(label)
                self.slot_labels[index].set(
                    f"数值={effect.value} 标识={effect.metadata:#010x}"
                )

    def _on_slot_picked(self, index: int) -> None:
        text = self.slot_combos[index].get()
        self.slot_labels[index].set("" if text == EMPTY_LABEL else text)

    def _pending_edit(self, index: int, current: EffectSlot) -> dict[str, int] | None:
        """Turn one slot widget into an edit, or ``None`` when nothing changed.

        Picking a legal affix writes the affix's 词条代码 into the slot: its
        effect id plus the catalog's nominal value (both live at the same
        relative offsets in the code and in the slot).  ``metadata`` is left
        untouched on purpose -- the slot-level encoding of the 固定/星 flag bits
        is not confirmed against a real save, so the GUI never guesses at it
        (the CLI can still set it explicitly with ``--edit slot:id:value:meta``).
        """
        text = self.slot_combos[index].get()
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
            return None  # unchanged: leave value/metadata untouched
        entry = self.affix_db.lookup(effect_id)
        if entry is None:
            raise UiEditError(f"词条 {effect_id:#06x} 不在合法的饰品词条表中")
        return {
            "slot_index": index,
            "effect_id": effect_id,
            "value": entry.value,
        }

    def _current_edits(self) -> tuple[dict[str, int], ...]:
        """Collect the changed slots for the selected accessory record."""
        if self.selected_accessory is None:
            return ()
        view = self._selected_view()
        if view is None:
            return ()
        edits: list[dict[str, int]] = []
        for index in range(EFFECT_COUNT):
            try:
                edit = self._pending_edit(index, view.effects[index])
            except UiEditError as error:
                messagebox.showwarning("提示", str(error))
                return ()
            if edit is not None:
                edits.append(dict(edit, record_index=self.selected_accessory))
        return tuple(edits)

    def apply_edits_to_selection(self) -> None:
        if self.decrypted is None or self.selected_accessory is None:
            messagebox.showwarning("提示", "请先读取饰品并选择一条记录")
            return
        target = self.selected_accessory
        edits = self._current_edits()
        if not edits:
            messagebox.showwarning("提示", "当前没有检测到改动")
            return
        try:
            self.decrypted = apply_edits(self.decrypted, edits, affix_db=self.affix_db)
        except Exception as error:  # noqa: BLE001 - surfaced through the GUI
            messagebox.showerror("错误", str(error))
            return
        # Refresh the tree from the patched memory image so the display matches
        # exactly what a write would persist.  The on-disk checksum is stale by
        # definition until commit, so keep the checksum verdict from load time.
        self._populate_accessories(
            (self.decrypted, list_accessories(self.decrypted), self.checksum_ok),
            keep_selection=True,
        )
        # Restore the selection explicitly: <<TreeviewSelect>> is not guaranteed
        # to fire synchronously, and the slot widgets must mirror the new state.
        self.tree.selection_set(str(target))
        self.selected_accessory = target
        self._on_accessory_selected()
        self._status(f"记录 #{target} 的修改已应用到内存数据（尚未写入存档）")

    def backup_save(self) -> None:
        if self.selected_save is None:
            messagebox.showwarning("提示", "请先选择存档")
            return
        save = self.selected_save
        crypto = self.crypto

        def worker() -> tuple[str, str]:
            return "backup", str(create_backup(save.path, state_root=Path.cwd(),
                                               crypto=crypto))

        self._run_worker(worker)

    def write_save(self) -> None:
        if self.decrypted is None or self.selected_save is None:
            messagebox.showwarning("提示", "请先读取饰品")
            return
        running = running_game_processes()
        warning = ""
        if running:
            warning = (
                "\n\n⚠ 检测到游戏正在运行（" + "、".join(running) + "）。\n"
                "游戏可能持有存档并在退出时覆盖本次修改，强烈建议先退出游戏。"
            )
            if not messagebox.askyesno("游戏正在运行", "仍要继续写入吗？" + warning):
                return
        if not messagebox.askyesno(
            "确认写入",
            "即将把修改写入存档（会自动备份原存档）。\n\n"
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
                save, data, crypto=crypto, state_root=Path.cwd(),
                dry_run=dry_run, verify=verify, allow_game_running=True,
            )
            return ("dry_run" if result.get("dry_run") else "written"), result

        self._run_worker(worker)


def main() -> int:
    app = AccessoryEditorApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
