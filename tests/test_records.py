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
    describe_diagnosis,
    header_candidates,
    iter_item_records,
    layout_diagnosis,
    locate_layout,
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
        self.assertEqual(records.LEGACY_GROUP_OFFSET, SCROLL_GROUP_OFFSET)
        layout = support.legacy_layout()
        self.assertEqual(record_offset(0, layout=layout), SCROLL_GROUP_OFFSET)
        self.assertEqual(record_offset(399, layout=layout),
                         SCROLL_GROUP_OFFSET + 399 * 0xE8)

    def test_effect_slots_fit_inside_a_record(self) -> None:
        last = EFFECT_START + (EFFECT_COUNT - 1) * EFFECT_STRIDE + 0x18
        self.assertLessEqual(last, SCROLL_RECORD_SIZE)
        self.assertEqual(EFFECT_COUNT, 7)

    def test_item_type_is_not_a_scroll_type(self) -> None:
        self.assertNotIn(ITEM_TYPE, SCROLL_TYPES)

    def test_record_offset_bounds(self) -> None:
        layout = support.legacy_layout()
        for bad in (-1, SCROLL_SLOT_COUNT, 10_000):
            with self.assertRaises(RecordError):
                record_offset(bad, layout=layout)

    def test_a_layout_is_required_to_compute_an_offset(self) -> None:
        """Deriving an offset from the captured constant is what broke reads."""
        with self.assertRaises(TypeError):
            record_offset(0)  # type: ignore[call-arg]


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
        self.assertEqual(len(found), 1)
        location = found[0]
        self.assertIsInstance(location, AccessoryRecord)
        # Inside the captured array the slot number is the captured slot number.
        self.assertEqual(location.slot_index, 3)
        self.assertEqual(location.offset,
                         records.LEGACY_GROUP_OFFSET + 3 * 0xE8)
        self.assertEqual(location.level, 155)
        self.assertEqual(location.rarity, 5)
        self.assertEqual(location.rarity_name, "神宝")
        self.assertEqual(location.kind_name, "装备/饰品")
        self.assertFalse(location.is_scroll)
        self.assertEqual(len(location.occupied_effects), 1)

    def test_read_item_record(self) -> None:
        record = read_item_record(self.save, 3)
        self.assertIsNotNone(record)
        self.assertEqual(record.offset, records.LEGACY_GROUP_OFFSET + 3 * 0xE8)
        self.assertIsNone(read_item_record(self.save, 4))

    def test_a_short_buffer_has_no_layout_to_read(self) -> None:
        for buffer in (b"\x00" * 16, b""):
            with self.subTest(size=len(buffer)):
                with self.assertRaises(RecordError):
                    locate_layout(buffer)
                with self.assertRaises(RecordError):
                    iter_item_records(buffer)

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


