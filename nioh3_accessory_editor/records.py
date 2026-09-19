"""Nioh 3 accessory record parsing and effect-slot patching.

Layout knowledge comes from Nioh3-Scroll-Generator (``savegame.py`` /
``emaki_exchange.py``, ``docs/knowledge/versions/pc-v2.00.02/save-and-propagation.md``)
and the ``Nioh3 v2.21.CT`` equipment structure:

* The save holds fixed 0xE8-byte item records grouped in one inventory array.
  Each record's own layout is stable across the reference captures:
  ``type u16@0x00``, ``mirrored type u16@0x02``, ``item_count u16@0x04``,
  ``level u16@0x06``, ``mirrored level u16@0x08``, rarity mirrors at
  ``0x30``/``0x31``, and ``account_id`` across ``high16@0x02``, ``mid16@0x04``,
  ``low32@0x14``.
* Each record carries 7 effect slots of 0x18 bytes starting at ``0x34``:
  ``prefix u32@+0x00``, ``effect_id u32@+0x04``, ``value u32@+0x08``,
  ``metadata u32@+0x0C``, ``tail_0 u32@+0x10``, ``tail_1 u32@+0x14``.

**Where the array starts is version-scoped.**  The reference project captured
``0x176CCE`` with 400 slots for the v2.00.02/v2.01 save layout and states
explicitly that those offsets "must be recaptured after an update".  Hard-coding
it made the tool report zero records on a later game build, so the array is now
**located in the save** instead of assumed.  :func:`locate_layout` recognises a
record the way the reference does -- the type and the level each mirror
themselves, the type is not 0, and the slot is a scroll type or an item with
``item_count == 1`` -- then groups the hits by offset modulo 0xE8, because every
slot of one array shares that residue, and confirms the strongest alignment by
counting how many of the following 400 slots validate.  Requiring the item count
is what keeps a record's own free effect slots (``0xFFFFFFFF``, which mirrors
itself) from looking like nested records.  ``LEGACY_GROUP_OFFSET`` survives only
as an alignment hint and as the synthetic-fixture anchor.

A random block passes the recognised-record test with probability ~1/2^48, so
every hit is evidence; a save with no hits fails closed rather than editing
guessed bytes, and everything the tool reports states which anchor it used
(:attr:`InventoryLayout.anchor`).

Unverified boundary: which record *types* are accessories, and whether the effect
slots sit at ``0x34`` on a newer game build, still need confirmation against a
real save.  :func:`layout_diagnosis` plus the ``scan`` command report what a save
actually contains -- including how thin the evidence is -- so that confirmation
can be made from evidence instead of assumption.
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
    "InventoryLayout",
    "LEGACY_GROUP_OFFSET",
    "MAX_GROUP_SLOTS",
    "RARITY_NAMES",
    "RecordError",
    "SCROLL_GROUP_END",
    "SCROLL_GROUP_OFFSET",
    "SCROLL_RECORD_SIZE",
    "SCROLL_SLOT_COUNT",
    "SCROLL_TYPES",
    "account_id_from_record",
    "describe_diagnosis",
    "header_candidates",
    "iter_item_records",
    "layout_diagnosis",
    "locate_layout",
    "looks_like_item_record",
    "patch_effect_slots",
    "read_effect_slots",
    "read_item_record",
    "record_is_empty",
    "record_offset",
    "record_rarity",
]

#: Array start captured from the v2.00.02/v2.01 save layout by the reference
#: project.  It is a **hint**, not a truth: a newer game build can move the
#: array, which is exactly what made this editor report "0 records" on a real
#: save.  :func:`locate_layout` searches for the array instead of trusting it.
LEGACY_GROUP_OFFSET = 0x176CCE
SCROLL_GROUP_OFFSET = LEGACY_GROUP_OFFSET
SCROLL_SLOT_COUNT = 400
SCROLL_RECORD_SIZE = 0xE8
SCROLL_GROUP_END = SCROLL_GROUP_OFFSET + SCROLL_SLOT_COUNT * SCROLL_RECORD_SIZE

#: The reference array holds 400 fixed slots; a located group is capped to that
#: many so a shifted/misaligned cluster can never be reported as a longer array.
MAX_GROUP_SLOTS = SCROLL_SLOT_COUNT

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
    """A parsed equipment/accessory record from a located inventory array."""

    slot_index: int
    offset: int
    record: bytes
    record_type: int
    item_count: int
    level: int
    rarity: int
    account_id: int
    effects: tuple[EffectSlot, ...]

    @property
    def is_scroll(self) -> bool:
        return self.record_type in SCROLL_TYPES

    @property
    def kind_name(self) -> str:
        """Coarse family label (the fine item type is not recovered yet)."""
        if self.is_scroll:
            return "绘卷"
        if self.item_count == 1:
            return "装备/饰品"
        return "未知物品"

    @property
    def rarity_name(self) -> str:
        if 0 <= self.rarity < len(RARITY_NAMES):
            return RARITY_NAMES[self.rarity]
        return f"未知({self.rarity})"

    @property
    def occupied_effects(self) -> tuple[EffectSlot, ...]:
        return tuple(e for e in self.effects if not e.is_empty)


@dataclass(frozen=True, slots=True)
class InventoryLayout:
    """A located array of fixed 0xE8-byte item records.

    ``anchor`` is the byte offset the tool actually found -- never a hard-coded
    constant -- and ``slot_count`` how many slots the array covers here.  The
    anchor is the **first occupied slot** of the array: where the array's
    physical slot 0 sits is not recorded anywhere in the save, so slot numbers
    are reported relative to this anchor (they are stable for one save, and all
    reads and writes go through absolute offsets anyway).
    """

    anchor: int
    slot_count: int
    stride: int = SCROLL_RECORD_SIZE
    record_count: int = 0
    scroll_count: int = 0
    item_count: int = 0
    type_counts: tuple[tuple[int, int], ...] = ()
    truncated: bool = False
    candidate_total: int = 0
    residue_counts: tuple[tuple[int, int], ...] = ()

    @property
    def end(self) -> int:
        return self.anchor + self.slot_count * self.stride

    @property
    def is_legacy_anchor(self) -> bool:
        """Whether this anchor is slot-aligned with the captured 0x176CCE array.

        Alignment (not an exact offset) is what matters: the captured array
        started at 0x176CCE, so an anchor a whole number of slots later is the
        same array, just not starting on its first occupied slot.
        """
        delta = self.anchor - LEGACY_GROUP_OFFSET
        return (delta % SCROLL_RECORD_SIZE == 0
                and 0 <= delta < MAX_GROUP_SLOTS * SCROLL_RECORD_SIZE)

    @property
    def anchor_note(self) -> str:
        if self.is_legacy_anchor:
            return "与参考项目捕获的记录表对齐（v2.00.02/2.01 布局）"
        return "与参考项目捕获的记录表不对齐（存档布局版本不同）"

    def offset(self, slot_index: int) -> int:
        """Absolute byte offset of one slot, bounds-checked."""
        if not 0 <= slot_index < self.slot_count:
            raise RecordError(f"记录槽索引必须位于 0..{self.slot_count - 1}，"
                              f"实际 {slot_index}")
        return self.anchor + slot_index * self.stride

    def slot_index_of(self, offset: int) -> int | None:
        """Slot index for an absolute offset, or ``None`` when misaligned."""
        delta = offset - self.anchor
        if delta < 0 or delta % self.stride:
            return None
        index = delta // self.stride
        return index if index < self.slot_count else None

    def describe(self) -> str:
        return (f"记录表 {self.anchor:#08x} 起 {self.slot_count} 槽（占用 "
                f"{self.record_count}：绘卷 {self.scroll_count} / 其它物品 "
                f"{self.record_count - self.scroll_count}）· {self.anchor_note}")


# --------------------------------------------------------------------------
# Region / record helpers
# --------------------------------------------------------------------------

def record_offset(slot_index: int, *, layout: InventoryLayout) -> int:
    """Return the absolute save offset of one slot in a located layout.

    A layout is required on purpose: deriving an offset from the version-scoped
    captured constant silently pointed at the wrong bytes on a newer save.
    """
    return layout.offset(slot_index)


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

def _parse_item_record(slot_index: int, record: bytes, offset: int) -> AccessoryRecord:
    """Build an :class:`AccessoryRecord` from one already-validated record."""
    return AccessoryRecord(
        slot_index=slot_index,
        offset=offset,
        record=record,
        record_type=struct.unpack_from("<H", record, RECORD_TYPE_OFFSET)[0],
        item_count=struct.unpack_from("<H", record, RECORD_ITEM_COUNT_OFFSET)[0],
        level=struct.unpack_from("<H", record, RECORD_LEVEL_OFFSET)[0],
        rarity=record_rarity(record),
        account_id=account_id_from_record(record),
        effects=read_effect_slots(record),
    )


def read_item_record(
    decrypted: bytes,
    slot_index: int,
    *,
    layout: InventoryLayout | None = None,
) -> AccessoryRecord | None:
    """Parse one slot of a located array, or ``None`` when it is not a record."""
    if layout is None:
        layout = locate_layout(decrypted)
    offset = layout.offset(slot_index)
    end = offset + layout.stride
    if end > len(decrypted):
        raise RecordError(
            f"解密存档过小，无法读取记录槽 {slot_index}（需要到 {end:#x}，"
            f"实际 {len(decrypted):#x}）"
        )
    record = bytes(decrypted[offset:end])
    if record_is_empty(record) or not looks_like_item_record(record):
        return None
    return _parse_item_record(slot_index, record, offset)


def iter_item_records(
    decrypted: bytes,
    *,
    layout: InventoryLayout | None = None,
) -> tuple[AccessoryRecord, ...]:
    """Return every non-scroll item record of the located inventory array.

    The array is located with :func:`locate_layout` when the caller does not
    supply one, so a save whose layout moved relative to the captured constant
    is read correctly instead of reporting nothing.
    """
    if layout is None:
        layout = locate_layout(decrypted)
    results: list[AccessoryRecord] = []
    for slot_index in range(layout.slot_count):
        offset = layout.offset(slot_index)
        record = bytes(decrypted[offset:offset + layout.stride])
        if len(record) < layout.stride:
            break
        if record_is_empty(record) or not looks_like_item_record(record):
            continue
        results.append(_parse_item_record(slot_index, record, offset))
    return tuple(results)


# --------------------------------------------------------------------------
# Layout location
# --------------------------------------------------------------------------

def _duplicate_word_positions(decrypted: bytes) -> set[int]:
    """Offsets where a 2-byte word equals the word 2 bytes later.

    Uses one big-integer XOR (C speed) instead of 4.7 million Python
    iterations: byte ``o`` of ``data ^ (data >> 16)`` is zero exactly when
    ``data[o] == data[o + 2]`` and ``data[o + 1] == data[o + 3]``.
    """
    size = len(decrypted)
    if size < 4:
        return set()
    value = int.from_bytes(decrypted, "little")
    diff = (value ^ (value >> 16)).to_bytes(size, "little")
    positions: set[int] = set()
    start = 0
    while True:
        found = diff.find(b"\x00\x00", start)
        if found < 0:
            return positions
        positions.add(found)
        start = found + 1


def _mirror_positions(decrypted: bytes) -> set[int]:
    """Positions where the u16 at ``o`` equals the u16 at ``o + 2``."""
    return _duplicate_word_positions(decrypted)


def _recognized_type(decrypted: bytes, offset: int) -> int | None:
    """Return the record type at ``offset`` when it is a recognized record.

    "Recognized" is the reference project's test for a real record: the type and
    the level both mirror themselves, the type is not 0 (an empty slot clears
    it), and the record is either a scroll type or a non-scroll item with
    ``item_count == 1``.  The item-count requirement is what rejects a record's
    own free effect slots, which hold ``0xFFFFFFFF`` and would otherwise mirror
    themselves into a nested fake record.
    """
    if offset < 0 or offset + RECORD_MIRROR_LEVEL_OFFSET + 2 > len(decrypted):
        return None
    record_type = struct.unpack_from("<H", decrypted, offset)[0]
    if record_type == 0:
        return None
    if record_type != struct.unpack_from(
            "<H", decrypted, offset + RECORD_MIRROR_TYPE_OFFSET)[0]:
        return None
    level = struct.unpack_from("<H", decrypted, offset + RECORD_LEVEL_OFFSET)[0]
    if level != struct.unpack_from(
            "<H", decrypted, offset + RECORD_MIRROR_LEVEL_OFFSET)[0]:
        return None
    if record_type in SCROLL_TYPES:
        return record_type
    if struct.unpack_from(
            "<H", decrypted, offset + RECORD_ITEM_COUNT_OFFSET)[0] == 1:
        return record_type
    return None


def header_candidates(decrypted: bytes) -> tuple[int, ...]:
    """Offsets that carry a recognized record header.

    The mirrored-word prefilter runs at C speed over the whole save; every
    shortlisted offset is then validated with :func:`_recognized_type`, so a hit
    is real evidence (a random block passes with probability ~1/2^48).
    """
    mirrored = _mirror_positions(decrypted)
    if not mirrored:
        return ()
    limit = len(decrypted) - 10
    candidates: list[int] = []
    for offset in sorted(mirrored):
        if offset > limit:
            break
        if offset + 6 not in mirrored:
            continue
        if _recognized_type(decrypted, offset) is not None:
            candidates.append(offset)
    return tuple(candidates)


#: How many candidate anchors are structurally scored when locating the array.
#: The true alignment is normally the densest residue cluster, so this only ever
#: decides genuine ties (e.g. a save holding a single record).
_MAX_ANCHOR_CANDIDATES = 24


def locate_layout(decrypted: bytes) -> InventoryLayout:
    """Find the save's array of 0xE8-byte item records.

    The array's start is **version-scoped** in the reference capture, so it is
    searched for here: recognized records are grouped by their offset modulo
    0xE8 (every slot of one array shares that residue), and the best-aligned
    candidate is confirmed by counting how many of the following 400 slots
    validate as records.  When the records lie inside the captured 0x176CCE
    array, that array's own start is used as slot 0 so slot numbers stay stable;
    otherwise the first occupied slot becomes the anchor.

    Raises :class:`RecordError` with an actionable message when no array is
    found, so the caller fails closed instead of reading (or later writing)
    bytes it guessed at.
    """
    candidates = header_candidates(decrypted)
    if not candidates:
        raise RecordError(
            "未在存档中找到物品记录表：没有任何 0xE8 槽位满足记录头特征"
            "（type==镜像 type、level==镜像 level，且 item_count==1 或为绘卷类型）。"
            "请运行 scan 查看诊断。"
        )

    by_residue: dict[int, list[int]] = {}
    for offset in candidates:
        by_residue.setdefault(offset % SCROLL_RECORD_SIZE, []).append(offset)
    residue_counts = tuple(sorted(
        ((residue, len(items)) for residue, items in by_residue.items()),
        key=lambda item: (-item[1], item[0]),
    ))

    # Score the most promising alignments: the densest residue clusters first,
    # the captured alignment first among equals (same evidence, comparable
    # reports), then by offset so the result is deterministic.
    legacy_residue = LEGACY_GROUP_OFFSET % SCROLL_RECORD_SIZE
    ranked = sorted(
        by_residue.items(),
        key=lambda item: (-len(item[1]), item[0] != legacy_residue, item[1][0]),
    )
    probes: list[int] = []
    for _residue, offsets in ranked:
        for offset in offsets:
            if offset not in probes:
                probes.append(offset)
        if len(probes) >= _MAX_ANCHOR_CANDIDATES:
            break
    probes = probes[:_MAX_ANCHOR_CANDIDATES]

    best: InventoryLayout | None = None
    for anchor in probes:
        slot_count = min(MAX_GROUP_SLOTS,
                         max(1, (len(decrypted) - anchor) // SCROLL_RECORD_SIZE))
        window = _layout_from_window(decrypted, anchor, slot_count, False,
                                     len(candidates), residue_counts)
        if best is None or _layout_score(window) > _layout_score(best):
            best = window
    assert best is not None  # probes is never empty when candidates exist

    # When the records sit inside the array captured for v2.00.02/v2.01, use that
    # array's own start as slot 0: slot numbers then stay stable (they do not
    # shift when the first item is sold) and match the reference numbering.  A
    # save whose array moved keeps the first occupied slot as its anchor.
    legacy_delta = best.anchor - LEGACY_GROUP_OFFSET
    if (0 <= legacy_delta < MAX_GROUP_SLOTS * SCROLL_RECORD_SIZE
            and legacy_delta % SCROLL_RECORD_SIZE == 0):
        slot_count = min(MAX_GROUP_SLOTS,
                         max(1, (len(decrypted) - LEGACY_GROUP_OFFSET)
                             // SCROLL_RECORD_SIZE))
        return _layout_from_window(decrypted, LEGACY_GROUP_OFFSET, slot_count,
                                   False, len(candidates), residue_counts)

    span = (candidates[-1] - best.anchor) // SCROLL_RECORD_SIZE + 1
    if span > MAX_GROUP_SLOTS:
        best = _layout_from_window(decrypted, best.anchor, best.slot_count, True,
                                   len(candidates), residue_counts)
    return best


def _layout_score(layout: InventoryLayout) -> tuple[int, int, int]:
    """Rank candidate anchors: most records, then scrolls, then items."""
    return (layout.record_count, layout.scroll_count, layout.item_count)


def _layout_from_window(
    decrypted: bytes,
    anchor: int,
    slot_count: int,
    truncated: bool,
    candidate_total: int,
    residue_counts: tuple[tuple[int, int], ...],
) -> InventoryLayout:
    """Measure one located window: how many slots hold which record families."""
    type_counts: dict[int, int] = {}
    scrolls = items = records = 0
    for slot_index in range(slot_count):
        offset = anchor + slot_index * SCROLL_RECORD_SIZE
        record_type = _recognized_type(decrypted, offset)
        if record_type is None:
            continue
        records += 1
        type_counts[record_type] = type_counts.get(record_type, 0) + 1
        if record_type in SCROLL_TYPES:
            scrolls += 1
        else:
            items += 1
    return InventoryLayout(
        anchor=anchor,
        slot_count=slot_count,
        record_count=records,
        scroll_count=scrolls,
        item_count=items,
        type_counts=tuple(sorted(type_counts.items(), key=lambda item: (-item[1],
                                                                       item[0]))),
        truncated=truncated,
        candidate_total=candidate_total,
        residue_counts=residue_counts,
    )


def _effect_slot_is_incomplete(record: bytes) -> bool:
    """Reference rule: byte ``+0x0E`` of any effect slot has its sign bit set."""
    return any(
        record[EFFECT_START + index * EFFECT_STRIDE + 0x0E] & 0x80
        for index in range(EFFECT_COUNT)
    )


def _confidence_note(layout: InventoryLayout) -> str:
    """State plainly how strong the located evidence is."""
    count = layout.record_count
    if count >= 3 and not layout.truncated:
        return f"证据充分：{count} 条记录落在同一 0xE8 对齐上"
    if count >= 1:
        return (f"证据较弱：只有 {count} 条记录命中；"
                "若读取结果与游戏内不符，请把 scan 输出反馈给作者")
    return "未发现任何记录"


def layout_diagnosis(decrypted: bytes, *, preview_records: int = 4) -> dict[str, object]:
    """Collect everything worth knowing when a save's records are inspected.

    ``scan`` prints this, so a save whose record family is still unconfirmed can
    be reported back as evidence instead of guesswork.
    """
    candidates = header_candidates(decrypted)
    diagnosis: dict[str, object] = {
        "save_size": len(decrypted),
        "candidate_count": len(candidates),
        "candidate_offsets": list(candidates[:16]),
        "layout": None,
        "confidence": "未发现任何记录",
        "type_counts": [],
        "rarity_counts": [],
        "level_range": None,
        "incomplete_records": 0,
        "effect_preview": [],
        "notes": [],
    }
    try:
        layout = locate_layout(decrypted)
    except RecordError as error:
        diagnosis["notes"] = [str(error)]
        return diagnosis

    diagnosis["confidence"] = _confidence_note(layout)

    diagnosis["layout"] = {
        "anchor": layout.anchor,
        "slot_count": layout.slot_count,
        "stride": layout.stride,
        "record_count": layout.record_count,
        "scroll_count": layout.scroll_count,
        "item_count": layout.item_count,
        "truncated": layout.truncated,
        "legacy_anchor": layout.is_legacy_anchor,
        "anchor_note": layout.anchor_note,
        "candidate_total": layout.candidate_total,
        "residue_counts": list(layout.residue_counts),
    }

    rarities: dict[int, int] = {}
    levels: list[int] = []
    incomplete = 0
    preview: list[dict[str, object]] = []
    for slot_index in range(layout.slot_count):
        offset = layout.offset(slot_index)
        record = bytes(decrypted[offset:offset + layout.stride])
        if len(record) < layout.stride or record_is_empty(record):
            continue
        if not looks_like_item_record(record):
            continue
        parsed = _parse_item_record(slot_index, record, offset)
        rarities[parsed.rarity] = rarities.get(parsed.rarity, 0) + 1
        levels.append(parsed.level)
        if _effect_slot_is_incomplete(record):
            incomplete += 1
        if len(preview) < preview_records and not parsed.is_scroll:
            preview.append({
                "slot": slot_index,
                "offset": offset,
                "type": parsed.record_type,
                "level": parsed.level,
                "rarity": parsed.rarity,
                "account_id": parsed.account_id,
                "effect_slots_hex": [
                    record[EFFECT_START + index * EFFECT_STRIDE:
                           EFFECT_START + (index + 1) * EFFECT_STRIDE].hex(" ")
                    for index in range(EFFECT_COUNT)
                ],
                "occupied_slots": [slot.slot_index
                                   for slot in parsed.occupied_effects],
            })

    diagnosis["type_counts"] = [{"type": type_, "count": count}
                                for type_, count in layout.type_counts]
    diagnosis["rarity_counts"] = [{"rarity": rarity, "count": count}
                                  for rarity, count in sorted(rarities.items())]
    diagnosis["level_range"] = ([min(levels), max(levels)] if levels else None)
    diagnosis["incomplete_records"] = incomplete
    diagnosis["effect_preview"] = preview
    notes = list(layout.notes) if hasattr(layout, "notes") else []
    if not preview:
        notes.append("记录表内没有非绘卷物品记录；若这不符合预期，"
                     "请把本诊断与存档一并反馈。")
    diagnosis["notes"] = notes
    return diagnosis


def describe_diagnosis(diagnosis: dict[str, object]) -> tuple[str, ...]:
    """Render :func:`layout_diagnosis` as human-readable lines."""
    lines: list[str] = [
        f"存档大小  : {diagnosis['save_size']:#x} 字节",
        f"记录头命中: {diagnosis['candidate_count']} 处"
        + (f"（前几处 {', '.join(f'{o:#x}' for o in diagnosis['candidate_offsets'][:5])}）"
           if diagnosis["candidate_offsets"] else ""),
    ]
    layout = diagnosis.get("layout")
    if not isinstance(layout, dict):
        lines.append("记录表定位: 失败")
        lines.extend(f"  {note}" for note in diagnosis["notes"])  # type: ignore[union-attr]
        return tuple(lines)

    lines.append(
        f"记录表定位: {layout['anchor']:#08x} 起 {layout['slot_count']} 槽"
        f"（步长 {layout['stride']:#x}，占用 {layout['record_count']}）"
    )
    lines.append(f"  · {layout['anchor_note']}")
    lines.append(f"  · {diagnosis['confidence']}")
    lines.append(
        f"  · 绘卷 {layout['scroll_count']} 条 / 非绘卷物品 {layout['item_count']} 条"
        f" / 其它 {layout['record_count'] - layout['scroll_count'] - layout['item_count']} 条"
    )
    if layout["truncated"]:
        lines.append("  · 命中跨越超过 400 槽，只取最密的一段（可能有多个记录表）")
    types = diagnosis["type_counts"]
    if types:
        shown = ", ".join(f"0x{item['type']:04X}×{item['count']}" for item in types[:8])  # type: ignore[index]
        more = "" if len(types) <= 8 else f" …共 {len(types)} 种"
        lines.append(f"  · 记录类型分布: {shown}{more}")
    rarities = diagnosis["rarity_counts"]
    if rarities:
        lines.append("  · 品质分布: " + ", ".join(
            f"{item['rarity']}×{item['count']}" for item in rarities))  # type: ignore[index]
    if diagnosis["level_range"]:
        low, high = diagnosis["level_range"]  # type: ignore[misc]
        lines.append(f"  · 等级范围: {low}..{high}")
    lines.append(f"  · 效果槽未完成标记（+0x0E 符号位）: "
                 f"{diagnosis['incomplete_records']} 条记录")
    for item in diagnosis["effect_preview"]:  # type: ignore[union-attr]
        lines.append(
            f"  · 槽 {item['slot']} @ {item['offset']:#08x} 类型 0x{item['type']:04X}"
            f" 等级 {item['level']} 品质 {item['rarity']}"
            f"（已占用效果槽 {item['occupied_slots'] or '无'}）"
        )
        for index, hexdump in enumerate(item["effect_slots_hex"]):
            lines.append(f"      [{index}] {hexdump}")
    lines.extend(f"  ! {note}" for note in diagnosis["notes"])  # type: ignore[union-attr]
    return tuple(lines)
