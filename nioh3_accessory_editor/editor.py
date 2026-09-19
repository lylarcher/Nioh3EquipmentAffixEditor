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
    UNCOVERED_TAIL_BYTES,
    BackupEntry,
    SaveCrypto,
    account_id_from_save_path,
    capture_quiescent_save_fingerprints,
    create_backup,
    decrypt_save_to_bytes,
    discover_save_paths,
    list_backups,
    read_backup_plaintext,
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
    "list_backups",
    "open_save",
    "plan_edits",
    "restore_backup",
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
    """User-facing view of one accessory record.

    ``catalog_hits`` is how many of the record's occupied effect slots name a
    饰品词条 in the shipped catalog, or ``None`` when the caller supplied no
    catalog; it is the evidence behind :attr:`kind_name`.
    """

    slot_index: int
    offset: int
    record_type: int
    level: int
    rarity: int
    rarity_name: str
    account_id: int
    effects: tuple[records.EffectSlot, ...]
    kind_name: str = "装备/饰品"
    catalog_hits: int | None = None

    @property
    def is_accessory(self) -> bool | None:
        """``True``/``False`` when a catalog was supplied, else ``None``."""
        if self.catalog_hits is None:
            return None
        return self.catalog_hits > 0

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
    offset: int
    edits: tuple[dict[str, int], ...]
    before: tuple[records.EffectSlot, ...]
    after: tuple[records.EffectSlot, ...]


# --------------------------------------------------------------------------
# Discovery / reading
# --------------------------------------------------------------------------

def discover_saves(root: Path | None = None) -> tuple[SaveDescriptor, ...]:
    """Return every discoverable USR save, newest account/slot first."""
    descriptors: list[SaveDescriptor] = []
    for path in discover_save_paths(root):
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


def inspect_layout(
    decrypted: bytes,
    *,
    known_ids: frozenset[int] | None = None,
) -> records.InventoryLayout:
    """Locate the save's item-record array (raises when there is none)."""
    return records.locate_layout(decrypted, known_ids=known_ids)


def list_accessories(
    decrypted: bytes,
    *,
    layout: records.InventoryLayout | None = None,
    known_ids: frozenset[int] | None = None,
) -> tuple[AccessoryView, ...]:
    """Parse item records from the save's located record array.

    ``known_ids`` (the shipped 饰品词条 catalog) makes each view's
    :attr:`AccessoryView.kind_name` evidence-based, so callers can tell real
    accessories from the weapons/armour/绘卷 sharing the same array.
    """
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
            kind_name=record.kind_name,
            catalog_hits=record.catalog_hits,
        )
        for record in records.iter_item_records(decrypted, layout=layout,
                                               known_ids=known_ids)
    )


def accessory_catalog_ids(affix_db: AffixDb) -> frozenset[int]:
    """The effect ids of every 饰品词条 in the shipped catalog."""
    return frozenset(entry.effect_id for entry in affix_db.all())


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
    known_ids: frozenset[int] | None = None,
    layout: records.InventoryLayout | None = None,
) -> tuple[EditPlan, ...]:
    """Validate edits against the live save and return per-record plans.

    Raises if a referenced record does not exist, if a slot index is invalid,
    or if the affix is outside the legal accessory table.

    ``known_ids``/``layout`` must be the ones the caller *listed* the records
    with: a record index only means something relative to one located array, so
    re-locating with different evidence could silently edit another item.
    """
    if not edits:
        raise EditorError("至少需要一个编辑项")
    normalized = tuple(_validate_edit(dict(edit), affix_db) for edit in edits)

    if known_ids is None and layout is None:
        known_ids = accessory_catalog_ids(affix_db)
    if layout is None:
        layout = records.locate_layout(decrypted, known_ids=known_ids)
    known = {view.slot_index: view
             for view in list_accessories(decrypted, layout=layout,
                                          known_ids=known_ids)}
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
        record = records.read_item_record(decrypted, record_index, layout=layout)
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
                offset=record.offset,
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
    known_ids: frozenset[int] | None = None,
    layout: records.InventoryLayout | None = None,
) -> bytes:
    """Return new save bytes with validated effect edits applied.

    ``known_ids``/``layout`` must be the ones the records were listed with, so a
    record index cannot resolve to a different item here than it did in the UI.
    """
    plans = plan_edits(decrypted, edits, affix_db=affix_db, known_ids=known_ids,
                       layout=layout)
    output = bytearray(decrypted)
    for plan in plans:
        offset = plan.offset
        record = bytes(output[offset:offset + records.SCROLL_RECORD_SIZE])
        patched = records.patch_effect_slots(
            record,
            [dict(edit, record_index=plan.record_index) for edit in plan.edits],
            allow_record_index=True,
        )
        output[offset:offset + records.SCROLL_RECORD_SIZE] = patched

    # Verify the write landed exactly where the plan said it would.
    for plan in plans:
        offset = plan.offset
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


