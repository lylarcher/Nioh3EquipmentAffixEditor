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
import struct

from . import records
from .affixdb import GRACE_KINDS, AffixDb, GraceDb, ItemDb
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
    "CreationError",
    "CreationPlan",
    "EditPlan",
    "EditorError",
    "FreeSlotReport",
    "KindSample",
    "KindSwapError",
    "KindSwapPlan",
    "LevelEditError",
    "LevelPlan",
    "PlusPlan",
    "SaveDescriptor",
    "SoulCoreView",
    "apply_creations",
    "apply_edits",
    "apply_kind_swaps",
    "apply_level_edits",
    "apply_plus_edits",
    "apply_soul_edits",
    "collect_kind_samples",
    "commit_save",
    "discover_saves",
    "find_free_slots",
    "identify_soul_cores",
    "list_accessories",
    "list_backups",
    "list_soul_cores",
    "open_save",
    "plan_creation",
    "plan_edits",
    "plan_kind_swap",
    "plan_level_edit",
    "plan_plus_edit",
    "plan_soul_edits",
    "resolve_item_id",
    "restore_backup",
    "soul_catalog_ids",
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
    level_mirror: int = 0
    plus_candidate: int = 0

    @property
    def is_accessory(self) -> bool | None:
        """``True``/``False`` when a catalog was supplied, else ``None``."""
        if self.catalog_hits is None:
            return None
        return self.catalog_hits > 0

    @property
    def item_kind(self) -> str:
        """大类 of the record: ``饰品``/``魂核``/其它 (evidence-based)."""
        return self.kind_name

    @property
    def occupied_effects(self) -> tuple[records.EffectSlot, ...]:
        return tuple(effect for effect in self.effects if not effect.is_empty)

    def slot_role(self, slot_index: int, affix_db: AffixDb) -> str:
        """What an occupied slot holds: 固定词条 / 饰品词条 / 恩宠/套装词条."""
        if self.slot_is_fixed(slot_index, affix_db):
            return "固定词条"
        if slot_index not in self.grace_slots(affix_db):
            return "饰品词条"
        return "恩宠/套装词条"

    def slot_is_fixed(self, slot_index: int, affix_db: AffixDb) -> bool:
        """Whether this slot holds the item's 同名固定 affix (never editable).

        Evidence, and why the metadata bit alone is *not* enough:

        * the catalog marks the id ``(同名固定)`` (byte 9 bit 6 of the affix code
          template, measured against the workbook's own 同名固定 rows), and
        * the slot's metadata byte 9 has bit ``0x40`` set.

        On the 795 catalogued slots of the reporting user's save the two agreed
        with zero exceptions.  The bit is *not* consulted for ids outside the
        catalog, because there it is set for other reasons too — 恩宠 slots
        (byte 9 = 0x4C = 0x40|0x0C) and 套装 slots all carry it, and 恩宠 must stay
        editable through the 恩宠 path.  So: catalog-fixed, or (in-catalog and bit
        set), and nothing else.

        An item's fixed affix is part of *what the item is*: changing it (or
        changing a normal slot into one) would describe an accessory that cannot
        drop.  The only code allowed to rewrite it is the 种类 swap, which copies
        the target kind's own fixed affix from a real sample.
        """
        if not 0 <= slot_index < len(self.effects):
            return False
        effect = self.effects[slot_index]
        if effect.is_empty:
            return False
        entry = affix_db.lookup(effect.effect_id)
        if entry is None:
            return False
        if entry.is_fixed:
            return True
        return records.effect_metadata_is_fixed(effect.metadata)

    def fixed_slots(self, affix_db: AffixDb) -> frozenset[int]:
        """Indices of the record's occupied 同名固定 slots (usually just one)."""
        return frozenset(
            effect.slot_index for effect in self.occupied_effects
            if self.slot_is_fixed(effect.slot_index, affix_db)
        )

    def grace_slots(self, affix_db: AffixDb) -> frozenset[int]:
        """Indices of trailing occupied slots whose id is outside the catalog.

        In a real v2.21 save every accessory ends with a 恩宠 or 套装组合 affix
        (confirmed in game: e.g. 稻荷神的恩宠 on a 龙笛).  Those ids are not in the
        shipped 饰品词条 table, so a trailing slot outside the table is reported as
        恩宠/套装 rather than as an unexplained "未知词条".

        The record must hit the table at least once: a record whose affixes are all
        outside it is not an accessory at all, and calling its affixes 恩宠/套装
        would be a guess.
        """
        known = {entry.effect_id for entry in affix_db.all()}
        if not any(effect.effect_id in known for effect in self.occupied_effects):
            return frozenset()
        trailing: list[int] = []
        for effect in reversed(self.effects):
            if effect.is_empty:
                continue
            if effect.effect_id in known:
                break
            trailing.append(effect.slot_index)
        return frozenset(trailing)

    def describe_item(self, item_db: ItemDb | None = None) -> str:
        """``种类 0x4987 八尺琼勾玉[武士]`` — the *item* this record is.

        In v2.21 the header field at ``+0x00`` (mirrored at ``+0x02``) is the
        per-item id, not the captured category type.  Naming it is display only:
        nothing here may authorise a write.
        """
        name = item_db.describe(self.record_type) if item_db else None
        if name:
            return f"种类 {self.record_type:#06x} {name}"
        if item_db is not None and item_db.is_loaded:
            return f"种类 {self.record_type:#06x}（不在物品种类表内）"
        return f"种类 {self.record_type:#06x}（未加载物品种类表）"

    def describe_effects(self, affix_db: AffixDb,
                         grace_db: GraceDb | None = None,
                         item_db: ItemDb | None = None) -> tuple[str, ...]:
        lines: list[str] = [f"  {self.describe_item(item_db)}"]
        grace = self.grace_slots(affix_db)
        for effect in self.effects:
            if effect.is_empty:
                lines.append(f"  [{effect.slot_index}] (空)")
                continue
            if self.slot_is_fixed(effect.slot_index, affix_db):
                lines.append(
                    f"  [{effect.slot_index}] {affix_db.describe(effect.effect_id)}"
                    f"（固定词条，不可修改，id={effect.effect_id:#06x}） "
                    f"(数值={effect.value} 标识={effect.metadata:#010x})"
                )
                continue
            if effect.slot_index in grace:
                named = grace_db.describe(effect.effect_id) if grace_db else None
                if named:
                    name = f"{named}（末位槽，id={effect.effect_id:#06x}）"
                else:
                    name = (f"恩宠/套装词条（不在饰品词条库，"
                            f"id={effect.effect_id:#06x}）")
                lines.append(
                    f"  [{effect.slot_index}] {name} (数值={effect.value} "
                    f"标识={effect.metadata:#010x})"
                )
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


@dataclass(frozen=True, slots=True)
class KindSample:
    """A verified sample of one 饰品 kind taken from the user's own save.

    The fixed affix is part of what an item kind *is*, so a 种类 swap may not
    invent one: it copies the fixed affix of the target kind from a real record.

    Only the kind-deterministic fields are copied — measured over the 60 kinds
    with a fixed affix in the reporting user's save, ``id``, ``value`` and
    metadata **byte 9** are identical on every copy of a kind (58/60; the two
    exceptions are the 八咫镜 variants, which are refused as ambiguous), while
    metadata bytes 10/11 vary per copy (only 25/60 kinds agree) and the slot
    ``prefix`` varies for 4 kinds.  Bytes 10/11 and the prefix are therefore left
    untouched: their meaning is unverified and they are not kind properties.
    """

    item_id: int
    copies: int
    fixed_slots: tuple[tuple[int, int, int, int], ...]
    ambiguous: bool


