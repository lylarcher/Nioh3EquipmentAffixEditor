"""Faithful Python port of the reference Nioh 3 custom AES-128-ECB (aes.c).

WARNING: this is a NON-STANDARD AES variant used only by the Nioh save crypto.
It deviates from FIPS-197 in three ways (all preserved here):

1. Custom S-box / inverse S-box tables ("custom Nioh rsbox" in aes.c).
2. KeyExpansion stores the first round key with each 4-byte word reversed
   (``RoundKey[i*4+0] = Key[i*4+3]`` ...).
3. The state matrix is stored column-major and ShiftRows rotates the *columns*
   at flat indexes [1,5,9,13] / [2,10] / [6,14] / [3,7,11,15] instead of rows.

The round function order (SubBytes, ShiftRows, MixColumns, AddRoundKey), the
MixColumns mathematics and the key schedule are otherwise the standard ones.

The reference implementation walks the 4x4 state byte by byte; this port uses
table-driven equivalents (``bytes.translate`` for SubBytes, precomputed index
permutations for ShiftRows, GF(2^8) lookup tables for MixColumns and integer
XOR for AddRoundKey) that produce **byte-identical** results.  The equivalence
is pinned by golden vectors captured from the reference executable — see
``tests/test_nioh_aes.py`` (equivalence to the literal C transcription) and
``tools/crosscheck_crypto_vs_exe.py`` (full-file byte comparison).

NOTE on ``decrypt_block``: the reference ``aes.c`` defines its ACTIVE ``rsbox``
as a byte-for-byte copy of the custom ``sbox`` (the real inverse table is
commented out).  Consequently the reference ``InvCipher`` is **not** the
inverse of ``Cipher``.  ``decrypt_block`` is kept as a literal port of that
reference routine for completeness, and is never used by the save pipeline:
file-level decryption XORs the same ECB keystream that encryption produces.
Do not use it expecting ``decrypt_block(encrypt_block(x)) == x``.
"""

from __future__ import annotations

__all__ = [
    "NiohAes",
    "RCON",
    "RSBOX",
    "SBOX",
    "aes_ecb_decrypt_block",
    "aes_ecb_encrypt_block",
    "BLOCK_SIZE",
    "INV_SHIFT_PERM",
    "KEY_SIZE",
    "SHIFT_PERM",
]

BLOCK_SIZE = 16
KEY_SIZE = 16
_NK = 4
_NR = 10
_NB = 4
_KEY_EXP_SIZE = 176

# Custom Nioh S-box (verbatim from aes.c).
SBOX = [
    0x1C, 0x2F, 0x03, 0x53, 0xA3, 0x01, 0x49, 0xDA, 0xA6, 0xCD, 0xE0, 0x8A, 0x19, 0xA7, 0x04, 0xD4,
    0x06, 0x1A, 0xDA, 0x49, 0x08, 0xE2, 0xF6, 0xB2, 0x9E, 0xE1, 0x22, 0x49, 0xCE, 0x7B, 0x7E, 0x5E,
    0xA0, 0x09, 0x2A, 0x63, 0xAF, 0x49, 0xCE, 0x70, 0x7B, 0x3C, 0x23, 0x80, 0xFA, 0x17, 0x47, 0xF2,
    0x62, 0x62, 0x6C, 0x59, 0x10, 0xCC, 0x29, 0x9C, 0xB5, 0x46, 0x58, 0xC7, 0x44, 0x13, 0xE7, 0x38,
    0xD5, 0xAF, 0x27, 0x83, 0xD4, 0xD5, 0xA0, 0x9E, 0xE3, 0x76, 0x3B, 0x85, 0x04, 0xD9, 0xD6, 0x98,
    0x60, 0x66, 0xD4, 0x78, 0x53, 0xEA, 0xCA, 0x0E, 0x8D, 0x56, 0x53, 0x44, 0xE2, 0xEF, 0xBD, 0xA9,
    0x9B, 0x10, 0x0A, 0xA1, 0x13, 0x93, 0xF0, 0x43, 0x0B, 0x7C, 0x39, 0x8A, 0x47, 0xDF, 0xD3, 0xC5,
    0x0E, 0x34, 0x31, 0xA6, 0xAE, 0x5A, 0xB8, 0xE7, 0xE6, 0x31, 0x43, 0xC0, 0xAA, 0x0F, 0xE0, 0x82,
    0x12, 0x4C, 0xD1, 0xDF, 0x8B, 0xA5, 0xAC, 0x70, 0xC5, 0x3D, 0x1B, 0x8E, 0x93, 0x17, 0x4D, 0x79,
    0x4E, 0xCE, 0x63, 0xC4, 0x33, 0x0E, 0x14, 0x57, 0xF0, 0xD8, 0x19, 0x5B, 0x9B, 0x61, 0x71, 0xF2,
    0x2B, 0x33, 0x7E, 0xFD, 0x2C, 0x0B, 0xB6, 0x23, 0x20, 0xB9, 0xD4, 0x91, 0x19, 0x94, 0x04, 0xA4,
    0x30, 0x13, 0x8A, 0xF1, 0xD0, 0x05, 0xEC, 0x5E, 0xAC, 0x4A, 0xD4, 0xD6, 0xA5, 0x17, 0x7F, 0xF9,
    0xE5, 0xF6, 0x00, 0x29, 0xD7, 0x93, 0x2D, 0x5E, 0x2C, 0xF1, 0x81, 0xA3, 0xB7, 0x63, 0x39, 0x57,
    0xC2, 0x33, 0x87, 0x2D, 0xA8, 0x3F, 0x02, 0xCC, 0x08, 0x67, 0x74, 0x60, 0xD8, 0xF0, 0xDA, 0x67,
    0x40, 0x64, 0x87, 0x55, 0xBB, 0x7F, 0xF2, 0x10, 0xC9, 0x03, 0x14, 0xB5, 0x80, 0x66, 0xCB, 0x91,
    0xF6, 0x1F, 0x79, 0x58, 0x88, 0xBC, 0x95, 0xC2, 0x06, 0x5F, 0xE9, 0x09, 0x32, 0xED, 0x9B, 0x85,
]