class LayoutLocationTests(unittest.TestCase):
    """The array must be found in the save, never assumed from a constant."""

    def test_finds_the_array_in_a_captured_layout_save(self) -> None:
        save = support.build_plain_save(
            records_by_slot={5: support.build_record(record_type=ITEM_TYPE)}
        )
        layout = locate_layout(save)
        # Records inside the captured array keep that array's own slot numbers.
        self.assertEqual(layout.anchor, records.LEGACY_GROUP_OFFSET)
        self.assertTrue(layout.is_legacy_anchor)
        self.assertEqual(layout.slot_count, 400)
        self.assertEqual(layout.record_count, 1)
        self.assertEqual(layout.item_count, 1)
        self.assertEqual(layout.scroll_count, 0)
        self.assertIn("对齐", layout.anchor_note)
        self.assertIn(f"{records.LEGACY_GROUP_OFFSET:#08x}", layout.describe())
        self.assertEqual(iter_item_records(save)[0].slot_index, 5)

    def test_finds_an_array_that_moved(self) -> None:
        """A newer game build can move the array; the scan must still find it."""
        moved = records.LEGACY_GROUP_OFFSET + 0x1234
        save = support.build_plain_save(
            records_by_slot={
                2: support.build_record(record_type=0x4002, level=99),
                7: support.build_record(record_type=ITEM_TYPE, level=150),
            },
            anchor=moved,
        )
        layout = locate_layout(save)
        # The anchor is the first occupied slot, and slot numbers start there.
        self.assertEqual(layout.anchor, moved + 2 * 0xE8)
        self.assertFalse(layout.is_legacy_anchor)
        self.assertIn("不对齐", layout.anchor_note)
        self.assertEqual(layout.record_count, 2)

        found = iter_item_records(save)
        self.assertEqual([record.slot_index for record in found], [0, 5])
        self.assertEqual([record.offset for record in found],
                         [moved + 2 * 0xE8, moved + 7 * 0xE8])
        self.assertEqual([record.level for record in found], [99, 150])
        # Reading through the *old* constant would have found nothing here.
        self.assertEqual(
            [record.slot_index
             for record in iter_item_records(save, layout=support.legacy_layout())],
            [],
        )

    def test_separates_scrolls_from_items(self) -> None:
        save = support.build_plain_save(records_by_slot={
            0: support.build_record(record_type=0x1E82),
            1: support.build_record(record_type=0x516D),
            2: support.build_record(record_type=ITEM_TYPE),
        })
        layout = locate_layout(save)
        self.assertEqual(layout.record_count, 3)
        self.assertEqual(layout.scroll_count, 2)
        self.assertEqual(layout.item_count, 1)
        self.assertEqual(dict(layout.type_counts)[0x1E82], 1)

    def test_no_array_at_all_fails_closed_with_a_hint(self) -> None:
        save = support.build_plain_save(pattern_body=False)
        with self.assertRaises(RecordError) as caught:
            locate_layout(save)
        self.assertIn("scan", str(caught.exception))

    def test_random_body_bytes_are_not_mistaken_for_records(self) -> None:
        """The mirror predicate is strong: a patterned body must not match."""
        save = support.build_plain_save(pattern_body=True)
        self.assertEqual(header_candidates(save), ())
        with self.assertRaises(RecordError):
            locate_layout(save)

    def test_a_lone_matching_block_is_still_useable(self) -> None:
        """A save with a single record must locate it, not its own internals."""
        save = bytearray(support.build_plain_save(pattern_body=False))
        block = support.build_record(record_type=ITEM_TYPE)
        offset = 0x400000
        save[offset:offset + 0xE8] = block
        diagnosis = layout_diagnosis(bytes(save))
        layout = diagnosis["layout"]
        self.assertIsInstance(layout, dict)
        self.assertEqual(layout["anchor"], offset)
        self.assertEqual(layout["record_count"], 1)
        self.assertEqual(len(header_candidates(bytes(save))), 1)
        self.assertEqual(len(diagnosis["effect_preview"]), 1)
        self.assertEqual(diagnosis["notes"], [])

    def test_free_effect_slots_do_not_look_like_records(self) -> None:
        """0xFFFFFFFF free slots must not become nested fake records."""
        block = support.build_record(record_type=ITEM_TYPE)
        save = support.build_plain_save(records_by_slot={3: block})
        candidates = header_candidates(save)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0], records.LEGACY_GROUP_OFFSET + 3 * 0xE8)

    def test_diagnosis_reports_what_a_save_contains(self) -> None:
        save = support.build_plain_save(records_by_slot={
            3: support.build_record(record_type=ITEM_TYPE, level=120, rarity=4,
                                    effects=((0x0B32, 20, 0x40),)),
        })
        diagnosis = layout_diagnosis(save)
        self.assertEqual(diagnosis["candidate_count"], 1)
        self.assertEqual(diagnosis["level_range"], [120, 120])
        self.assertEqual(diagnosis["rarity_counts"], [{"rarity": 4, "count": 1}])
        preview = diagnosis["effect_preview"]
        self.assertEqual(len(preview), 1)
        self.assertEqual(preview[0]["type"], ITEM_TYPE)
        self.assertEqual(preview[0]["occupied_slots"], [0])
        self.assertEqual(len(preview[0]["effect_slots_hex"]), EFFECT_COUNT)

        text = "\n".join(describe_diagnosis(diagnosis))
        self.assertIn("记录表定位", text)
        self.assertIn(f"0x{ITEM_TYPE:04X}", text)
        self.assertIn("等级范围", text)

    def test_diagnosis_explains_a_save_without_records(self) -> None:
        diagnosis = layout_diagnosis(support.build_plain_save(pattern_body=False))
        self.assertIsNone(diagnosis["layout"])
        text = "\n".join(describe_diagnosis(diagnosis))
        self.assertIn("记录表定位: 失败", text)
        self.assertIn("scan", text)

    def test_iter_uses_a_located_layout_by_default(self) -> None:
        moved = records.LEGACY_GROUP_OFFSET - 0x800
        save = support.build_plain_save(
            records_by_slot={1: support.build_record(record_type=ITEM_TYPE)},
            anchor=moved,
        )
        found = iter_item_records(save)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].offset, moved + 0xE8)
        self.assertEqual(found[0].slot_index, 0)  # slot 0 = anchor on a moved array
        self.assertEqual(found[0].offset, read_item_record(save, 0).offset)


if __name__ == "__main__":
    unittest.main()