@dataclass(frozen=True, slots=True)
class KindSwapPlan:
    """A validated 种类 swap: the new item id plus the fixed slots it rewrites."""

    record_index: int
    offset: int
    old_item_id: int
    new_item_id: int
    category: str
    fixed_before: tuple[tuple[int, int, int, int], ...]
    fixed_after: tuple[tuple[int, int, int, int], ...]

    def describe(self) -> str:
        return (f"记录 #{self.record_index}: 种类 {self.old_item_id:#06x} → "
                f"{self.new_item_id:#06x}（{self.category}），"
                f"固定词条随种类同步 {len(self.fixed_after)} 条")


class KindSwapError(EditorError):
    """Raised when a 种类 swap would be illegal or unverifiable."""


def _fixed_slot_tuple(effect: records.EffectSlot) -> tuple[int, int, int, int]:
    """(slot, id, value, metadata 第 9 字节) — the kind-deterministic fixed fields."""
    return (effect.slot_index, effect.effect_id, effect.value,
            (effect.metadata >> records.EFFECT_FIXED_MARKER_SHIFT) & 0xFF)


def collect_kind_samples(
    decrypted: bytes,
    *,
    affix_db: AffixDb,
    known_ids: frozenset[int] | None = None,
    layout: records.InventoryLayout | None = None,
) -> dict[int, KindSample]:
    """Index the accessory kinds present in the save, with their fixed slots.

    Read-only.  A kind is ``ambiguous`` when its copies disagree about *which*
    slot holds the fixed affix, or about that slot's id/value/metadata — the
    editor then refuses to swap *to* it rather than guess.
    """
    if known_ids is None and layout is None:
        known_ids = accessory_catalog_ids(affix_db)
    if layout is None:
        layout = records.locate_layout(decrypted, known_ids=known_ids)
    patterns: dict[int, dict[tuple[tuple[int, int, int, int, int], ...], int]] = {}
    for view in list_accessories(decrypted, layout=layout, known_ids=known_ids):
        pattern = tuple(
            _fixed_slot_tuple(view.effects[index])
            for index in sorted(view.fixed_slots(affix_db))
        )
        seen = patterns.setdefault(view.record_type, {})
        seen[pattern] = seen.get(pattern, 0) + 1
    samples: dict[int, KindSample] = {}
    for item_id, seen in patterns.items():
        pattern, copies = max(seen.items(), key=lambda item: item[1])
        samples[item_id] = KindSample(
            item_id=item_id,
            copies=sum(seen.values()),
            fixed_slots=pattern,
            ambiguous=len(seen) > 1,
        )
    return samples


def plan_kind_swap(
    decrypted: bytes,
    record_index: int,
    target_item_id: int,
    *,
    affix_db: AffixDb,
    item_db: ItemDb,
    known_ids: frozenset[int] | None = None,
    layout: records.InventoryLayout | None = None,
) -> KindSwapPlan:
    """Validate one 种类 swap inside the same 中类 (武士饰品/忍者饰品).

    Legality, all fail-closed:

    * the target must be a 饰品 row of 物品总目录 with a 中类, and the record's own
      kind must resolve to the *same* 中类 — cross-category swaps (e.g.
      武士饰品 → 忍者饰品) are refused;
    * the target kind must already exist in this save with an unambiguous fixed
      affix, because that fixed affix is copied from it — never invented;
    * the record's fixed-slot *positions* must match the sample's, otherwise
      slots would have to be converted (refused, with the reason reported);
    * normal slots and the 恩宠/套装 slot are left exactly as they are.

    Only ``+0x00``/``+0x02`` (种类) and the fixed slots change.
    """
    if not isinstance(target_item_id, int) or isinstance(target_item_id, bool):
        raise KindSwapError("目标种类 id 必须是整数")
    if known_ids is None and layout is None:
        known_ids = accessory_catalog_ids(affix_db)
    if layout is None:
        layout = records.locate_layout(decrypted, known_ids=known_ids)
    if not 0 <= record_index < layout.slot_count:
        raise KindSwapError(
            f"记录索引必须位于 0..{layout.slot_count - 1}，实际 {record_index}"
        )
    record = records.read_item_record(decrypted, record_index, layout=layout)
    if record is None:
        raise KindSwapError(f"记录 #{record_index} 不存在或不是物品记录")
    raw = record.record
    old_id = records.read_record_item_id(raw)
    mirror_ok = (old_id == struct.unpack_from("<H", raw,
                                              records.RECORD_MIRROR_TYPE_OFFSET)[0])
    target_category = item_db.category_of(target_item_id)
    if target_category is None:
        raise KindSwapError(
            f"目标种类 {target_item_id:#06x} 不在物品种类表内（或没有中类），"
            "无法证明与当前饰品同分类，已拒绝"
        )
    current_category = item_db.category_of(old_id)
    if current_category is None:
        raise KindSwapError(
            f"当前记录的种类 {old_id:#06x} 不在物品种类表内（或没有中类），"
            "无法证明同分类，已拒绝改种类"
        )
    if current_category != target_category:
        raise KindSwapError(
            f"只能同分类互换：当前 {old_id:#06x} 属于「{current_category}」，"
            f"目标 {target_item_id:#06x} 属于「{target_category}」"
        )
    if target_item_id == old_id:
        raise KindSwapError(f"记录 #{record_index} 的种类已经是 {old_id:#06x}")

    views = {view.slot_index: view
             for view in list_accessories(decrypted, layout=layout,
                                          known_ids=known_ids)}
    view = views.get(record_index)
    if view is None:
        raise KindSwapError(f"记录 #{record_index} 不在当前存档的饰品记录中")
    samples = collect_kind_samples(decrypted, affix_db=affix_db,
                                   known_ids=known_ids, layout=layout)
    sample = samples.get(target_item_id)
    if sample is None:
        raise KindSwapError(
            f"存档里没有种类 {target_item_id:#06x}"
            f"（{item_db.describe(target_item_id) or '表内条目'}）的实例，"
            "无法从真实样本同步该种类的固定词条，已拒绝"
        )
    if sample.ambiguous:
        raise KindSwapError(
            f"种类 {target_item_id:#06x} 在存档内的固定词条不一致"
            "（不同副本的固定槽/数值不同），无法确定该复制哪一个，已拒绝"
        )
    before = tuple(_fixed_slot_tuple(view.effects[index])
                   for index in sorted(view.fixed_slots(affix_db)))
    if tuple(item[0] for item in before) != tuple(item[0]
                                                  for item in sample.fixed_slots):
        raise KindSwapError(
            f"记录 #{record_index} 的固定槽位置 "
            f"{tuple(item[0] for item in before)} 与种类 "
            f"{target_item_id:#06x} 的样本 {tuple(item[0] for item in sample.fixed_slots)} "
            "不一致：互换需要把普通槽变成固定槽（或反之），已拒绝"
        )
    records.patch_record_item_id(raw, target_item_id, mirror_ok=mirror_ok)
    return KindSwapPlan(record_index=record_index, offset=record.offset,
                        old_item_id=old_id, new_item_id=target_item_id,
                        category=target_category, fixed_before=before,
                        fixed_after=sample.fixed_slots)