def restore_backup(
    save: SaveDescriptor,
    entry: BackupEntry,
    *,
    crypto: SaveCrypto,
    state_root: Path,
    dry_run: bool = False,
    verify: bool = True,
    allow_game_running: bool = False,
) -> dict[str, object]:
    """Put a plaintext backup back into the save file (re-encrypted).

    Backups hold *decrypted* bytes, so restoring is not a file copy: the bytes
    are re-encrypted with the same routine an edit uses, which also means the
    write inherits every safety property (quiescence re-check, staged
    verification, atomic durable replace, rollback on failure) and the same
    "close the game first" gate.

    The current save is backed up **before** it is overwritten, so a restore is
    itself undoable.  ``dry_run`` performs every check and writes nothing.

    Trailing bytes (:data:`savefile.UNCOVERED_TAIL_BYTES`): the reference crypto
    tool zeroes them in *both* directions, so a backup made with it cannot carry
    a real tail.  When the backup does hold a non-zero tail (pure-Python backend,
    or a hand-made backup) that value is written back; otherwise the on-disk tail
    is kept, which never discards data we actually have.  ``tail_source`` in the
    report states which happened, and ``matches_original_save`` remains the exact
    verdict on the resulting file.

    ``source_mismatch`` lists any disagreement between the backup's recorded
    account/slot and the save being written (possible only when the caller names a
    backup file explicitly).  The restore still proceeds -- the user asked for that
    file and the safety backup keeps it reversible -- but the caller is expected to
    surface the warning.
    """
    plain = read_backup_plaintext(entry)
    running = require_game_not_running(allow_running=allow_game_running)
    expected = capture_quiescent_save_fingerprints(save.path)

    backed_up_tail = plain[-UNCOVERED_TAIL_BYTES:]
    tail = backed_up_tail if any(backed_up_tail) else None

    # A backup from another account/slot (possible only via an explicit path:
    # the listing is per save) is very likely a mix-up, so say so loudly.
    mismatch: list[str] = []
    if entry.account_id is not None:
        try:
            current_account = account_id_from_save_path(save.path)
        except ValueError:  # pragma: no cover - path shape already validated
            current_account = None
        if current_account is not None and entry.account_id != current_account:
            mismatch.append(f"备份属于账号 {entry.account_id}，当前存档是账号 "
                            f"{current_account}")
    if entry.slot_index is not None and entry.slot_index != save.slot_index:
        mismatch.append(f"备份来自栏位 {entry.slot_index:02d}，当前存档是栏位 "
                        f"{save.slot_index:02d}")

    report: dict[str, object] = {
        "dry_run": bool(dry_run),
        "path": str(save.path),
        "restored_from": str(entry.plain_path),
        "backup_created_at": entry.created_at,
        "plain_sha256": entry.plain_sha256,
        "game_processes_running": list(running),
        "verified": False,
        "safety_backup_dir": None,
        "new_sha256": None,
        "matches_original_save": None,
        "tail_source": "backup" if tail is not None else "on-disk",
        "source_mismatch": mismatch,
    }
    if dry_run:
        return report

    # Make the restore reversible: keep what is on disk right now.
    safety = create_backup(
        save.path,
        state_root=state_root,
        crypto=crypto,
        expected_fingerprints=expected,
    )
    new_sha256 = write_encrypted_save(
        save.path,
        plain,
        crypto=crypto,
        expected_fingerprints=expected,
        verify=verify,
        # ``None`` keeps the current on-disk tail; the backup's tail is written
        # explicitly when it carries one, because encryption zeroes it.
        tail=tail,
    )
    report.update(
        safety_backup_dir=str(safety),
        new_sha256=new_sha256,
        verified=bool(verify),
    )
    if entry.original_save_sha256:
        report["matches_original_save"] = new_sha256 == entry.original_save_sha256
    return report
