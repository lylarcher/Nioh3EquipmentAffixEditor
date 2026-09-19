"""User-checksum tests: determinism, patching, verification, validation."""

from __future__ import annotations

import struct
import unittest

from nioh3_accessory_editor import checksum as checksum_module
from nioh3_accessory_editor.checksum import (
    BLOCK_SIZE,
    USER_CHECKSUM_BODY_END,
    USER_CHECKSUM_BODY_SIZE,
    USER_CHECKSUM_BODY_START,
    USER_CHECKSUM_SEED_OFFSET,
    USER_CHECKSUM_VALUE_OFFSET,
    VALUES_PER_BLOCK,
    compute_user_checksum,
    patch_user_checksum,
    verify_user_checksum,
)
from nioh3_accessory_editor.crypto import USER_SAVE_SIZE
from tests import support


class LayoutInvariantTests(unittest.TestCase):
    def test_region_offsets(self) -> None:
        self.assertEqual(USER_CHECKSUM_BODY_START, 0x190)
        self.assertEqual(USER_CHECKSUM_BODY_END, 0x900190)
        self.assertEqual(USER_CHECKSUM_BODY_SIZE, 0x900000)
        self.assertEqual(USER_CHECKSUM_SEED_OFFSET, 0x900190)
        self.assertEqual(USER_CHECKSUM_VALUE_OFFSET, 0x900194)
        self.assertEqual(BLOCK_SIZE, 0x400)
        self.assertEqual(VALUES_PER_BLOCK, 128)

    def test_module_invariants_hold(self) -> None:
        self.assertEqual(checksum_module.USER_SAVE_SIZE, USER_SAVE_SIZE)
        self.assertEqual(USER_CHECKSUM_BODY_SIZE % BLOCK_SIZE, 0)
        self.assertEqual(USER_CHECKSUM_BODY_END, USER_CHECKSUM_SEED_OFFSET)


class ComputeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plain = support.build_plain_save()
        cls.body = cls.plain[USER_CHECKSUM_BODY_START:USER_CHECKSUM_BODY_END]
        cls.seed = struct.unpack_from("<I", cls.plain, USER_CHECKSUM_SEED_OFFSET)[0]

    def test_deterministic_and_32_bit(self) -> None:
        first = compute_user_checksum(self.body, self.seed)
        second = compute_user_checksum(self.body, self.seed)
        self.assertEqual(first, second)
        self.assertIsInstance(first, int)
        self.assertTrue(0 <= first <= 0xFFFFFFFF)

    def test_seed_changes_the_result(self) -> None:
        self.assertNotEqual(
            compute_user_checksum(self.body, self.seed),
            compute_user_checksum(self.body, (self.seed + 1) & 0xFFFFFFFF),
        )

    def test_body_change_changes_the_result(self) -> None:
        other = bytearray(self.body)
        other[0x10] ^= 0xFF
        self.assertNotEqual(
            compute_user_checksum(self.body, self.seed),
            compute_user_checksum(bytes(other), self.seed),
        )

    def test_matches_a_straightforward_reference_fold(self) -> None:
        """Compare the iter_unpack fast path against literal nested loops."""
        self.assertEqual(
            compute_user_checksum(self.body, self.seed),
            self._reference_compute(self.body, self.seed),
        )

    @staticmethod
    def _reference_compute(body: bytes, seed: int) -> int:
        total = 0
        for block_start in range(0, len(body), BLOCK_SIZE):
            block = body[block_start:block_start + BLOCK_SIZE]
            block_sum = 0
            for offset in range(0, len(block), 8):
                block_sum += struct.unpack_from("<q", block, offset)[0]
            total = ((total + block_sum) ^ seed) & 0xFFFFFFFFFFFFFFFF
        return ((total // 0xFFFFFFFF) + (total & 0xFFFFFFFF)) & 0xFFFFFFFF

    def test_rejects_wrong_body_size(self) -> None:
        for size in (0, 8, BLOCK_SIZE, len(self.body) + 1):
            with self.assertRaises(ValueError):
                compute_user_checksum(bytes(size), 0)

    def test_rejects_out_of_range_seed(self) -> None:
        with self.assertRaises(ValueError):
            compute_user_checksum(self.body, 0x100000000)
        with self.assertRaises(ValueError):
            compute_user_checksum(self.body, -1)


class PatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plain = support.build_plain_save()

    def test_patch_writes_a_verifiable_value(self) -> None:
        data = bytearray(self.plain)
        old, new = patch_user_checksum(data)
        self.assertTrue(verify_user_checksum(data))
        stored = struct.unpack_from("<I", data, USER_CHECKSUM_VALUE_OFFSET)[0]
        self.assertEqual(stored, new)
        self.assertEqual(old, struct.unpack_from(
            "<I", self.plain, USER_CHECKSUM_VALUE_OFFSET)[0])

    def test_patch_on_a_reseeded_save(self) -> None:
        data = bytearray(self.plain)
        struct.pack_into("<I", data, USER_CHECKSUM_SEED_OFFSET, 0xDEADBEEF)
        self.assertFalse(verify_user_checksum(data))
        patch_user_checksum(data)
        self.assertTrue(verify_user_checksum(data))

    def test_requires_a_bytearray(self) -> None:
        with self.assertRaises(TypeError):
            patch_user_checksum(bytes(self.plain))

    def test_rejects_wrong_size(self) -> None:
        with self.assertRaises(ValueError):
            patch_user_checksum(bytearray(16))

    def test_verify_rejects_wrong_size(self) -> None:
        with self.assertRaises(ValueError):
            verify_user_checksum(b"\x00" * 32)

    def test_verify_detects_a_corrupted_value(self) -> None:
        data = bytearray(self.plain)
        struct.pack_into("<I", data, USER_CHECKSUM_VALUE_OFFSET, 0x12345678)
        self.assertFalse(verify_user_checksum(bytes(data)))

    def test_verify_accepts_the_built_fixture(self) -> None:
        self.assertTrue(verify_user_checksum(self.plain))


if __name__ == "__main__":
    unittest.main()