def apply_kind_swaps(
    decrypted: bytes,
    plans: tuple[KindSwapPlan, ...] | list[KindSwapPlan],
) -> bytes:
    """Return new save bytes with the planned 种类 swaps applied.

    Each fixed slot gets the target kind's id, value and metadata byte 9; the
    slot's own prefix and metadata bytes 10/11 are preserved (per-instance fields
    — see :class:`KindSample`).
    """
    if not plans:
        raise KindSwapError("没有种类互换计划")
    output = bytearray(decrypted)
    for plan in plans:
        offset = plan.offset
        record = bytes(output[offset:offset + records.SCROLL_RECORD_SIZE])
        patched = records.patch_record_item_id(record, plan.new_item_id)
        slots = {effect.slot_index: effect
                 for effect in records.read_effect_slots(patched)}
        edits = []
        for slot_index, effect_id, value, byte9 in plan.fixed_after:
            effect = slots[slot_index]
            metadata = (effect.metadata
                        & ~(0xFF << records.EFFECT_FIXED_MARKER_SHIFT))
            metadata |= byte9 << records.EFFECT_FIXED_MARKER_SHIFT
            edits.append({"slot_index": slot_index, "effect_id": effect_id,
                          "value": value, "metadata": metadata})
        if edits:
            patched = records.patch_effect_slots(patched, edits)
        output[offset:offset + records.SCROLL_RECORD_SIZE] = patched
    return bytes(output)



@dataclass(frozen=True, slots=True)
class LevelPlan:
    """A validated 等级 change for one record (``+0x06`` and its mirror ``+0x08``)."""

    record_index: int
    offset: int
    old_level: int
    new_level: int

    def describe(self) -> str:
        return (f"记录 #{self.record_index}: 等级 {self.old_level} → "
                f"{self.new_level}")


class LevelEditError(EditorError):
    """Raised when a 等级 change would be illegal or unverifiable."""


def plan_level_edit(
    decrypted: bytes,
    record_index: int,
    level: int,
    *,
    affix_db: AffixDb,
    known_ids: frozenset[int] | None = None,
    layout: records.InventoryLayout | None = None,
) -> LevelPlan:
    """Validate one 等级 change and return its plan (nothing is written here).

    Legal range is ``1..180``: the reference project reads the effective level as
    ``min(record +0x06, 180)`` and the reporting user's save tops out at exactly
    180, so anything above it is not something the game would ever serialise.

    Only the level fields change.  Measured on the reporting user's save: the
    *stored* affix values of a kind are identical at every level (e.g. every
    copy of 除雷护身符[武士] holds 雷属性伤害降低 +15 at levels 156..170), so this
    edit does not - and must not - rewrite affix values to match the new level.
    Whether the game recomputes displayed stats from the level on load is not
    something a save file can prove, which is why the UI warns before writing.
    """
    if not isinstance(level, int) or isinstance(level, bool):
        raise LevelEditError("等级必须是整数")
    if not records.MIN_ITEM_LEVEL <= level <= records.MAX_ITEM_LEVEL:
        raise LevelEditError(
            f"等级必须在 {records.MIN_ITEM_LEVEL}..{records.MAX_ITEM_LEVEL} 之间"
            f"（{records.MAX_ITEM_LEVEL} 是游戏可序列化的上限）"
        )
    if not isinstance(record_index, int) or isinstance(record_index, bool):
        raise LevelEditError("记录索引必须是整数")
    if known_ids is None and layout is None:
        known_ids = accessory_catalog_ids(affix_db)
    if layout is None:
        layout = records.locate_layout(decrypted, known_ids=known_ids)
    if not 0 <= record_index < layout.slot_count:
        raise LevelEditError(
            f"记录索引必须位于 0..{layout.slot_count - 1}，实际 {record_index}"
        )
    record = records.read_item_record(decrypted, record_index, layout=layout)
    if record is None:
        raise LevelEditError(f"记录 #{record_index} 不存在或不是物品记录")
    current = records.read_record_level(record.record)
    mirror = records.read_record_level_mirror(record.record)
    if current != mirror:
        raise LevelEditError(
            f"记录 #{record_index} 的等级字段不一致"
            f"（+0x06={current}，+0x08={mirror}），已拒绝改写"
        )
    if current == level:
        raise LevelEditError(f"记录 #{record_index} 的等级已经是 {level}")
    # The patch function owns the range/mirror checks as well; call it so a
    # changed rule cannot be bypassed here.
    records.patch_record_level(record.record, level)
    return LevelPlan(record_index=record_index, offset=record.offset,
                     old_level=current, new_level=level)


def apply_level_edits(
    decrypted: bytes,
    plans: tuple[LevelPlan, ...] | list[LevelPlan],
) -> bytes:
    """Return new save bytes with the planned 等级 changes applied."""
    if not plans:
        raise LevelEditError("没有等级修改计划")
    output = bytearray(decrypted)
    for plan in plans:
        offset = plan.offset
        record = bytes(output[offset:offset + records.SCROLL_RECORD_SIZE])
        output[offset:offset + records.SCROLL_RECORD_SIZE] = (
            records.patch_record_level(record, plan.new_level)
        )
    return bytes(output)


@dataclass(frozen=True, slots=True)
class PlusPlan:
    """A ``+0x0A`` change for one record — the in-game A/B test writer.

    The field's meaning is unverified.  This plan exists so the *user* can change
    exactly one word, load the game, and see what the item card does; nothing else
    in the record is touched.
    """

    record_index: int
    offset: int
    old_value: int
    new_value: int

    def describe(self) -> str:
        return (f"记录 #{self.record_index}: +0x0A {self.old_value} → "
                f"{self.new_value}（该字段含义未核实，仅供进游戏对照试验）")


