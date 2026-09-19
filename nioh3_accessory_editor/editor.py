"""Core edit service: discover -> decrypt -> list -> edit -> checksum -> save.

The service mirrors Nioh3-Scroll-Generator's save-application flow:

1. Discover save paths under ``%LOCALAPPDATA%/KoeiTecmo/NIOH3/Savedata``.
2. Decrypt the selected save (reference exe, or pure-Python fallback).
3. Scan the fixed record region for accessory-like item records.
4. Apply requested effect-slot edits (validated against the legal affix DB).
5. Recompute the user checksum, re-encrypt, verify, and durably replace the
   save after a quiescence re-check, with a plaintext backup + manifest.

All writes fail closed: an unknown layout, a changed save, an invalid affix, a
missing target record, or a failed verification aborts before (or rolls back
after) anything is modified.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import records
from .affixdb import AffixDb
from .checksum import patch_user_checksum, verify_user_checksum
from .crypto import USER_SAVE_SIZE
from .savefile import (
    SaveCrypto,
    account_id_from_save_path,
    capture_quiescent_save_fingerprints,
    create_backup,
    decrypt_save_to_bytes,
    discover_save_paths,
    require_game_not_running,
    save_slot_index_from_path,
    write_encrypted_save,
)

__all__ = [
    "AccessoryView",
    "EditPlan",
    "EditorError",
    "SaveDescriptor",
    "apply_edits",
    "commit_save",
    "discover_saves",
    "list_accessories",
    "open_save",
    "plan_edits",
]


class EditorError(RuntimeError):
    """Raised for any editor-level failure."""


@dataclass(frozen=True, slots=True)
class SaveDescriptor:
    path: Path
    account_id: int
    slot_index: int
    size: int

    @property
    def display(self) -> str:
        return f"账号 {self.account_id} / 栏位 {self.slot_index:02d} / {self.size:#x} 字节"


@dataclass(frozen=True, slots=True)
class AccessoryView:
    """User-facing view of one accessory record."""

    slot_index: int
    offset: int
    record_type: int
    level: int
    rarity: int
    rarity_name: str
    account_id: int
    effects: tuple[records.EffectSlot, ...]

    @property
    def occupied_effects(self) -> tuple[records.EffectSlot, ...]:
        return tuple(effect for effect in self.effects if not effect.is_empty)

    def describe_effects(self, affix_db: AffixDb) -> tuple[str, ...]:
        lines: list[str] = []
        for effect in self.effects:
            if effect.is_empty:
                lines.append(f"  [{effect.slot_index}] (空)")
                continue
            lines.append(
                f"  [{effect.slot_index}] {affix_db.describe(effect.effect_id)} "
                f"(id={effect.effect_id:#06x} 数值={effect.value} "
                f"标识={effect.metadata:#010x})"
            )
        return tuple(lines)


@dataclass(frozen=True, slots=True)
class EditPlan:
    """Validated edit set plus the record it targets."""

    record_index: int
    edits: tuple[dict[str, int], ...]
    before: tuple[records.EffectSlot, ...]
    after: tuple[records.EffectSlot, ...]


# --------------------------------------------------------------------------
# Discovery / reading
# --------------------------------------------------------------------------

def discover_saves() -> tuple[SaveDescriptor, ...]:
    """Return every discoverable USR save, newest account/slot first."""
    descriptors: list[SaveDescriptor] = []
    for path in discover_save_paths():
        try:
            size = path.stat().st_size
        except OSError:
            continue
        descriptors.append(
            SaveDescriptor(
                path=path,
                account_id=account_id_from_save_path(path),
                slot_index=save_slot_index_from_path(path),
                size=size,
            )
        )
    return tuple(descriptors)


def open_save(save: SaveDescriptor, crypto: SaveCrypto) -> bytes:
    """Decrypt a save and return its RNNUSR bytes."""
    return decrypt_save_to_bytes(save.path, crypto)


def list_accessories(decrypted: bytes) -> tuple[AccessoryView, ...]:
    """Parse accessory-like records from the fixed record region."""
    return tuple(
        AccessoryView(
            slot_index=record.slot_index,
            offset=record.offset,
            record_type=record.record_type,
            level=record.level,
            rarity=record.rarity,
            rarity_name=record.rarity_name,
            account_id=record.account_id,
            effects=record.effects,
        )
        for record in records.iter_item_records(decrypted)
    )


def save_checksum_is_valid(decrypted: bytes) -> bool:
    """Return whether the decrypted save carries a consistent checksum."""
    return verify_user_checksum(decrypted)


# --------------------------------------------------------------------------
# Editing
# --------------------------------------------------------------------------

def _validate_edit(edit: dict[str, int], affix_db: AffixDb) -> dict[str, int]:
    """Fail closed: every edited slot must reference a legal accessory affix."""
    if not isinstance(edit, dict):
        raise EditorError("编辑项必须是字典")
    record_index = edit.get("record_index")
    slot_index = edit.get("slot_index")
    if record_index is None:
        raise EditorError("编辑缺少目标记录索引 (record_index)")
    if not isinstance(record_index, int) or isinstance(record_index, bool):
        raise EditorError("record_index 必须是整数")
    if slot_index is None:
        raise EditorError("编辑缺少效果槽索引 (slot_index)")
    if not isinstance(slot_index, int) or isinstance(slot_index, bool):
        raise EditorError("slot_index 必须是整数")
    if not 0 <= slot_index < records.EFFECT_COUNT:
        raise EditorError(f"效果槽索引必须位于 0..{records.EFFECT_COUNT - 1}")

    effect_id = edit.get("effect_id")
    if effect_id is not None:
        if not isinstance(effect_id, int) or isinstance(effect_id, bool):
            raise EditorError("effect_id 必须是整数")
        if effect_id != records.EMPTY_EFFECT_ID:
            affix_db.require(effect_id)
    # Reuse the record-layer validation for the remaining fields and ranges.
    stripped = {key: value for key, value in edit.items() if key != "record_index"}
    records.patch_effect_slots(bytes(records.SCROLL_RECORD_SIZE), [stripped])
    return edit


def plan_edits(
    decrypted: bytes,
    edits: tuple[dict[str, int], ...] | list[dict[str, int]],
    *,
    affix_db: AffixDb,
) -> tuple[EditPlan, ...]:
    """Validate edits against the live save and return per-record plans.

    Raises if a referenced record does not exist, if a slot index is invalid,
    or if the affix is outside the legal accessory table.
    """
    if not edits:
        raise EditorError("至少需要一个编辑项")
    normalized = tuple(_validate_edit(dict(edit), affix_db) for edit in edits)

    known = {view.slot_index: view for view in list_accessories(decrypted)}
    missing = sorted({edit["record_index"] for edit in normalized} - set(known))
    if missing:
        raise EditorError(
            "以下记录不在当前存档的饰品记录中："
            + "、".join(f"#{index}" for index in missing)
        )

    by_record: dict[int, list[dict[str, int]]] = {}
    for edit in normalized:
        by_record.setdefault(edit["record_index"], []).append(edit)

    plans: list[EditPlan] = []
    for record_index, record_edits in sorted(by_record.items()):
        view = known[record_index]
        record = records.read_item_record(decrypted, record_index)
        if record is None:
            raise EditorError(f"记录 #{record_index} 无法解析为饰品记录")
        patched = records.patch_effect_slots(
            record.record,
            [dict(edit, record_index=record_index) for edit in record_edits],
            allow_record_index=True,
        )
        plans.append(
            EditPlan(
                record_index=record_index,
                edits=tuple(record_edits),
                before=view.effects,
                after=records.read_effect_slots(patched),
            )
        )
    return tuple(plans)


def apply_edits(
    decrypted: bytes,
    edits: tuple[dict[str, int], ...] | list[dict[str, int]],
    *,
    affix_db: AffixDb,
) -> bytes:
    """Return new save bytes with validated effect edits applied."""
    plans = plan_edits(decrypted, edits, affix_db=affix_db)
    output = bytearray(decrypted)
    for plan in plans:
        offset = records.record_offset(plan.record_index)
        record = bytes(output[offset:offset + records.SCROLL_RECORD_SIZE])
        patched = records.patch_effect_slots(
            record,
            [dict(edit, record_index=plan.record_index) for edit in plan.edits],
            allow_record_index=True,
        )
        output[offset:offset + records.SCROLL_RECORD_SIZE] = patched

    # Verify the write landed exactly where the plan said it would.
    for plan in plans:
        offset = records.record_offset(plan.record_index)
        applied = records.read_effect_slots(
            bytes(output[offset:offset + records.SCROLL_RECORD_SIZE])
        )
        if applied != plan.after:
            raise EditorError(
                f"记录 #{plan.record_index} 的修改未能正确写入，已中止"
            )
    return bytes(output)


# --------------------------------------------------------------------------
# Commit
# --------------------------------------------------------------------------

def commit_save(
    save: SaveDescriptor,
    decrypted: bytes,
    *,
    crypto: SaveCrypto,
    state_root: Path,
    dry_run: bool = False,
    verify: bool = True,
    allow_game_running: bool = False,
) -> dict[str, object]:
    """Checksum, encrypt, back up, verify, and durably install the save.

    ``dry_run`` performs every check except creating a backup and replacing the
    file, and reports what a real run would do.
    """
    if not decrypted.startswith(b"RNNUSR"):
        raise EditorError("待提交数据不是有效的解密存档 (RNNUSR)")
    if len(decrypted) != USER_SAVE_SIZE:
        raise EditorError(
            f"待提交数据大小 {len(decrypted):#x} 不是标准存档大小 {USER_SAVE_SIZE:#x}"
        )

    # Measured on the buffer handed to us: after an edit the stored checksum is
    # expected to be stale, which is exactly why we recompute it below.
    input_checksum_was_consistent = verify_user_checksum(decrypted)
    # Single gate shared with the rest of the codebase: writes land in the file
    # on disk, so a running game holding that save in memory is refused.
    running = require_game_not_running(allow_running=allow_game_running)

    # Recompute the user checksum over the patched body.
    patched = bytearray(decrypted)
    old_checksum, new_checksum = patch_user_checksum(patched)

    expected = capture_quiescent_save_fingerprints(save.path)
    report: dict[str, object] = {
        "dry_run": bool(dry_run),
        "path": str(save.path),
        "checksum_before": f"{old_checksum:#010x}",
        "checksum_after": f"{new_checksum:#010x}",
        "checksum_was_consistent": input_checksum_was_consistent,
        "game_processes_running": list(running),
        "verified": False,
    }
    if dry_run:
        report["backup_dir"] = None
        report["new_sha256"] = None
        return report

    backup_dir = create_backup(
        save.path,
        state_root=state_root,
        crypto=crypto,
        expected_fingerprints=expected,
    )
    new_sha256 = write_encrypted_save(
        save.path,
        bytes(patched),
        crypto=crypto,
        expected_fingerprints=expected,
        verify=verify,
    )
    report.update(
        backup_dir=str(backup_dir),
        new_sha256=new_sha256,
        verified=bool(verify),
    )
    return report
