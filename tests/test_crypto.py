"""Crypto-layer tests: golden key schedule, keystream, region coverage, XOR."""

from __future__ import annotations

import unittest

from nioh3_accessory_editor import crypto
from nioh3_accessory_editor.crypto import (
    HEADER_SIZE,
    ROOT_CRYPTO_BLOB,
    ROOT_CRYPTO_BLOB_SIZE,
    USER_SAVE_SIZE,
    USR_BODY_CRYPTO_SIZE,
    CryptoError,
    SaveCrypto,
    _increment_counter,
    _keystream,
    decrypt_user_save,
    encrypt_user_save,
)
from tests import support

GOLDEN_HEADER = {
    "key1": "CD1F3135A24E52F89251CC79FE48584B",
    "iv1": "1BDFDD5727CBCE873AEAC29EE05B2925",
    "key2": "CD958C0EC2F6296334FAEC535E7FB4D5",
    "iv2": "FD4C40A28ECE198B7701BC0C14B757BD",
}
GOLDEN_KEYSTREAM_PREFIX = "31d530acb6d67a46"


class LayoutInvariantTests(unittest.TestCase):
    def test_sizes_match_the_real_save(self) -> None:
        self.assertEqual(USER_SAVE_SIZE, 0x9001B0)
        self.assertEqual(HEADER_SIZE, 0x158)
        self.assertEqual(USR_BODY_CRYPTO_SIZE, 0x900050)
        self.assertEqual(USR_BODY_CRYPTO_SIZE % 16, 0)
        self.assertEqual(USER_SAVE_SIZE - HEADER_SIZE - USR_BODY_CRYPTO_SIZE, 8)

    def test_root_blob_length(self) -> None:
        self.assertEqual(len(ROOT_CRYPTO_BLOB), ROOT_CRYPTO_BLOB_SIZE)


class KeyScheduleTests(unittest.TestCase):
    def test_header_key_pair_matches_golden_values(self) -> None:
        state = SaveCrypto()
        state.key_setup(header=True)
        self.assertEqual(bytes(state._s_key_1).hex().upper(), GOLDEN_HEADER["key1"])
        self.assertEqual(bytes(state._s_iv_1).hex().upper(), GOLDEN_HEADER["iv1"])
        self.assertEqual(bytes(state._s_key_2).hex().upper(), GOLDEN_HEADER["key2"])
        self.assertEqual(bytes(state._s_iv_2).hex().upper(), GOLDEN_HEADER["iv2"])

    def test_key_pair_properties(self) -> None:
        state = SaveCrypto()
        state.key_setup(header=True)
        self.assertEqual(state.key_pair_1[0].hex().upper(), GOLDEN_HEADER["key1"])
        self.assertEqual(state.key_pair_2[0].hex().upper(), GOLDEN_HEADER["key2"])

    def test_body_key_setup_requires_a_clear_header(self) -> None:
        with self.assertRaises(CryptoError):
            SaveCrypto().key_setup(header=False)
        with self.assertRaises(CryptoError):
            SaveCrypto().key_setup(header=False, clear_header=b"\x00" * 16)

    def test_body_key_setup_uses_header_fields(self) -> None:
        clear_header = bytearray(HEADER_SIZE)
        for index in range(HEADER_SIZE):
            clear_header[index] = index & 0xFF
        state = SaveCrypto()
        state.key_setup(header=False, clear_header=bytes(clear_header))
        # Key material must actually derive from the header fields.
        other = bytearray(clear_header)
        other[0x49] ^= 0xFF
        changed = SaveCrypto()
        changed.key_setup(header=False, clear_header=bytes(other))
        self.assertNotEqual(bytes(state._s_key_1), bytes(changed._s_key_1))

    def test_rejects_wrong_blob_size(self) -> None:
        with self.assertRaises(CryptoError):
            SaveCrypto(b"\x00" * 147)
        with self.assertRaises(CryptoError):
            SaveCrypto(b"\x00" * 149)

    def test_backwards_compatible_alias(self) -> None:
        state = SaveCrypto()
        state._key_setup(header=True)
        self.assertEqual(bytes(state._s_key_1).hex().upper(), GOLDEN_HEADER["key1"])


class KeystreamTests(unittest.TestCase):
    def test_header_keystream_prefix_matches_the_exe(self) -> None:
        state = SaveCrypto()
        state.key_setup(header=True)
        blocks = HEADER_SIZE // 16 + 1
        stream = bytes(
            a ^ b
            for a, b in zip(
                _keystream(bytes(state._s_key_2), bytes(state._s_iv_2), blocks),
                _keystream(bytes(state._s_key_1), bytes(state._s_iv_1), blocks),
            )
        )
        self.assertEqual(stream[:8].hex(), GOLDEN_KEYSTREAM_PREFIX)

    def test_zero_blocks(self) -> None:
        self.assertEqual(_keystream(bytes(16), bytes(16), 0), b"")

    def test_counter_increment_and_carry(self) -> None:
        counter = bytearray(16)
        _increment_counter(counter)
        self.assertEqual(counter[15], 1)
        counter = bytearray(b"\xff" * 16)
        _increment_counter(counter)  # full carry wraps the whole counter
        self.assertEqual(bytes(counter), bytes(16))
        counter = bytearray(16)
        counter[15] = 0xFF
        _increment_counter(counter)
        self.assertEqual(counter[15], 0x00)
        self.assertEqual(counter[14], 0x01)