def plan_plus_edit(
    decrypted: bytes,
    record_index: int,
    value: int,
    *,
    affix_db: AffixDb,
    known_ids: frozenset[int] | None = None,
    layout: records.InventoryLayout | None = None,
) -> PlusPlan:
    """Validate one ``+0x0A`` change (0..30, the span the save actually uses)."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise EditorError("+0x0A 的值必须是整数")
    if not 0 <= value <= records.MAX_RECORD_PLUS:
        raise EditorError(
            f"+0x0A 必须在 0..{records.MAX_RECORD_PLUS} 之间"
            "（这是你存档里实测的取值范围；含义未核实，不接受范围外的值）"
        )
    if not isinstance(record_index, int) or isinstance(record_index, bool):
        raise EditorError("记录索引必须是整数")
    if known_ids is None and layout is None:
        known_ids = accessory_catalog_ids(affix_db)
    if layout is None:
        layout = records.locate_layout(decrypted, known_ids=known_ids)
    if not 0 <= record_index < layout.slot_count:
        raise EditorError(
            f"记录索引必须位于 0..{layout.slot_count - 1}，实际 {record_index}"
        )
    record = records.read_item_record(decrypted, record_index, layout=layout)
    if record is None:
        raise EditorError(f"记录 #{record_index} 不存在或不是物品记录")
    current = records.read_record_plus_candidate(record.record)
    if current == value:
        raise EditorError(f"记录 #{record_index} 的 +0x0A 已经是 {value}")
    records.patch_record_plus(record.record, value)
    return PlusPlan(record_index=record_index, offset=record.offset,
                    old_value=current, new_value=value)


def apply_plus_edits(
    decrypted: bytes,
    plans: tuple[PlusPlan, ...] | list[PlusPlan],
) -> bytes:
    """Return new save bytes with the planned ``+0x0A`` changes applied."""
    if not plans:
        raise EditorError("没有 +0x0A 修改计划")
    output = bytearray(decrypted)
    for plan in plans:
        offset = plan.offset
        record = bytes(output[offset:offset + records.SCROLL_RECORD_SIZE])
        output[offset:offset + records.SCROLL_RECORD_SIZE] = (
            records.patch_record_plus(record, plan.new_value)
        )
    return bytes(output)


@dataclass(frozen=True, slots=True)
class SoulCoreView:
    """User-facing view of one 魂核 (soul core) record.

    A 魂核 is identified by evidence, not by a type table: its header item id must
    be a 物品总目录 魂核 row, and its effect slots must name affixes from the
    绘卷-魂核词条 魂核 pool.  Measured on the reporting user's save: 132 of 134
    candidate records satisfy both, and the workbook's own 固定词条代码 matched
    those 132 with 0 mismatches (the other 2 are the two copies of `0xda62`, whose
    id is not in the 魂核 sheet — they are reported as unidentified, never guessed).

    A 魂核 has **no 恩宠/套装 affix**: its affix pool is separate from 饰品词条 and
    the 恩宠/套装 table is not consulted anywhere in this view.
    """

    slot_index: int
    offset: int
    record_type: int
    level: int
    rarity: int
    rarity_name: str
    account_id: int
    effects: tuple[records.EffectSlot, ...]
    kind_name: str = "魂核"
    catalog_hits: int | None = None
    level_mirror: int = 0
    plus_candidate: int = 0
    unidentified: str = ""

    def describe_item(self, item_db: ItemDb | None = None) -> str:
        """``魂核 0x1234 夜刀神`` when the item table has the id."""
        name = item_db.describe(self.record_type) if item_db else None
        if name:
            return f"魂核 {self.record_type:#06x} {name}"
        if item_db is not None and item_db.is_loaded:
            return f"魂核 {self.record_type:#06x}（不在魂核种类表内）"
        return f"魂核 {self.record_type:#06x}（未加载魂核种类表）"

    def slot_is_fixed(self, slot_index: int, soul_db: AffixDb) -> bool:
        """Whether this slot holds the 魂核's own 同名固定 affix (never editable).

        Same rule as accessories, and the same evidence: the 魂核 sheet lists the
        fixed 词条代码 of every core (its 词条代码 preimage carries the 0x40 bit, so
        the catalog flag applies), and the save marks the slot the same way.
        """
        if not 0 <= slot_index < len(self.effects):
            return False
        effect = self.effects[slot_index]
        if effect.is_empty:
            return False
        entry = soul_db.lookup(effect.effect_id)
        if entry is None:
            return False
        if entry.is_fixed:
            return True
        return records.effect_metadata_is_fixed(effect.metadata)

    def fixed_slots(self, soul_db: AffixDb) -> frozenset[int]:
        return frozenset(
            effect.slot_index for effect in self.occupied_effects
            if self.slot_is_fixed(effect.slot_index, soul_db)
        )

    def slot_role(self, slot_index: int, soul_db: AffixDb) -> str:
        effect = self.effects[slot_index]
        if effect.is_empty:
            return "空"
        if soul_db.lookup(effect.effect_id) is None:
            return "不在魂核词条库内"
        return "魂核固定词条" if self.slot_is_fixed(slot_index, soul_db) else "魂核词条"

    @property
    def occupied_effects(self) -> tuple[records.EffectSlot, ...]:
        return tuple(effect for effect in self.effects if not effect.is_empty)

    def describe_effects(self, soul_db: AffixDb,
                         item_db: ItemDb | None = None) -> tuple[str, ...]:
        lines: list[str] = [f"  {self.describe_item(item_db)}"]
        if self.unidentified:
            lines.append(f"  ⚠ {self.unidentified}")
        for effect in self.effects:
            if effect.is_empty:
                lines.append(f"  [{effect.slot_index}] (空)")
                continue
            if self.slot_is_fixed(effect.slot_index, soul_db):
                lines.append(
                    f"  [{effect.slot_index}] {soul_db.describe(effect.effect_id)}"
                    f"（固定词条，不可修改，id={effect.effect_id:#06x}） "
                    f"(数值={effect.value} 标识={effect.metadata:#010x})"
                )
                continue
            lines.append(
                f"  [{effect.slot_index}] {soul_db.describe(effect.effect_id)} "
                f"(id={effect.effect_id:#06x} 数值={effect.value} "
                f"标识={effect.metadata:#010x})"
            )
        return tuple(lines)


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
            level_mirror=records.read_record_level_mirror(record.record),
            plus_candidate=records.read_record_plus_candidate(record.record),
        )
        for record in records.iter_item_records(decrypted, layout=layout,
                                               known_ids=known_ids)
    )


def accessory_catalog_ids(affix_db: AffixDb) -> frozenset[int]:
    """The effect ids of every 饰品词条 in the shipped catalog."""
    return frozenset(entry.effect_id for entry in affix_db.all())


def soul_catalog_ids(soul_db: AffixDb) -> frozenset[int]:
    """The effect ids of every 魂核词条 in the shipped 魂核 catalog."""
    return frozenset(entry.effect_id for entry in soul_db.all())


def identify_soul_cores(
    views: tuple[AccessoryView, ...] | list[AccessoryView],
    *,
    soul_db: AffixDb,
    soul_item_db: ItemDb,
) -> tuple[SoulCoreView, ...]:
    """Keep the records that are provably 魂核, and say why the rest are not.

    Two independent facts are required, because either alone over-matches:

    * the header item id is a 物品总目录 魂核 row (an id outside the sheet — e.g.
      `0xda62`, both copies of which carry 9 heads' 恩宠 — is not a 魂核 we can
      identify, so it is reported instead of being edited), **and**
    * at least one effect slot names an affix from the 魂核 pool.

    A record that satisfies both but whose slots mix in 饰品 ids is still listed,
    with an explicit note, so nothing is silently reclassified.
    """
    cores: list[SoulCoreView] = []
    accessory_ids = None
    for view in views:
        kind = soul_item_db.describe(view.record_type)
        hits = view.catalog_hits or 0
        if kind is None:
            if hits:
                # Looks like a core, but the save's id is not in the 魂核 sheet.
                cores.append(SoulCoreView(
                    slot_index=view.slot_index, offset=view.offset,
                    record_type=view.record_type, level=view.level,
                    rarity=view.rarity, rarity_name=view.rarity_name,
                    account_id=view.account_id, effects=view.effects,
                    catalog_hits=hits, level_mirror=view.level_mirror,
                    plus_candidate=view.plus_candidate,
                    unidentified=(f"疑似魂核但种类 {view.record_type:#06x} "
                                  "不在魂核种类表内，未识别；本工具不会改它"),
                ))
            continue
        if not hits:
            continue
        if accessory_ids is None:
            accessory_ids = frozenset()
        cores.append(SoulCoreView(
            slot_index=view.slot_index, offset=view.offset,
            record_type=view.record_type, level=view.level,
            rarity=view.rarity, rarity_name=view.rarity_name,
            account_id=view.account_id, effects=view.effects,
            catalog_hits=hits, level_mirror=view.level_mirror,
            plus_candidate=view.plus_candidate,
        ))
    return tuple(cores)


def list_soul_cores(
    decrypted: bytes,
    *,
    soul_db: AffixDb,
    soul_item_db: ItemDb,
    layout: records.InventoryLayout | None = None,
    known_ids: frozenset[int] | None = None,
) -> tuple[SoulCoreView, ...]:
    """List the save's 魂核 records (identified by two independent facts)."""
    if known_ids is None:
        known_ids = soul_catalog_ids(soul_db)
    if layout is None:
        layout = records.locate_layout(decrypted, known_ids=known_ids)
    return identify_soul_cores(
        list_accessories(decrypted, layout=layout, known_ids=known_ids),
        soul_db=soul_db, soul_item_db=soul_item_db,
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
            entry = affix_db.require(effect_id)
            # A 同名固定 affix belongs to one item kind only: writing it into an
            # arbitrary slot would describe an accessory that cannot exist.  The
            # 种类 swap is the one code path allowed to place it, and it copies the
            # target kind's own entry instead of going through this function.
            if entry.is_fixed:
                raise EditorError(
                    f"词条 {effect_id:#010x}「{entry.name}」是饰品的同名固定词条，"
                    "只能由「改种类」自动带入，不能手动写入其它饰品"
                )
            # 数值: the workbook's own span is the gate.  None of the 807 catalogued
            # slots of the reporting user's save falls outside its affix's span, so
            # a value outside it describes an affix the game cannot roll.
            requested = edit.get("value")
            if requested is not None and requested != entry.value:
                if not entry.has_value_range:
                    raise EditorError(
                        f"词条「{entry.name}」在原始表里没有数值区间，"
                        f"因此只允许写目录值 {entry.value}，不能改成 {requested}"
                    )
                if not entry.allows_value(requested):
                    raise EditorError(
                        f"词条「{entry.name}」的数值必须在 "
                        f"{entry.describe_value_range()} 之内，{requested} 超出范围"
                    )
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
    if the affix is outside the legal accessory table, or if the slot being
    changed holds the item's 同名固定 affix (that affix is part of what the item
    is — see :meth:`AccessoryView.slot_is_fixed`).

    ``known_ids``/``layout`` must be the ones the caller *listed* the records
    with: a record index only means something relative to one located array, so
    re-locating with different evidence could silently edit another item.
    """
    if not edits:
        raise EditorError("至少需要一个编辑项")

    if known_ids is None and layout is None:
        known_ids = accessory_catalog_ids(affix_db)
    if layout is None:
        layout = records.locate_layout(decrypted, known_ids=known_ids)
    known = {view.slot_index: view
             for view in list_accessories(decrypted, layout=layout,
                                          known_ids=known_ids)}

    # 同名固定词条 is refused **first**: such a slot is not editable at all, so
    # reporting a 数值区间 error for it would be misleading (and the value the
    # caller happened to pass is irrelevant).  需求(1).
    fixed_refusals = []
    for edit in edits:
        record_index = edit.get("record_index")
        slot_index = edit.get("slot_index")
        if not isinstance(record_index, int) or not isinstance(slot_index, int):
            continue  # malformed edits are reported by _validate_edit below
        view = known.get(record_index)
        if view is None or not 0 <= slot_index < records.EFFECT_COUNT:
            continue
        if view.slot_is_fixed(slot_index, affix_db):
            entry = affix_db.lookup(view.effects[slot_index].effect_id)
            fixed_refusals.append(
                f"#{record_index} 槽{slot_index}"
                f"（{entry.name if entry else '未收录'}）"
            )
    if fixed_refusals:
        raise EditorError(
            "同名固定词条不能修改：" + "、".join(fixed_refusals)
        )

    normalized = tuple(_validate_edit(dict(edit), affix_db) for edit in edits)

    missing = sorted({edit["record_index"] for edit in normalized} - set(known))
    if missing:
        raise EditorError(
            "以下记录不在当前存档的饰品记录中："
            + "、".join(f"#{index}" for index in missing)
        )

    fixed_refusals = []
    for edit in normalized:
        view = known[edit["record_index"]]
        if view.slot_is_fixed(edit["slot_index"], affix_db):
            entry = affix_db.lookup(view.effects[edit["slot_index"]].effect_id)
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


def plan_soul_edits(
    decrypted: bytes,
    edits: tuple[dict[str, int], ...] | list[dict[str, int]],
    *,
    soul_db: AffixDb,
    soul_item_db: ItemDb,
    known_ids: frozenset[int] | None = None,
    layout: records.InventoryLayout | None = None,
) -> tuple[EditPlan, ...]:
    """Validate 魂核 affix edits (the 魂核 pool, not the 饰品 one).

    A 魂核 has no 恩宠/套装 affix, and that falls out of the gate rather than being
    special-cased: every written id must be a 绘卷-魂核词条 魂核 row, so an 恩宠 id
    (which is not in that table) is refused as "not a legal 魂核 affix".  Fixed slots
    are refused exactly as for accessories — the 魂核 sheet's own 固定词条代码 matched
    the save on 132/134 candidates with 0 mismatches, so the catalog flag is the
    right evidence.
    """
    if not edits:
        raise EditorError("至少需要一个编辑项")
    normalized = tuple(_validate_edit(dict(edit), soul_db) for edit in edits)
    if known_ids is None and layout is None:
        known_ids = soul_catalog_ids(soul_db)
    if layout is None:
        layout = records.locate_layout(decrypted, known_ids=known_ids)
    cores = {view.slot_index: view
             for view in list_soul_cores(decrypted, soul_db=soul_db,
                                          soul_item_db=soul_item_db,
                                          layout=layout, known_ids=known_ids)}
    missing = sorted({edit["record_index"] for edit in normalized} - set(cores))
    if missing:
        raise EditorError(
            "以下记录不在当前存档的魂核记录中："
            + "、".join(f"#{index}" for index in missing)
        )
    refusals = []
    for edit in normalized:
        view = cores[edit["record_index"]]
        if view.unidentified:
            refusals.append(f"#{edit['record_index']}（{view.unidentified}）")
            continue
        if view.slot_is_fixed(edit["slot_index"], soul_db):
            entry = soul_db.lookup(view.effects[edit["slot_index"]].effect_id)
            refusals.append(
                f"#{edit['record_index']} 槽{edit['slot_index']}"
                f"（{entry.name if entry else '未收录'}）"
            )
    if refusals:
        raise EditorError(
            "魂核固定词条不能修改：" + "、".join(refusals)
            + "。它是该魂核固有的一部分（随种类决定），"
            "只有「改种类」会按新种类的固定词条自动同步"
        )

    by_record: dict[int, list[dict[str, int]]] = {}
    for edit in normalized:
        by_record.setdefault(edit["record_index"], []).append(edit)
    plans: list[EditPlan] = []
    for record_index, record_edits in sorted(by_record.items()):
        view = cores[record_index]
        offset = view.offset
        record = decrypted[offset:offset + records.SCROLL_RECORD_SIZE]
        patched = records.patch_effect_slots(
            record, [dict(edit, record_index=record_index) for edit in record_edits],
            allow_record_index=True,
        )
        plans.append(
            EditPlan(
                record_index=record_index,
                offset=offset,
                edits=tuple(record_edits),
                before=view.effects,
                after=records.read_effect_slots(patched),
            )
        )
    return tuple(plans)


def apply_soul_edits(
    decrypted: bytes,
    edits: tuple[dict[str, int], ...] | list[dict[str, int]],
    *,
    soul_db: AffixDb,
    soul_item_db: ItemDb,
    known_ids: frozenset[int] | None = None,
    layout: records.InventoryLayout | None = None,
) -> bytes:
    """Return new save bytes with validated 魂核 effect edits applied."""
    plans = plan_soul_edits(decrypted, edits, soul_db=soul_db,
                            soul_item_db=soul_item_db, known_ids=known_ids,
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
    for plan in plans:
        offset = plan.offset
        applied = records.read_effect_slots(
            bytes(output[offset:offset + records.SCROLL_RECORD_SIZE])
        )
        if applied != plan.after:
            raise EditorError(f"记录 #{plan.record_index} 的修改未能正确写入，已中止")
    return bytes(output)


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
# 恩宠 (grace) editing
# --------------------------------------------------------------------------

#: Metadata byte 9 of an effect slot is the game's own family tag, measured on a
#: real v2.21 save: all 196 恩宠 slots hold 0x0C, all 16 套装 slots hold 0x4C
#: (and a normal 词条 holds something else entirely).  It is therefore the gate
#: for "may this slot be rewritten as another 恩宠".
GRACE_FAMILY_BYTE9 = 0x0C
#: Metadata byte 9 of a 套装 / 专属套装 slot, measured on the same save; kept here
#: so the refusal message can say *what* the slot is instead of "not a 恩宠".
SET_FAMILY_BYTE9 = 0x4C


class GraceEditError(EditorError):
    """Raised when a 恩宠 slot may not be rewritten (fail closed)."""


@dataclass(frozen=True, slots=True)
class GraceAvailability:
    """Whether the selected record's last slot can be changed to another 恩宠."""

    allowed: bool
    reason: str
    slot_index: int | None = None
    current_id: int | None = None
    current_name: str = ""
    kind: str = ""

    def describe_current(self) -> str:
        if self.current_id is None:
            return "（末位槽为空）"
        name = self.current_name or "未命名"
        return f"{name}（{self.current_id:#06x}）"


