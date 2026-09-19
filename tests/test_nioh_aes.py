"""Custom-AES tests: golden vectors, permutation invariants, C equivalence."""

from __future__ import annotations

import collections
import random
import unittest

from nioh3_accessory_editor.nioh_aes import (
    BLOCK_SIZE,
    INV_SHIFT_PERM,
    KEY_SIZE,
    RCON,
    RSBOX,
    SBOX,
    SHIFT_PERM,
    NiohAes,
    aes_ecb_decrypt_block,
    aes_ecb_encrypt_block,
)
from tests import reference_aes, support


class TableIntegrityTests(unittest.TestCase):
    """The custom tables must equal the reference's, verbatim."""

    def test_tables_have_256_entries(self) -> None:
        self.assertEqual(len(SBOX), 256)
        self.assertEqual(len(RSBOX), 256)

    @unittest.skipUnless(support.HAVE_REFERENCE_SOURCE,
                         "sibling reference checkout is unavailable")
    def test_tables_match_the_reference_source(self) -> None:
        expected_sbox, expected_rsbox = support.reference_sbox_tables()
        self.assertEqual(list(SBOX), expected_sbox)
        self.assertEqual(list(RSBOX), expected_rsbox)

    def test_custom_sbox_is_not_a_permutation(self) -> None:
        """Documents why this cipher cannot be inverted.

        The custom substitution has duplicate and missing values, so SubBytes
        destroys information; the reference's decrypt branch therefore cannot
        undo encryption (its ``rsbox`` is the same table as well).
        """
        counts = collections.Counter(SBOX)
        distinct_values = len(counts)
        repeated_values = sum(1 for value in counts.values() if value > 1)
        never_produced = [value for value in range(256) if value not in counts]
        self.assertEqual(distinct_values, 164)
        self.assertEqual(repeated_values, 69)
        self.assertEqual(len(never_produced), 92)
        self.assertLess(distinct_values, 256)

    def test_reference_rsbox_is_a_copy_of_sbox(self) -> None:
        """Documents the reference quirk this port must preserve."""
        self.assertEqual(tuple(RSBOX), tuple(SBOX))

    def test_shift_permutations_are_inverses(self) -> None:
        self.assertEqual(sorted(SHIFT_PERM), list(range(16)))
        self.assertEqual(sorted(INV_SHIFT_PERM), list(range(16)))
        for index in range(16):
            self.assertEqual(INV_SHIFT_PERM[SHIFT_PERM[index]], index)
            self.assertEqual(SHIFT_PERM[INV_SHIFT_PERM[index]], index)

    def test_rcon_length(self) -> None:
        self.assertEqual(len(RCON), 11)
        self.assertEqual(RCON[1], 0x01)


class CipherEquivalenceTests(unittest.TestCase):
    """The optimized cipher must equal the literal aes.c transcription."""

    def test_matches_reference_cipher_on_random_blocks(self) -> None:
        rng = random.Random(0xC0FFEE)
        for trial in range(64):
            key = bytes(rng.randrange(256) for _ in range(KEY_SIZE))
            block = bytes(rng.randrange(256) for _ in range(BLOCK_SIZE))
            aes = NiohAes(key)
            expected = reference_aes.cipher(reference_aes.key_expansion(key), block)
            self.assertEqual(aes.encrypt_block(block), expected, f"trial {trial}")

    def test_matches_reference_inv_cipher_on_random_blocks(self) -> None:
        rng = random.Random(0xBEEF)
        for trial in range(32):
            key = bytes(rng.randrange(256) for _ in range(KEY_SIZE))
            block = bytes(rng.randrange(256) for _ in range(BLOCK_SIZE))
            aes = NiohAes(key)
            expected = reference_aes.inv_cipher(reference_aes.key_expansion(key), block)
            self.assertEqual(aes.decrypt_block(block), expected, f"trial {trial}")

    def test_reference_round_trip_is_not_an_identity(self) -> None:
        """The custom cipher's decrypt branch is intentionally not an inverse."""
        key = bytes(range(16))
        block = bytes(range(16, 32))
        aes = NiohAes(key)
        self.assertNotEqual(aes.decrypt_block(aes.encrypt_block(block)), block)

    def test_module_level_helpers(self) -> None:
        """Note the ``(block, key)`` argument order, matching aes.h."""
        key = bytes(range(16))
        block = bytes(range(16, 32))
        self.assertEqual(
            aes_ecb_encrypt_block(block, key), NiohAes(key).encrypt_block(block)
        )
        self.assertEqual(
            aes_ecb_decrypt_block(block, key), NiohAes(key).decrypt_block(block)
        )

    def test_encryption_is_deterministic(self) -> None:
        key = bytes.fromhex("CD1F3135A24E52F89251CC79FE48584B")
        iv = bytes.fromhex("1BDFDD5727CBCE873AEAC29EE05B2925")
        self.assertEqual(NiohAes(key).encrypt_block(iv), NiohAes(key).encrypt_block(iv))


class ValidationTests(unittest.TestCase):
    def test_rejects_wrong_key_size(self) -> None:
        for bad in (b"", b"\x00" * 15, b"\x00" * 17, b"\x00" * 32):
            with self.assertRaises(ValueError):
                NiohAes(bad)

    def test_rejects_wrong_block_size(self) -> None:
        aes = NiohAes(bytes(16))
        for bad in (b"", b"\x00" * 15, b"\x00" * 17, b"\x00" * 32):
            with self.assertRaises(ValueError):
                aes.encrypt_block(bad)
            with self.assertRaises(ValueError):
                aes.decrypt_block(bad)

    def test_accepts_bytearray_and_memoryview(self) -> None:
        aes = NiohAes(bytes(16))
        block = bytes(range(16))
        expected = aes.encrypt_block(block)
        self.assertEqual(aes.encrypt_block(bytearray(block)), expected)
        self.assertEqual(aes.encrypt_block(memoryview(block)), expected)

    def test_output_is_bytes_of_block_size(self) -> None:
        aes = NiohAes(bytes(16))
        result = aes.encrypt_block(bytes(16))
        self.assertIsInstance(result, bytes)
        self.assertEqual(len(result), BLOCK_SIZE)


if __name__ == "__main__":
    unittest.main()
