"""Nioh 3 accessory record parsing and effect-slot patching.

Layout knowledge comes from Nioh3-Scroll-Generator (``savegame.py`` /
``emaki_exchange.py``) and the ``Nioh3 v2.21.CT`` equipment structure:

* The user save has a fixed record region at ``0x176CCE`` holding 400 slots of
  ``0xE8`` bytes each.  Scrolls are one record family there; the same region
  also holds non-scroll equipment/accessory records sharing the same header.
* Record header (offsets relative to the 0xE8 record):
  ``type u16@0x00``, ``mirrored type u16@0x02``, ``item_count u16@0x04``,
  ``level u16@0x06``, ``mirrored level u16@0x08``.
  ``account_id`` occupies ``high16@0x02``, ``mid16@0x04``, ``low32@0x14``.
* Each record carries 7 effect slots.  In the save layout each slot is 0x18
  bytes starting at ``0x34`` (as verified for scrolls); per slot:
  ``prefix u32@+0x00``, ``effect_id u32@+0x04``, ``value u32@+0x08``,
  ``metadata u32@+0x0C``, ``tail_0 u32@+0x10``, ``tail_1 u32@+0x14``.
  (The Cheat Engine memory structure shows +0x38 with a 0x18 stride; the save
  file uses +0x34.  ``EFFECT_START`` is a module constant so the layout can be
  re-pointed after a real-save confirmation.)

Unverified boundary: the exact accessory effect-slot base in a *real* save
still needs confirmation against a decrypted Nioh 3 v2.21 save.  Everything in
this module is derived from the reference scroll layout and the CT equipment
structure, and is covered by round-trip tests on synthetic records.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

__all__ = [
    "AccessoryRecord",
    "EFFECT_COUNT",
    "EFFECT_FIELD_OFFSETS",
    "EFFECT_START",
    "EFFECT_STRIDE",
    "EMPTY_EFFECT_ID",
    "EffectSlot",
    "RARITY_NAMES",
    "RecordError",
    "SCROLL_GROUP_END",
    "SCROLL_GROUP_OFFSET",
    "SCROLL_RECORD_SIZE",
    "SCROLL_SLOT_COUNT",
    "SCROLL_TYPES",
    "account_id_from_record",
    "iter_item_records",
    "looks_like_item_record",
    "patch_effect_slots",
    "read_effect_slots",
    "read_item_record",
    "record_is_empty",
    "record_offset",
    "record_rarity",
]

SCROLL_GROUP_OFFSET = 0x176CCE
SCROLL_SLOT_COUNT = 400
SCROLL_RECORD_SIZE = 0xE8
SCROLL_GROUP_END = SCROLL_GROUP_OFFSET + SCROLL_SLOT_COUNT * SCROLL_RECORD_SIZE

EFFECT_START = 0x34
EFFECT_STRIDE = 0x18
EFFECT_COUNT = 7
EMPTY_EFFECT_ID = 0xFFFFFFFF

#: Effect ids that mean "this slot holds no affix".
#: The reference fixture writes 0xFFFFFFFF for free slots; a never-written slot
#: in a real save can be all zeros, so both are treated as empty.
EMPTY_EFFECT_IDS = frozenset((0x00000000, EMPTY_EFFECT_ID))

#: Allowed keys of an effect edit and their byte offset inside one slot.
EFFECT_FIELD_OFFSETS = {
    "prefix": 0x00,
    "effect_id": 0x04,
    "value": 0x08,
    "metadata": 0x0C,
    "tail_0": 0x10,
    "tail_1": 0x14,
}

_UINT32_MAX = 0xFFFFFFFF

# Record header field offsets (relative to the 0xE8 record).
RECORD_TYPE_OFFSET = 0x00
RECORD_MIRROR_TYPE_OFFSET = 0x02
RECORD_ITEM_COUNT_OFFSET = 0x04
RECORD_LEVEL_OFFSET = 0x06
RECORD_MIRROR_LEVEL_OFFSET = 0x08
RECORD_RARITY_OFFSET = 0x30
RECORD_RARITY_HIGH_OFFSET = 0x31
RECORD_ACCOUNT_LOW_OFFSET = 0x14

# Six known scroll record types (CATEGORY_TO_TYPE in the reference project).
SCROLL_TYPES = frozenset((0x0000, 0x1E82, 0x516D, 0xE604, 0xDD82, 0xD523))

# Cheat Engine quality (品质) list values.
RARITY_NAMES = ("粗物", "名器", "大名器", "特大名器", "神器", "神宝")


class RecordError(ValueError):
    """Raised when a record is malformed or its layout is unsupported."""


@dataclass(frozen=True, slots=True)
class EffectSlot:
    """Raw fields of one effect slot within a record."""

    slot_index: int
    prefix: int
    effect_id: int
    value: int
    metadata: int
    tail_0: int
    tail_1: int

    @property
    def is_empty(self) -> bool:
        return self.effect_id in EMPTY_EFFECT_IDS

    def to_tuple(self) -> tuple[int, int, int, int, int, int]:
        return (self.prefix, self.effect_id, self.value,
                self.metadata, self.tail_0, self.tail_1)


@dataclass(frozen=True, slots=True)
class AccessoryRecord:
    """A parsed equipment/accessory record from the fixed record region."""

    slot_index: int
    record: bytes
    record_type: int
    item_count: int
    level: int
    rarity: int
    account_id: int
    effects: tuple[EffectSlot, ...]

    @property
    def offset(self) -> int:
        return record_offset(self.slot_index)

    @property
    def rarity_name(self) -> str:
        if 0 <= self.rarity < len(RARITY_NAMES):
            return RARITY_NAMES[self.rarity]
        return f"未知({self.rarity})"

    @property
    def occupied_effects(self) -> tuple[EffectSlot, ...]:
        return tuple(e for e in self.effects if not e.is_empty)


# --------------------------------------------------------------------------
# Region / record helpers
# --------------------------------------------------------------------------

def record_offset(slot_index: int) -> int:
    """Return the absolute save offset of one record slot."""
    if not 0 <= slot_index < SCROLL_SLOT_COUNT:
        raise RecordError(f"记录槽索引必须位于 0..{SCROLL_SLOT_COUNT - 1}")
    return SCROLL_GROUP_OFFSET + slot_index * SCROLL_RECORD_SIZE


def record_is_empty(record: bytes) -> bool:
    """A free slot has record type 0 at +0x00 (native deletion semantics)."""
    if len(record) != SCROLL_RECORD_SIZE:
        raise RecordError("record must be exactly 0xE8 bytes")
    return struct.unpack_from("<H", record, RECORD_TYPE_OFFSET)[0] == 0


def account_id_from_record(record: bytes) -> int:
    """Reassemble the 64-bit account id stored across three header fields."""
    if len(record) != SCROLL_RECORD_SIZE:
        raise RecordError("record must be exactly 0xE8 bytes")
    high = struct.unpack_from("<H", record, RECORD_MIRROR_TYPE_OFFSET)[0]
    middle = struct.unpack_from("<H", record, RECORD_ITEM_COUNT_OFFSET)[0]
    low = struct.unpack_from("<I", record, RECORD_ACCOUNT_LOW_OFFSET)[0]
    return (high << 48) | (middle << 32) | low


def looks_like_item_record(
    record: bytes,
    *,
    scroll_types: frozenset[int] | None = None,
) -> bool:
    """Return whether ``record`` has the captured equipment header.

    Mirrors ``_looks_like_non_scroll_item_record`` from the reference project:
    type == mirrored type, type not in the scroll type table, item count == 1,
    and level == mirrored level.  Accessories are non-scroll item records, so
    they satisfy this predicate.
    """
    if len(record) != SCROLL_RECORD_SIZE:
        return False
    record_type = struct.unpack_from("<H", record, RECORD_TYPE_OFFSET)[0]
    mirrored_type = struct.unpack_from("<H", record, RECORD_MIRROR_TYPE_OFFSET)[0]
    item_count = struct.unpack_from("<H", record, RECORD_ITEM_COUNT_OFFSET)[0]
    level = struct.unpack_from("<H", record, RECORD_LEVEL_OFFSET)[0]
    mirrored_level = struct.unpack_from("<H", record, RECORD_MIRROR_LEVEL_OFFSET)[0]
    if scroll_types is None:
        scroll_types = SCROLL_TYPES
    return (
        record_type != 0
        and record_type == mirrored_type
        and record_type not in scroll_types
        and item_count == 1
        and level == mirrored_level
    )


def record_rarity(record: bytes) -> int:
    """Read the 4-bit rarity field (品质) from a record."""
    if len(record) != SCROLL_RECORD_SIZE:
        raise RecordError("record must be exactly 0xE8 bytes")
    rarity = record[RECORD_RARITY_OFFSET] & 0x0F
    if rarity == 0:
        rarity = record[RECORD_RARITY_HIGH_OFFSET] & 0x0F
    return rarity


# --------------------------------------------------------------------------
# Effect slot read / write
# --------------------------------------------------------------------------

def read_effect_slots(record: bytes) -> tuple[EffectSlot, ...]:
    """Parse the seven editable effect slots of one record."""
    if len(record) != SCROLL_RECORD_SIZE:
        raise RecordError("record must be exactly 0xE8 bytes")
    slots: list[EffectSlot] = []
    for slot_index in range(EFFECT_COUNT):
        base = EFFECT_START + slot_index * EFFECT_STRIDE
        prefix, effect_id, value, metadata, tail_0, tail_1 = struct.unpack_from(
            "<6I", record, base
        )
        slots.append(
            EffectSlot(
                slot_index=slot_index,
                prefix=prefix,
                effect_id=effect_id,
                value=value,
                metadata=metadata,
                tail_0=tail_0,
                tail_1=tail_1,
            )
        )
    return tuple(slots)


def _validate_effect_edit(edit: dict[str, int], *, allow_record_index: bool) -> dict[str, int]:
    """Validate one effect edit, rejecting unknown keys and out-of-range values.

    ``record_index`` is an editor-level routing key, not a slot field: it is
    accepted (and ignored here) only when ``allow_record_index`` is set, so the
    editor can pass its own edit dictionaries straight through.
    """
    if not isinstance(edit, dict):
        raise RecordError("effect edit must be a dict")
    allowed = set(EFFECT_FIELD_OFFSETS) | {"slot_index"}
    if allow_record_index:
        allowed.add("record_index")
    unknown = set(edit) - allowed
    if unknown:
        raise RecordError(f"未知的编辑字段: {', '.join(sorted(unknown))}")

    slot_index = edit.get("slot_index")
    if slot_index is None:
        raise RecordError("编辑缺少 slot_index")
    if not isinstance(slot_index, int) or isinstance(slot_index, bool):
        raise RecordError("slot_index 必须是整数")
    if not 0 <= slot_index < EFFECT_COUNT:
        raise RecordError(f"槽位索引必须位于 0..{EFFECT_COUNT - 1}，实际 {slot_index}")

    normalized: dict[str, int] = {"slot_index": slot_index}
    for name in EFFECT_FIELD_OFFSETS:
        if name not in edit:
            continue
        raw = edit[name]
        if not isinstance(raw, int) or isinstance(raw, bool):
            raise RecordError(f"{name} 必须是整数")
        if not 0 <= raw <= _UINT32_MAX:
            raise RecordError(f"{name} 必须落在 uint32 范围内，实际 {raw}")
        normalized[name] = raw
    if len(normalized) == 1:
        raise RecordError("至少需要修改一个词条字段")
    return normalized


def patch_effect_slots(
    record: bytes,
    edits: tuple[dict[str, int], ...] | list[dict[str, int]],
    *,
    allow_record_index: bool = False,
) -> bytes:
    """Return a new record with the given slot field edits applied.

    Edits are validated strictly: unknown field names, non-integer values,
    out-of-range slot indexes and duplicate slots all raise ``RecordError``
    instead of silently corrupting neighbouring record bytes.
    """
    if len(record) != SCROLL_RECORD_SIZE:
        raise RecordError("record must be exactly 0xE8 bytes")
    if not edits:
        raise RecordError("至少需要一个编辑项")

    normalized = [
        _validate_effect_edit(edit, allow_record_index=allow_record_index)
        for edit in edits
    ]
    slots = [edit["slot_index"] for edit in normalized]
    if len(set(slots)) != len(slots):
        raise RecordError("同一个词条槽不能被重复编辑")

    output = bytearray(record)
    for edit in normalized:
        base = EFFECT_START + edit["slot_index"] * EFFECT_STRIDE
        for name, rel in EFFECT_FIELD_OFFSETS.items():
            if name in edit:
                struct.pack_into("<I", output, base + rel, edit[name])
    return bytes(output)


# --------------------------------------------------------------------------
# Region scanning
# --------------------------------------------------------------------------

def _parse_item_record(slot_index: int, record: bytes) -> AccessoryRecord:
    """Build an :class:`AccessoryRecord` from one already-validated record."""
    return AccessoryRecord(
        slot_index=slot_index,
        record=record,
        record_type=struct.unpack_from("<H", record, RECORD_TYPE_OFFSET)[0],
        item_count=struct.unpack_from("<H", record, RECORD_ITEM_COUNT_OFFSET)[0],
        level=struct.unpack_from("<H", record, RECORD_LEVEL_OFFSET)[0],
        rarity=record_rarity(record),
        account_id=account_id_from_record(record),
        effects=read_effect_slots(record),
    )


def read_item_record(decrypted: bytes, slot_index: int) -> AccessoryRecord | None:
    """Parse one record slot, or return ``None`` when it is not an item record."""
    offset = record_offset(slot_index)
    end = offset + SCROLL_RECORD_SIZE
    if end > len(decrypted):
        raise RecordError(
            f"解密存档过小，无法读取记录槽 {slot_index}（需要到 {end:#x}，"
            f"实际 {len(decrypted):#x}）"
        )
    record = bytes(decrypted[offset:end])
    if record_is_empty(record) or not looks_like_item_record(record):
        return None
    return _parse_item_record(slot_index, record)


def iter_item_records(decrypted: bytes) -> tuple[AccessoryRecord, ...]:
    """Scan the fixed record region and parse non-scroll item records.

    Returns every record that looks like an equipment/accessory item.  In a
    real save accessories are a subset of these records; item-type filtering
    against the CT 物品列表 is the editor's job.
    """
    if len(decrypted) < SCROLL_GROUP_END:
        raise RecordError(
            f"解密存档过小，无法包含记录区（需要至少 {SCROLL_GROUP_END:#x} 字节，"
            f"实际 {len(decrypted):#x} 字节）"
        )
    results: list[AccessoryRecord] = []
    for slot_index in range(SCROLL_SLOT_COUNT):
        offset = record_offset(slot_index)
        record = bytes(decrypted[offset:offset + SCROLL_RECORD_SIZE])
        if record_is_empty(record):
            continue
        if not looks_like_item_record(record):
            continue
        results.append(_parse_item_record(slot_index, record))
    return tuple(results)