def grace_edit_availability(
    view: "AccessoryView",
    *,
    grace_db: GraceDb,
    affix_db: AffixDb,
) -> GraceAvailability:
    """Decide whether ``view``'s last slot may be rewritten as another 恩宠.

    Strict on purpose -- every condition below is evidence from a real save plus
    the shipped workbook:

    * the record must be an accessory (it hits the 饰品词条 catalog),
    * the last occupied slot must be a *trailing* slot outside that catalog,
    * its metadata byte 9 must be the 恩宠 family tag (0x0C); 套装 entries carry
      0x4C, so an item-specific 套装 effect such as 怨恨盖世 is refused,
    * and its current id must be a 恩宠/上位恩宠 in the workbook's 词条总目录.
    """
    occupied = view.occupied_effects
    if not occupied:
        return GraceAvailability(False, "该饰品没有任何占用中的词条槽")
    if view.is_accessory is False:
        return GraceAvailability(False, "该记录不是含饰品词条的饰品记录")

    last = occupied[-1]
    slot_index = last.slot_index
    family = (last.metadata >> 8) & 0xFF
    entry = grace_db.lookup(last.effect_id)
    kind = entry.category if entry else ""

    if slot_index not in view.grace_slots(affix_db):
        return GraceAvailability(
            False,
            f"末位槽 [{slot_index}] 是饰品词条（id={last.effect_id:#06x}），"
            "恩宠只能替换恩宠，不能把普通词条改成恩宠",
            slot_index=slot_index, current_id=last.effect_id,
            current_name=affix_db.describe(last.effect_id), kind="饰品词条",
        )
    if family == SET_FAMILY_BYTE9:
        return GraceAvailability(
            False,
            f"末位槽 [{slot_index}] 是套装/专属套装词条"
            f"（{entry.name if entry else '未命名'}，id={last.effect_id:#06x}），"
            "按规则不能改成恩宠",
            slot_index=slot_index, current_id=last.effect_id,
            current_name=entry.name if entry else "", kind=kind or "套装",
        )
    if family != GRACE_FAMILY_BYTE9:
        return GraceAvailability(
            False,
            f"末位槽 [{slot_index}] 的标识族字节是 {family:#04x}，"
            f"不是恩宠族 {GRACE_FAMILY_BYTE9:#04x}，无法确认可以安全替换",
            slot_index=slot_index, current_id=last.effect_id,
            current_name=entry.name if entry else "", kind=kind,
        )
    if entry is None or kind not in GRACE_KINDS:
        return GraceAvailability(
            False,
            f"末位槽 [{slot_index}] 的 id {last.effect_id:#06x} 不在恩宠名表里，"
            "无法确认它是可替换的恩宠",
            slot_index=slot_index, current_id=last.effect_id,
            current_name=entry.name if entry else "", kind=kind,
        )
    return GraceAvailability(
        True,
        f"末位槽 [{slot_index}] 当前是 {entry.name}，可以改成任意其他恩宠",
        slot_index=slot_index, current_id=last.effect_id,
        current_name=entry.name, kind=kind,
    )