# Custom Nioh inverse S-box (verbatim from aes.c).
RSBOX = [
    0x1C, 0x2F, 0x03, 0x53, 0xA3, 0x01, 0x49, 0xDA, 0xA6, 0xCD, 0xE0, 0x8A, 0x19, 0xA7, 0x04, 0xD4,
    0x06, 0x1A, 0xDA, 0x49, 0x08, 0xE2, 0xF6, 0xB2, 0x9E, 0xE1, 0x22, 0x49, 0xCE, 0x7B, 0x7E, 0x5E,
    0xA0, 0x09, 0x2A, 0x63, 0xAF, 0x49, 0xCE, 0x70, 0x7B, 0x3C, 0x23, 0x80, 0xFA, 0x17, 0x47, 0xF2,
    0x62, 0x62, 0x6C, 0x59, 0x10, 0xCC, 0x29, 0x9C, 0xB5, 0x46, 0x58, 0xC7, 0x44, 0x13, 0xE7, 0x38,
    0xD5, 0xAF, 0x27, 0x83, 0xD4, 0xD5, 0xA0, 0x9E, 0xE3, 0x76, 0x3B, 0x85, 0x04, 0xD9, 0xD6, 0x98,
    0x60, 0x66, 0xD4, 0x78, 0x53, 0xEA, 0xCA, 0x0E, 0x8D, 0x56, 0x53, 0x44, 0xE2, 0xEF, 0xBD, 0xA9,
    0x9B, 0x10, 0x0A, 0xA1, 0x13, 0x93, 0xF0, 0x43, 0x0B, 0x7C, 0x39, 0x8A, 0x47, 0xDF, 0xD3, 0xC5,
    0x0E, 0x34, 0x31, 0xA6, 0xAE, 0x5A, 0xB8, 0xE7, 0xE6, 0x31, 0x43, 0xC0, 0xAA, 0x0F, 0xE0, 0x82,
    0x12, 0x4C, 0xD1, 0xDF, 0x8B, 0xA5, 0xAC, 0x70, 0xC5, 0x3D, 0x1B, 0x8E, 0x93, 0x17, 0x4D, 0x79,
    0x4E, 0xCE, 0x63, 0xC4, 0x33, 0x0E, 0x14, 0x57, 0xF0, 0xD8, 0x19, 0x5B, 0x9B, 0x61, 0x71, 0xF2,
    0x2B, 0x33, 0x7E, 0xFD, 0x2C, 0x0B, 0xB6, 0x23, 0x20, 0xB9, 0xD4, 0x91, 0x19, 0x94, 0x04, 0xA4,
    0x30, 0x13, 0x8A, 0xF1, 0xD0, 0x05, 0xEC, 0x5E, 0xAC, 0x4A, 0xD4, 0xD6, 0xA5, 0x17, 0x7F, 0xF9,
    0xE5, 0xF6, 0x00, 0x29, 0xD7, 0x93, 0x2D, 0x5E, 0x2C, 0xF1, 0x81, 0xA3, 0xB7, 0x63, 0x39, 0x57,
    0xC2, 0x33, 0x87, 0x2D, 0xA8, 0x3F, 0x02, 0xCC, 0x08, 0x67, 0x74, 0x60, 0xD8, 0xF0, 0xDA, 0x67,
    0x40, 0x64, 0x87, 0x55, 0xBB, 0x7F, 0xF2, 0x10, 0xC9, 0x03, 0x14, 0xB5, 0x80, 0x66, 0xCB, 0x91,
    0xF6, 0x1F, 0x79, 0x58, 0x88, 0xBC, 0x95, 0xC2, 0x06, 0x5F, 0xE9, 0x09, 0x32, 0xED, 0x9B, 0x85,
]

