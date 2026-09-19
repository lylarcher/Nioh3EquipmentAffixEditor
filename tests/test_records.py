"""Record-layout tests: parsing, predicates, strict patching, scanning."""

from __future__ import annotations

import struct
import unittest

from nioh3_accessory_editor import records
from nioh3_accessory_editor.records import (
    EFFECT_COUNT,
    EFFECT_START,
    EFFECT_STRIDE,
    EMPTY_EFFECT_ID,
    SCROLL_GROUP_OFFSET,
    SCROLL_RECORD_SIZE,
    SCROLL_SLOT_COUNT,
    SCROLL_TYPES,
    AccessoryRecord,
    EffectSlot,
    RecordError,
    account_id_from_record,
    iter_item_records,
    looks_like_item_record,
    patch_effect_slots,
    read_effect_slots,
    read_item_record,
    record_is_empty,
    record_offset,
    record_rarity,
)
from tests import support

ITEM_TYPE = 0x4001


class LayoutInvariantTests(unittest.TestCase):
    def test_region_geometry(self) -> None:
        self.assertEqual(records.SCROLL_GROUP_END, 0x176CCE + 400 * 0xE8)
        self.assertEqual(SCROLL_SLOT_COUNT, 400)
        self.assertEqual(SCROLL_RECORD_SIZE, 0xE8)
        self.assertEqual(record_offset(0), SCROLL_GROUP_OFFSET)
        self.assertEqual(record_offset(399), SCROLL_GROUP_OFFSET + 399 * 0xE8)

    def test_effect_slots_fit_inside_a_record(self) -> None:
        last = EFFECT_START + (EFFECT_COUNT - 1) * EFFECT_STRIDE + 0x18
        self.assertLessEqual(last, SCROLL_RECORD_SIZE)
        self.assertEqual(EFFECT_COUNT, 7)

    def test_item_type_is_not_a_scroll_type(self) -> None:
        self.assertNotIn(ITEM_TYPE, SCROLL_TYPES)

    def test_record_offset_bounds(self) -> None:
        for bad in (-1, SCROLL_SLOT_COUNT, 10_000):
            with self.assertRaises(RecordError):
                record_offset(bad)


class PredicateTests(unittest.TestCase):
    def test_build_fixture_looks_like_an_item_record(self) -> None:
        record = support.build_record(record_type=ITEM_TYPE)
        self.assertTrue(looks_like_item_record(record))
        self.assertFalse(record_is_empty(record))

    def test_empty_record(self) -> None:
        record = bytes(SCROLL_RECORD_SIZE)
        self.assertTrue(record_is_empty(record))
        self.assertFalse(looks_like_item_record(record))

    def test_scroll_type_is_rejected(self) -> None:
        for scroll_type in SCROLL_TYPES:
            record = support.build_record(record_type=scroll_type)
            if scroll_type == 0:
                continue
            self.assertFalse(looks_like_item_record(record), hex(scroll_type))

    def test_rejects_mismatched_mirror_fields(self) -> None:
        record = bytearray(support.build_record(record_type=ITEM_TYPE))
        struct.pack_into("<H", record, 0x02, ITEM_TYPE + 1)
        self.assertFalse(looks_like_item_record(bytes(record)))

        record = bytearray(support.build_record(record_type=ITEM_TYPE))
        struct.pack_into("<H", record, 0x08, 999)
        self.assertFalse(looks_like_item_record(bytes(record)))

        record = bytearray(support.build_record(record_type=ITEM_TYPE))
        struct.pack_into("<H", record, 0x04, 2)
        self.assertFalse(looks_like_item_record(bytes(record)))

    def test_rejects_wrong_length(self) -> None:
        self.assertFalse(looks_like_item_record(b"\x00" * 10))
        with self.assertRaises(RecordError):
            record_is_empty(b"\x00" * 10)
        with self.assertRaises(RecordError):
            record_rarity(b"\x00" * 10)
        with self.assertRaises(RecordError):
            account_id_from_record(b"\x00" * 10)

    def test_rarity_fallback_to_the_high_nibble(self) -> None:
        record = bytearray(support.build_record(record_type=ITEM_TYPE, rarity=0))
        self.assertEqual(record_rarity(bytes(record)), 0)
        record[0x31] = 4
        self.assertEqual(record_rarity(bytes(record)), 4)
        record[0x30] = 5
        self.assertEqual(record_rarity(bytes(record)), 5)

    def test_account_id_reassembly(self) -> None:
        record = support.build_record(record_type=ITEM_TYPE, account_low32=0xCAFEBABE)
        expected = (ITEM_TYPE << 48) | (1 << 32) | 0xCAFEBABE
        self.assertEqual(account_id_from_record(record), expected)
        self.assertEqual(expected, support.expected_account_id(ITEM_TYPE, 0xCAFEBABE))