def resolve_grace_id(grace_db: GraceDb, text: str) -> int:
    """Accept ``0x4fa3``, ``4fa3`` or a unique name such as ``稻荷神``."""
    raw = (text or "").strip()
    if not raw:
        raise GraceEditError("请指定要写入的恩宠")
    try:
        value = int(raw, 16) if raw.lower().startswith("0x") else int(raw, 16)
    except ValueError:
        value = None
    if value is not None and grace_db.lookup(value) is not None:
        return value
    if value is not None:
        raise GraceEditError(f"恩宠 id {value:#06x} 不在恩宠名表里")

    wanted = raw.replace("的恩宠", "").replace("恩宠", "").strip() or raw
    matches = [entry for entry in grace_db.all()
               if entry.category in GRACE_KINDS
               and (wanted in entry.name or raw == entry.name)]
    if not matches:
        raise GraceEditError(
            f"没有匹配「{raw}」的恩宠；可用 list 查看全部恩宠，"
            "或改用 id（如 --grace 0x4fa3）"
        )
    if len(matches) > 1:
        raise GraceEditError(
            f"「{raw}」匹配到多个恩宠："
            + "、".join(f"{entry.name}({entry.effect_id:#06x})" for entry in matches)
        )
    return matches[0].effect_id


def resolve_item_id(item_db: ItemDb, text: str) -> int:
    """Accept ``0x4987``, ``4987`` or a unique 饰品 name such as ``八尺琼勾玉``.

    Only 饰品 rows count: a 魂核/武器 name would resolve to an id that can never
    be swapped in, and resolving it here would only produce a confusing error.
    """
    raw = (text or "").strip()
    if not raw:
        raise KindSwapError("请指定目标种类（名称或 id）")
    try:
        value = int(raw, 16)
    except ValueError:
        value = None
    if value is not None:
        entry = item_db.lookup(value)
        if entry is None:
            raise KindSwapError(f"物品种类 id {value:#06x} 不在物品种类表内")
        return value

    matches = [entry for entry in item_db.all() if raw == entry.name]
    if not matches:
        matches = [entry for entry in item_db.all() if raw in entry.name]
    if not matches:
        raise KindSwapError(
            f"没有匹配「{raw}」的饰品种类；可用 list 查看存档里的种类，"
            "或改用 id（如 --kind 0x4987）"
        )
    if len(matches) > 1:
        raise KindSwapError(
            f"「{raw}」匹配到多个种类："
            + "、".join(f"{entry.name}({entry.item_id:#06x})" for entry in matches)
        )
    return matches[0].item_id