class RegionCoverageTests(unittest.TestCase):
    """The header pass must cover all 0x158 bytes, partial block included."""

    def test_header_pass_covers_every_header_byte(self) -> None:
        state = SaveCrypto()
        state.key_setup(header=True)
        target = bytearray(USER_SAVE_SIZE)
        state._xor_header(target)
        # Bytes beyond the header belong to the body pass and stay untouched.
        self.assertEqual(target[HEADER_SIZE:HEADER_SIZE + 64], bytes(64))
        # A partially covered final block would leave a zero tail here.
        self.assertNotEqual(target[HEADER_SIZE - 8:HEADER_SIZE], bytes(8))

    def test_header_pass_is_an_involution(self) -> None:
        state = SaveCrypto()
        state.key_setup(header=True)
        first = bytearray(USER_SAVE_SIZE)
        for index in range(USER_SAVE_SIZE):
            first[index] = (index * 31 + 7) & 0xFF
        original = bytes(first[:HEADER_SIZE])
        state._xor_header(first)
        self.assertNotEqual(bytes(first[:HEADER_SIZE]), original)
        state._xor_header(first)
        self.assertEqual(bytes(first[:HEADER_SIZE]), original)

    def test_body_pass_matches_a_naive_bytewise_xor(self) -> None:
        """Shrink the body region so the check stays fast but still exact."""
        block_count = 40
        length = block_count * 16
        original_size = crypto.USR_BODY_CRYPTO_SIZE
        crypto.USR_BODY_CRYPTO_SIZE = length
        try:
            state = SaveCrypto()
            state.key_setup(header=True)
            size = HEADER_SIZE + length
            target = bytearray((index * 17 + 5) & 0xFF for index in range(size))

            expected = bytearray(target)
            for key, iv in (
                (bytes(state._s_key_2), bytes(state._s_iv_2)),
                (bytes(state._s_key_1), bytes(state._s_iv_1)),
            ):
                stream = _keystream(key, iv, block_count)[:length]
                for offset in range(length):
                    expected[HEADER_SIZE + offset] ^= stream[offset]

            state._xor_body(target)
            self.assertEqual(bytes(target), bytes(expected))
        finally:
            crypto.USR_BODY_CRYPTO_SIZE = original_size

    def test_body_pass_is_an_involution(self) -> None:
        length = 32 * 16
        original_size = crypto.USR_BODY_CRYPTO_SIZE
        crypto.USR_BODY_CRYPTO_SIZE = length
        try:
            state = SaveCrypto()
            state.key_setup(header=True)
            target = bytearray(HEADER_SIZE + length)
            for index in range(HEADER_SIZE, len(target)):
                target[index] = (index * 13) & 0xFF
            original = bytes(target)
            state._xor_body(target)
            self.assertNotEqual(bytes(target), original)
            state._xor_body(target)
            self.assertEqual(bytes(target), original)
        finally:
            crypto.USR_BODY_CRYPTO_SIZE = original_size


class StateDetectionTests(unittest.TestCase):
    def test_is_encrypted(self) -> None:
        self.assertTrue(SaveCrypto.is_encrypted(b"\x00" * 16))
        self.assertFalse(SaveCrypto.is_encrypted(b"NIOH" + bytes(12)))
        self.assertFalse(SaveCrypto.is_encrypted(b"RNNUSR" + bytes(10)))

    def test_is_encrypted_rejects_tiny_input(self) -> None:
        with self.assertRaises(CryptoError):
            SaveCrypto.is_encrypted(b"abc")

    def test_transform_rejects_wrong_size(self) -> None:
        for size in (0, 0x158, USER_SAVE_SIZE - 1, USER_SAVE_SIZE + 1):
            with self.assertRaises(CryptoError):
                SaveCrypto().transform(bytes(size))

    def test_decrypt_rejects_non_usr_result(self) -> None:
        with self.assertRaises(CryptoError):
            decrypt_user_save(bytes(USER_SAVE_SIZE))

    def test_encrypt_requires_usr_magic(self) -> None:
        with self.assertRaises(CryptoError):
            encrypt_user_save(bytes(USER_SAVE_SIZE))


@unittest.skipUnless(
    support.PURE_CRYPTO_ENABLED,
    "set NIOH3_PURE_CRYPTO_TESTS=1 to run full-file pure-Python crypto (~70 s)",
)
class PurePythonFullFileTests(unittest.TestCase):
    """End-to-end transforms through the pure-Python backend only."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.plain = support.build_plain_save()

    def test_round_trip_is_lossless_in_the_crypto_region(self) -> None:
        encrypted = SaveCrypto().transform(self.plain)
        self.assertTrue(SaveCrypto.is_encrypted(encrypted))
        decrypted = SaveCrypto().transform(encrypted)
        self.assertEqual(decrypted, self.plain)

    def test_uncovered_tail_is_preserved(self) -> None:
        encrypted = SaveCrypto().transform(self.plain)
        covered = USER_SAVE_SIZE - 8
        self.assertEqual(encrypted[covered:], self.plain[covered:])

    def test_helper_functions(self) -> None:
        encrypted = encrypt_user_save(self.plain)
        self.assertEqual(decrypt_user_save(encrypted), self.plain)


if __name__ == "__main__":
    unittest.main()