RCON = [0x8D, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]

_SBOX_BYTES = bytes(SBOX)
_RSBOX_BYTES = bytes(RSBOX)

# ShiftRows / InvShiftRows as flat-index permutations of the 16 state bytes.
# Derived from aes.c: the cipher rotates the columns at flat indexes
# [1,5,9,13] (left by one), swaps [2,10] and [6,14], and rotates
# [3,7,11,15] (right by one).  ``INV_SHIFT_PERM`` is its exact inverse.
SHIFT_PERM = (0, 5, 10, 15, 4, 9, 14, 3, 8, 13, 2, 7, 12, 1, 6, 11)
INV_SHIFT_PERM = (0, 13, 10, 7, 4, 1, 14, 11, 8, 5, 2, 15, 12, 9, 6, 3)


def _xtime(value: int) -> int:
    return ((value << 1) ^ (0x1B if value & 0x80 else 0x00)) & 0xFF


def _multiply(left: int, right: int) -> int:
    """GF(2^8) multiplication, mirroring the reference ``Multiply`` macro."""
    return (
        ((right & 1) * left)
        ^ ((right >> 1 & 1) * _xtime(left))
        ^ ((right >> 2 & 1) * _xtime(_xtime(left)))
        ^ ((right >> 3 & 1) * _xtime(_xtime(_xtime(left))))
        ^ ((right >> 4 & 1) * _xtime(_xtime(_xtime(_xtime(left)))))
    ) & 0xFF


_MUL2 = bytes(_xtime(i) for i in range(256))
_MUL9 = bytes(_multiply(i, 0x09) for i in range(256))
_MUL11 = bytes(_multiply(i, 0x0B) for i in range(256))
_MUL13 = bytes(_multiply(i, 0x0D) for i in range(256))
_MUL14 = bytes(_multiply(i, 0x0E) for i in range(256))


def _mix_columns(state: bytes) -> bytes:
    out = bytearray(BLOCK_SIZE)
    for base in (0, 4, 8, 12):
        t0, t1, t2, t3 = state[base], state[base + 1], state[base + 2], state[base + 3]
        tmp = t0 ^ t1 ^ t2 ^ t3
        out[base] = t0 ^ _MUL2[t0 ^ t1] ^ tmp
        out[base + 1] = t1 ^ _MUL2[t1 ^ t2] ^ tmp
        out[base + 2] = t2 ^ _MUL2[t2 ^ t3] ^ tmp
        out[base + 3] = t3 ^ _MUL2[t3 ^ t0] ^ tmp
    return bytes(out)


def _inv_mix_columns(state: bytes) -> bytes:
    out = bytearray(BLOCK_SIZE)
    for base in (0, 4, 8, 12):
        a, b, c, d = state[base], state[base + 1], state[base + 2], state[base + 3]
        out[base] = _MUL14[a] ^ _MUL11[b] ^ _MUL13[c] ^ _MUL9[d]
        out[base + 1] = _MUL9[a] ^ _MUL14[b] ^ _MUL11[c] ^ _MUL13[d]
        out[base + 2] = _MUL13[a] ^ _MUL9[b] ^ _MUL14[c] ^ _MUL11[d]
        out[base + 3] = _MUL11[a] ^ _MUL13[b] ^ _MUL9[c] ^ _MUL14[d]
    return bytes(out)