def plan_grace_edit(
    decrypted: bytes,
    record_index: int,
    grace_id: int,
    *,
    affix_db: AffixDb,
    grace_db: GraceDb,
    known_ids: frozenset[int] | None = None,
    layout: records.InventoryLayout | None = None,
) -> EditPlan:
    """Validate a 恩宠 -> 恩宠 change and return the plan for it.

    Only the slot's effect id and value are written; the family/sub-kind and the
    unexplained byte 11 are kept exactly as the game wrote them, because a real
    save shows byte 11 varying per item for one and the same 恩宠 id.
    """
    target = grace_db.lookup(grace_id)
    if target is None:
        raise GraceEditError(f"恩宠 id {grace_id:#06x} 不在恩宠名表里")
    if target.category not in GRACE_KINDS:
        raise GraceEditError(
            f"{target.name} 是「{target.category}」，不是恩宠；"
            "只有 xxx的恩宠 才能改成另外的 xxx的恩宠"
        )

    if known_ids is None and layout is None:
        known_ids = accessory_catalog_ids(affix_db)
    if layout is None:
        layout = records.locate_layout(decrypted, known_ids=known_ids)
    views = {view.slot_index: view
             for view in list_accessories(decrypted, layout=layout,
                                          known_ids=known_ids)}
    view = views.get(record_index)
    if view is None:
        raise GraceEditError(f"记录 #{record_index} 不在当前存档的饰品记录中")

    availability = grace_edit_availability(view, grace_db=grace_db,
                                           affix_db=affix_db)
    if not availability.allowed:
        raise GraceEditError(availability.reason)
    if availability.current_id == grace_id:
        raise GraceEditError(f"末位槽已经是 {target.name}，无需修改")

    record = records.read_item_record(decrypted, record_index, layout=layout)
    if record is None:
        raise GraceEditError(f"记录 #{record_index} 无法解析为饰品记录")
    edit = {
        "record_index": record_index,
        "slot_index": int(availability.slot_index),
        "effect_id": grace_id,
        "value": int(target.value),
        "metadata": int(view.effects[int(availability.slot_index)].metadata),
    }
    patched = records.patch_effect_slots(
        record.record, [edit], allow_record_index=True,
    )
    return EditPlan(
        record_index=record_index,
        offset=record.offset,
        edits=(edit,),
        before=view.effects,
        after=records.read_effect_slots(patched),
    )


def apply_grace_edit(
    decrypted: bytes,
    record_index: int,
    grace_id: int,
    *,
    affix_db: AffixDb,
    grace_db: GraceDb,
    known_ids: frozenset[int] | None = None,
    layout: records.InventoryLayout | None = None,
) -> bytes:
    """Return new save bytes with one accessory's 恩宠 replaced."""
    plan = plan_grace_edit(
        decrypted, record_index, grace_id, affix_db=affix_db, grace_db=grace_db,
        known_ids=known_ids, layout=layout,
    )
    output = bytearray(decrypted)
    offset = plan.offset
    record = bytes(output[offset:offset + records.SCROLL_RECORD_SIZE])
    patched = records.patch_effect_slots(record, [dict(plan.edits[0])],
                                         allow_record_index=True)
    output[offset:offset + records.SCROLL_RECORD_SIZE] = patched
    applied = records.read_effect_slots(
        bytes(output[offset:offset + records.SCROLL_RECORD_SIZE])
    )
    if applied != plan.after:
        raise GraceEditError(f"记录 #{record_index} 的恩宠修改未能正确写入，已中止")
    return bytes(output)


# --------------------------------------------------------------------------
# 无中生有: create a new item in a free slot
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FreeSlotReport:
    """How many slots of the located array are free (``type == 0``)."""

    slot_count: int
    free_slots: tuple[int, ...]

    @property
    def free_count(self) -> int:
        return len(self.free_slots)

    @property
    def is_full(self) -> bool:
        return not self.free_slots

    def describe(self) -> str:
        if self.is_full:
            return (f"背包已满：记录表 {self.slot_count} 个槽位全部被占用，"
                    "请先在游戏里清理背包后再添加")
        return (f"记录表 {self.slot_count} 槽，空位 {self.free_count} 个"
                f"（首个空位 #{self.free_slots[0]}）")


@dataclass(frozen=True, slots=True)
class CreationPlan:
    """A brand-new item record to be written into a free slot."""

    slot_index: int
    offset: int
    record_type: int
    level: int
    donor_slot: int
    donor_offset: int
    record: bytes
    effects: tuple[records.EffectSlot, ...]
    free_before: int

    def describe(self, item_db: ItemDb | None = None) -> str:
        name = item_db.describe(self.record_type) if item_db else None
        text = (f"新建 #{self.slot_index} {name or f'{self.record_type:#06x}'} "
                f"Lv{self.level}")
        if self.donor_slot == self.slot_index:
            return text
        return f"{text}（模板 #{self.donor_slot}）"


class CreationError(EditorError):
    """Raised when a new item cannot be created legally."""


def find_free_slots(
    decrypted: bytes,
    *,
    layout: records.InventoryLayout | None = None,
    known_ids: frozenset[int] | None = None,
) -> FreeSlotReport:
    """Every slot whose type word is 0 -- the game's own "empty" marker.

    Measured on the reporting user's save: 465 of the 2000 slots carry type 0,
    they are scattered (209 have occupied neighbours on both sides), and their
    effect slots hold no residual affix, so the game marks emptiness with the
    type word alone -- which is exactly what a deleted item leaves behind.
    """
    if layout is None:
        layout = records.locate_layout(decrypted, known_ids=known_ids)
    free: list[int] = []
    for index in range(layout.slot_count):
        offset = layout.offset(index)
        record = bytes(decrypted[offset:offset + records.SCROLL_RECORD_SIZE])
        if len(record) < records.SCROLL_RECORD_SIZE:
            break
        if records.record_is_empty(record):
            free.append(index)
    return FreeSlotReport(slot_count=layout.slot_count, free_slots=tuple(free))


def _donor_for_kind(view_type: int,
                    views: tuple[AccessoryView, ...] | list[AccessoryView],
                    ) -> AccessoryView | None:
    """The lowest-indexed real record of ``view_type`` -- the creation template.

    A new item is copied from a *real* record of the same kind, because the header
    carries fields this tool has not decoded (``+0x18`` takes 6 distinct values,
    ``+0x1c``/``+0x20``/``+0x28`` are unique per record).  Guessing them would be
    an unverified write; copying them makes the new record structurally identical
    to one the game itself wrote, and the three per-record words are documented as
    the one unverified part of 无中生有.
    """
    for view in views:
        if view.record_type == view_type:
            return view
    return None