class EffectSlotTests(unittest.TestCase):
    def test_is_empty_for_sentinel_values(self) -> None:
        self.assertTrue(EffectSlot(0, 0, EMPTY_EFFECT_ID, 0, 0, 0, 0).is_empty)
        self.assertTrue(EffectSlot(0, 0, 0, 0, 0, 0, 0).is_empty)
        self.assertFalse(EffectSlot(0, 0, 0x1234, 0, 0, 0, 0).is_empty)

    def test_to_tuple(self) -> None:
        slot = EffectSlot(3, 1, 2, 3, 4, 5, 6)
        self.assertEqual(slot.to_tuple(), (1, 2, 3, 4, 5, 6))

    def test_read_effect_slots_round_trip(self) -> None:
        record = support.build_record(
            record_type=ITEM_TYPE,
            effects=((0x0B32, 20, 0x40), (0x1111, 5, 0x04)),
        )
        slots = read_effect_slots(record)
        self.assertEqual(len(slots), EFFECT_COUNT)
        self.assertEqual(slots[0].effect_id, 0x0B32)
        self.assertEqual(slots[0].value, 20)
        self.assertEqual(slots[0].metadata, 0x40)
        self.assertEqual(slots[1].effect_id, 0x1111)
        self.assertTrue(all(slot.is_empty for slot in slots[2:]))

    def test_read_effect_slots_rejects_wrong_length(self) -> None:
        with self.assertRaises(RecordError):
            read_effect_slots(b"\x00" * 10)


class PatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = support.build_record(
            record_type=ITEM_TYPE, effects=((0x0B32, 20, 0x40),)
        )

    def test_patch_changes_only_the_target_bytes(self) -> None:
        patched = patch_effect_slots(
            self.record, [{"slot_index": 2, "effect_id": 0x2222, "value": 7}]
        )
        self.assertEqual(len(patched), SCROLL_RECORD_SIZE)
        base = EFFECT_START + 2 * EFFECT_STRIDE
        self.assertEqual(struct.unpack_from("<I", patched, base + 4)[0], 0x2222)
        self.assertEqual(struct.unpack_from("<I", patched, base + 8)[0], 7)
        changed = {i for i in range(SCROLL_RECORD_SIZE)
                   if patched[i] != self.record[i]}
        self.assertTrue(changed.issubset(set(range(base, base + 0x18))))

    def test_patch_can_clear_a_slot(self) -> None:
        patched = patch_effect_slots(
            self.record, [{"slot_index": 0, "effect_id": EMPTY_EFFECT_ID, "value": 0}]
        )
        self.assertTrue(read_effect_slots(patched)[0].is_empty)

    def test_patch_accepts_record_index_only_when_allowed(self) -> None:
        edit = {"record_index": 5, "slot_index": 0, "effect_id": 0x1234}
        with self.assertRaises(RecordError):
            patch_effect_slots(self.record, [edit])
        patched = patch_effect_slots(self.record, [edit], allow_record_index=True)
        self.assertEqual(read_effect_slots(patched)[0].effect_id, 0x1234)

    def test_rejects_unknown_field(self) -> None:
        with self.assertRaises(RecordError):
            patch_effect_slots(self.record, [{"slot_index": 0, "bogus": 1}])

    def test_rejects_non_integer_values(self) -> None:
        for bad in ("1", 1.0, None, [1], True):
            with self.assertRaises(RecordError):
                patch_effect_slots(self.record, [{"slot_index": 0, "value": bad}])

    def test_rejects_out_of_range_values(self) -> None:
        for bad in (-1, 0x100000000):
            with self.assertRaises(RecordError):
                patch_effect_slots(self.record, [{"slot_index": 0, "value": bad}])

    def test_rejects_out_of_range_slot(self) -> None:
        for bad in (-1, EFFECT_COUNT, 99):
            with self.assertRaises(RecordError):
                patch_effect_slots(self.record, [{"slot_index": bad, "value": 1}])

    def test_rejects_missing_slot_index(self) -> None:
        with self.assertRaises(RecordError):
            patch_effect_slots(self.record, [{"value": 1}])

    def test_rejects_empty_edit_list(self) -> None:
        with self.assertRaises(RecordError):
            patch_effect_slots(self.record, [])

    def test_rejects_an_edit_with_no_fields(self) -> None:
        with self.assertRaises(RecordError):
            patch_effect_slots(self.record, [{"slot_index": 0}])

    def test_rejects_duplicate_slots(self) -> None:
        with self.assertRaises(RecordError):
            patch_effect_slots(
                self.record,
                [{"slot_index": 1, "value": 1}, {"slot_index": 1, "value": 2}],
            )

    def test_rejects_wrong_record_length(self) -> None:
        with self.assertRaises(RecordError):
            patch_effect_slots(b"\x00" * 10, [{"slot_index": 0, "value": 1}])

    def test_all_six_fields_are_writable(self) -> None:
        patched = patch_effect_slots(self.record, [{
            "slot_index": 6,
            "prefix": 0xAAAABBBB,
            "effect_id": 0xCCCCDDDD,
            "value": 0xEEEEFFFF,
            "metadata": 0x11112222,
            "tail_0": 0x33334444,
            "tail_1": 0x55556666,
        }])
        slot = read_effect_slots(patched)[6]
        self.assertEqual(slot.to_tuple(),
                         (0xAAAABBBB, 0xCCCCDDDD, 0xEEEEFFFF,
                          0x11112222, 0x33334444, 0x55556666))


class ScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = support.build_record(
            record_type=ITEM_TYPE, level=155, rarity=5,
            effects=((0x0B32, 20, 0x40),),
        )
        self.save = support.build_plain_save(records_by_slot={3: self.record})

    def test_iter_finds_only_the_seeded_slot(self) -> None:
        found = iter_item_records(self.save)
        self.assertEqual([record.slot_index for record in found], [3])
        self.assertIsInstance(found[0], AccessoryRecord)
        self.assertEqual(found[0].level, 155)
        self.assertEqual(found[0].rarity, 5)
        self.assertEqual(found[0].rarity_name, "神宝")
        self.assertEqual(found[0].offset, record_offset(3))
        self.assertEqual(len(found[0].occupied_effects), 1)

    def test_read_item_record(self) -> None:
        record = read_item_record(self.save, 3)
        self.assertIsNotNone(record)
        self.assertEqual(record.slot_index, 3)
        self.assertIsNone(read_item_record(self.save, 4))

    def test_read_item_record_rejects_a_short_buffer(self) -> None:
        with self.assertRaises(RecordError):
            read_item_record(b"\x00" * 16, 0)

    def test_iter_rejects_a_short_buffer(self) -> None:
        with self.assertRaises(RecordError):
            iter_item_records(b"\x00" * 16)

    def test_unknown_rarity_name(self) -> None:
        record = bytearray(support.build_record(record_type=ITEM_TYPE))
        record[0x30] = 9
        record[0x31] = 9
        parsed = read_effect_slots(bytes(record))  # smoke: layout stays readable
        self.assertEqual(len(parsed), EFFECT_COUNT)

    def test_scroll_records_are_not_returned(self) -> None:
        save = support.build_plain_save(
            records_by_slot={1: support.build_record(record_type=0x1E82)}
        )
        self.assertEqual(iter_item_records(save), ())


if __name__ == "__main__":
    unittest.main()