class NiohAes:
    """Custom Nioh AES-128 block cipher (matches reference aes.c).

    Instances are reusable: key expansion runs once at construction, and
    ``encrypt_block``/``decrypt_block`` may be called repeatedly.
    """

    __slots__ = ("_round_key", "_round_key_words")

    def __init__(self, key: bytes) -> None:
        if not isinstance(key, (bytes, bytearray, memoryview)):
            raise TypeError("key must be bytes-like")
        if len(key) != BLOCK_SIZE:
            raise ValueError("custom Nioh AES requires a 16-byte key")
        self._round_key = self._key_expansion(bytes(key))
        self._round_key_words = [
            int.from_bytes(bytes(self._round_key[index:index + BLOCK_SIZE]), "big")
            for index in range(0, _KEY_EXP_SIZE, BLOCK_SIZE)
        ]

    # -- key schedule ----------------------------------------------------

    @staticmethod
    def _key_expansion(key: bytes) -> list[int]:
        """Expand the key exactly like aes.c (first round key word-reversed)."""
        rk = [0] * _KEY_EXP_SIZE
        for index in range(_NK):
            rk[index * 4 + 0] = key[index * 4 + 3]
            rk[index * 4 + 1] = key[index * 4 + 2]
            rk[index * 4 + 2] = key[index * 4 + 1]
            rk[index * 4 + 3] = key[index * 4 + 0]
        index = _NK
        while index < _NB * (_NR + 1):
            temp = [rk[(index - 1) * 4 + 0], rk[(index - 1) * 4 + 1],
                    rk[(index - 1) * 4 + 2], rk[(index - 1) * 4 + 3]]
            if index % _NK == 0:
                temp = temp[1:] + temp[:1]              # RotWord
                temp = [SBOX[value] for value in temp]  # SubWord
                temp[0] ^= RCON[index // _NK]
            for offset in range(4):
                rk[index * 4 + offset] = rk[(index - _NK) * 4 + offset] ^ temp[offset]
            index += 1
        return rk

    # -- round primitives ------------------------------------------------

    def _add_round_key(self, state: bytes, round_index: int) -> bytes:
        value = int.from_bytes(state, "big") ^ self._round_key_words[round_index]
        return value.to_bytes(BLOCK_SIZE, "big")

    @staticmethod
    def _shift_rows(state: bytes) -> bytes:
        return bytes(map(state.__getitem__, SHIFT_PERM))

    @staticmethod
    def _inv_shift_rows(state: bytes) -> bytes:
        return bytes(map(state.__getitem__, INV_SHIFT_PERM))

    # -- block operations ------------------------------------------------

    def encrypt_block(self, block: bytes | bytearray | memoryview) -> bytes:
        """Encrypt one 16-byte block (matches ``AES_ECB_encrypt``)."""
        if len(block) != BLOCK_SIZE:
            raise ValueError("block must be 16 bytes")
        state = bytes(block)
        state = self._add_round_key(state, 0)
        for round_index in range(1, _NR):
            state = state.translate(_SBOX_BYTES)
            state = self._shift_rows(state)
            state = _mix_columns(state)
            state = self._add_round_key(state, round_index)
        state = state.translate(_SBOX_BYTES)
        state = self._shift_rows(state)
        return self._add_round_key(state, _NR)

    def decrypt_block(self, block: bytes | bytearray | memoryview) -> bytes:
        """Decrypt one 16-byte block (literal port of ``AES_ECB_decrypt``).

        The reference ``rsbox`` equals its ``sbox``, so this is NOT the inverse
        of :meth:`encrypt_block`; it exists only to mirror ``aes.c`` exactly and
        is unused by the save pipeline.
        """
        if len(block) != BLOCK_SIZE:
            raise ValueError("block must be 16 bytes")
        state = bytes(block)
        state = self._add_round_key(state, _NR)
        for round_index in range(_NR - 1, 0, -1):
            state = self._inv_shift_rows(state)
            state = state.translate(_RSBOX_BYTES)
            state = self._add_round_key(state, round_index)
            state = _inv_mix_columns(state)
        state = self._inv_shift_rows(state)
        state = state.translate(_RSBOX_BYTES)
        return self._add_round_key(state, 0)


def aes_ecb_encrypt_block(block: bytes, key: bytes) -> bytes:
    """Encrypt a single 16-byte block with the custom Nioh AES."""
    return NiohAes(key).encrypt_block(block)


def aes_ecb_decrypt_block(block: bytes, key: bytes) -> bytes:
    """Decrypt a single 16-byte block with the custom Nioh AES."""
    return NiohAes(key).decrypt_block(block)