def plan_creation(
    decrypted: bytes,
    *,
    record_type: int,
    level: int,
    effects: tuple[dict[str, int], ...] | list[dict[str, int]] = (),
    affix_db: AffixDb,
    item_db: ItemDb,
    known_ids: frozenset[int] | None = None,
    layout: records.InventoryLayout | None = None,
    slot_index: int | None = None,
    soul: bool = False,
) -> CreationPlan:
    """Plan a new 饰品/魂核 in a free slot, or refuse with the reason.

    ``effects`` lists the wanted slots as ``{"slot_index": i, "effect_id": id,
    "value": v, "metadata": m}``.  Everything else is copied from the donor, and
    the donor's own 同名固定 slots are enforced: they must stay exactly as they are
    (a new item may not gain, lose or change a fixed affix), which is the creation
    side of the "no adding fixed affixes" rule.
    """
    if known_ids is None:
        known_ids = frozenset(entry.effect_id for entry in affix_db.all())
    if layout is None:
        layout = records.locate_layout(decrypted, known_ids=known_ids)
    if item_db.describe(record_type) is None:
        raise CreationError(
            f"{record_type:#06x} 不在{'魂核' if soul else '饰品'}种类表内，"
            "无法生成（本工具只生成原始表里有的种类）"
        )
    if not isinstance(level, int) or isinstance(level, bool):
        raise CreationError("等级必须是整数")
    if not records.MIN_ITEM_LEVEL <= level <= records.MAX_ITEM_LEVEL:
        raise CreationError(
            f"等级必须在 {records.MIN_ITEM_LEVEL}..{records.MAX_ITEM_LEVEL} 之内，"
            f"{level} 超出范围"
        )

    # Donor: a real record of this exact kind (see _donor_for_kind).
    if soul:
        donor_views = list_soul_cores(decrypted, soul_db=affix_db,
                                       soul_item_db=item_db, layout=layout,
                                       known_ids=known_ids)
        donors = [view for view in donor_views if not view.unidentified]
    else:
        donors = [view for view in list_accessories(decrypted, layout=layout,
                                                    known_ids=known_ids)
                  if view.catalog_hits]
    donor = _donor_for_kind(record_type, donors)
    if donor is None:
        raise CreationError(
            f"存档里没有 {item_db.describe(record_type)} 的样本，无法生成："
            "新物品的固定词条与逐件字段必须从同种类的真实样本复制，"
            "本工具不会凭空猜这些字节"
        )

    report = find_free_slots(decrypted, layout=layout)
    if report.is_full:
        raise CreationError(report.describe())
    target = report.free_slots[0] if slot_index is None else slot_index
    if target not in report.free_slots:
        raise CreationError(
            f"槽位 #{target} 不是空位（只有 type==0 的槽位可以新建）；"
            f"{report.describe()}"
        )

    donor_record = bytes(decrypted[donor.offset:
                                   donor.offset + records.SCROLL_RECORD_SIZE])
    record = bytearray(donor_record)
    # Header: kind (both mirrored copies) and level (both mirrored copies).
    records.patch_record_item_id(bytes(record), record_type)
    record = bytearray(records.patch_record_item_id(bytes(record), record_type))
    record = bytearray(records.patch_record_level(bytes(record), level))

    fixed_slots = donor.fixed_slots(affix_db)
    by_slot = {}
    for effect in donor.effects:
        by_slot[effect.slot_index] = effect
    for edit in effects:
        slot = edit.get("slot_index")
        if not isinstance(slot, int) or isinstance(slot, bool):
            raise CreationError("新建词条缺少 slot_index")
        if not 0 <= slot < records.EFFECT_COUNT:
            raise CreationError(f"效果槽索引必须位于 0..{records.EFFECT_COUNT - 1}")
        if slot in fixed_slots:
            entry = affix_db.lookup(by_slot[slot].effect_id)
            raise CreationError(
                f"槽{slot + 1} 是 {item_db.describe(record_type)} 的同名固定词条"
                f"（{entry.name if entry else '未收录'}），新建时不能改动；"
                "它会按模板自动带入"
            )
        effect_id = edit.get("effect_id")
        if effect_id is None:
            raise CreationError(f"槽{slot + 1} 的新建词条缺少 effect_id")
        if effect_id == records.EMPTY_EFFECT_ID:
            value = 0
            metadata = 0
        else:
            entry = affix_db.require(effect_id)
            if entry.is_fixed:
                raise CreationError(
                    f"词条 {effect_id:#010x}「{entry.name}」是同名固定词条，"
                    "只能由模板带入，不能新增固定词条"
                )
            value = edit.get("value", entry.value)
            if not isinstance(value, int) or isinstance(value, bool):
                raise CreationError(f"槽{slot + 1} 的数值必须是整数")
            if value != entry.value:
                if not entry.has_value_range:
                    raise CreationError(
                        f"词条「{entry.name}」在原始表里没有数值区间，"
                        f"只能写目录值 {entry.value}"
                    )
                if not entry.allows_value(value):
                    raise CreationError(
                        f"词条「{entry.name}」的数值必须在 "
                        f"{entry.describe_value_range()} 之内，{value} 超出范围"
                    )
            metadata = edit.get("metadata", entry.flags << 8)
            if not isinstance(metadata, int) or isinstance(metadata, bool):
                raise CreationError(f"槽{slot + 1} 的标识必须是整数")
        try:
            record = bytearray(records.patch_effect_slots(
                bytes(record),
                [{"slot_index": slot, "effect_id": effect_id, "value": value,
                  "metadata": metadata}],
            ))
        except RecordError as error:
            raise CreationError(f"槽{slot + 1} 无法写入：{error}") from error

    parsed = records.read_effect_slots(bytes(record))
    # The fixed slots must still hold the donor's own fixed affixes.
    for slot in fixed_slots:
        if parsed[slot].effect_id != by_slot[slot].effect_id:
            raise CreationError(f"槽{slot + 1} 的固定词条未能保留，已中止")
    if records.record_is_empty(bytes(record)) or \
            not records.looks_like_item_record(bytes(record)):
        raise CreationError("生成的新记录头不合法，已中止（请把该存档反馈给作者）")
    return CreationPlan(
        slot_index=target,
        offset=layout.offset(target),
        record_type=record_type,
        level=level,
        donor_slot=donor.slot_index,
        donor_offset=donor.offset,
        record=bytes(record),
        effects=parsed,
        free_before=report.free_count,
    )


def apply_creations(
    decrypted: bytes,
    plans: tuple[CreationPlan, ...] | list[CreationPlan],
) -> bytes:
    """Write planned new records into their free slots (verify after writing)."""
    if not plans:
        raise EditorError("至少需要一个新建计划")
    output = bytearray(decrypted)
    for plan in plans:
        offset = plan.offset
        if not records.record_is_empty(bytes(output[offset:offset +
                                                      records.SCROLL_RECORD_SIZE])):
            raise CreationError(f"槽位 #{plan.slot_index} 在写入前已被占用，已中止")
        output[offset:offset + records.SCROLL_RECORD_SIZE] = plan.record
    for plan in plans:
        offset = plan.offset
        written = bytes(output[offset:offset + records.SCROLL_RECORD_SIZE])
        if written != plan.record:
            raise CreationError(f"新建 #{plan.slot_index} 未能正确写入，已中止")
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
