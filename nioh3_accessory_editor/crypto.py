"""Nioh 3 save-file AES encryption/decryption (pure Python).

This is a faithful port of the reference ``CryptoState`` (C++,
``third_party/nioh_savefile_decrypt``) algorithm.  It depends on a NON-STANDARD
custom AES variant (``nioh_aes``) -- the reference tool swaps in custom S-box
tables and a reversed-word key schedule, so standard AES (pycryptodome,
Windows BCrypt, hashlib) will NOT produce matching output.  The reference exe's
debug output was used as a golden oracle to validate this port.

Algorithm summary (for a USR save, size 0x9001B0):

* The file is encrypted as a counter/CFB-style stream: each 16-byte block is
  XORed with ``AES_ECB(IV_counter, key)`` and the IV counter is incremented.
* Two independent passes are applied: a header pass over the first 0x158
  bytes and a body pass over the remaining 0x900058 bytes.
* Two key pairs are derived from a fixed root blob (header pair) and from
  fields stored in the *clear* header at +0x49..+0x89 (body pair).
* Because the scheme is a symmetric XOR stream, "encryption" runs the same
  keystream generator as "decryption"; only the pass order differs.
* The last 8 bytes of the body lie outside the crypto scope
  (``USR_BODY_SIZE // 16 * 16`` covers everything but them); this port keeps
  the input's trailing bytes so the transform stays lossless, and the file
  writer restores the original on-disk tail.
"""

from __future__ import annotations

from .nioh_aes import BLOCK_SIZE, NiohAes

__all__ = [
    "BLOCK_SIZE",
    "HEADER_SIZE",
    "ROOT_CRYPTO_BLOB",
    "ROOT_CRYPTO_BLOB_SIZE",
    "SYS_BODY_SIZE",
    "USER_SAVE_SIZE",
    "USR_BODY_SIZE",
    "CryptoError",
    "SaveCrypto",
    "decrypt_user_save",
    "encrypt_user_save",
]

ROOT_CRYPTO_BLOB_SIZE = 148
HEADER_SIZE = 0x158
USR_BODY_SIZE = 0x900058
SYS_BODY_SIZE = 0x39620

#: Bytes of the body actually covered by the crypto stream (block aligned).
USR_BODY_CRYPTO_SIZE = USR_BODY_SIZE // BLOCK_SIZE * BLOCK_SIZE  # 0x900050

USER_SAVE_SIZE = HEADER_SIZE + USR_BODY_SIZE  # 0x9001B0

# Fixed 148-byte root crypto blob from the reference CryptoState.cpp.
ROOT_CRYPTO_BLOB = bytes(
    [
        0x54, 0x19, 0x31, 0x3E, 0xF4, 0x6B, 0xE4, 0x24, 0xCD, 0xA7, 0x96, 0x6F,
        0xAB, 0xF0, 0x69, 0xCA,
        0x00, 0x00, 0x80, 0xBF, 0xEC, 0x3B, 0x9D, 0xA1, 0x46, 0x0C, 0xDD, 0x33,
        0xF3, 0xD2, 0x58, 0xE0,
        0xC0, 0x9F, 0xC7, 0xD4, 0xF6, 0xEF, 0xDC, 0x70, 0x92, 0x6F, 0x52, 0xD8,
        0xF1, 0xBD, 0x54, 0x36,
        0xA2, 0xCB, 0xAC, 0xA3, 0x99, 0xFC, 0xC8, 0xD2, 0xA9, 0x61, 0x72, 0x6B,
        0xD7, 0x8D, 0x15, 0xB8,
        0x80, 0xAC, 0xA0, 0xB5, 0x9A, 0xA0, 0xEE, 0x1E, 0x8B, 0xF5, 0xD9, 0xDA,
        0x2C, 0x92, 0xAE, 0xB4,
        0x9D, 0x92, 0xE0, 0x79, 0xAA, 0x76, 0x55, 0x31, 0xBC, 0xE3, 0x02, 0x00,
        0x7A, 0xB9, 0x53, 0x7F,
        0xE2, 0x60, 0xF5, 0x26, 0x2B, 0x1E, 0x7D, 0xA7, 0x5D, 0xD1, 0xBD, 0x84,
        0x23, 0x3B, 0xE4, 0x32,
        0x33, 0x03, 0xA4, 0x81, 0x84, 0x98, 0x97, 0xAB, 0x63, 0x7A, 0x82, 0x25,
        0x39, 0x9F, 0xC0, 0x73,
        0x49, 0x63, 0x94, 0xFD, 0xD8, 0xDE, 0xA8, 0xC8, 0xB0, 0x36, 0x52, 0xCD,
        0x07, 0xD6, 0xA2, 0x0A,
        0xF2, 0x00, 0x8C, 0x62,
    ]
)


class CryptoError(RuntimeError):
    """Raised for any crypto failure."""


def _flip_32bit_endianness(array: bytearray) -> None:
    """Reverse the byte order of every 4-byte group in place."""
    for base in range(0, len(array) - len(array) % 4, 4):
        array[base:base + 4] = array[base:base + 4][::-1]


def _increment_counter(counter: bytearray, index: int = BLOCK_SIZE - 1) -> None:
    """Increment the big-endian IV counter starting at its last byte.

    Iterative on purpose: a carry run through the whole block must not recurse
    hundreds of thousands of times inside the keystream loop.
    """
    while index >= 0:
        if counter[index] == 0xFF:
            counter[index] = 0x00
            index -= 1
            continue
        counter[index] += 1
        return


def _keystream_blocks(aes: NiohAes, iv_start: bytes, block_count: int) -> bytearray:
    """Generate ``block_count`` counter-mode keystream blocks."""
    if block_count < 0:
        raise CryptoError("block count must not be negative")
    stream = bytearray(block_count * BLOCK_SIZE)
    counter = bytearray(iv_start)
    position = 0
    for _ in range(block_count):
        stream[position:position + BLOCK_SIZE] = aes.encrypt_block(counter)
        position += BLOCK_SIZE
        _increment_counter(counter)
    return stream


def _keystream(key: bytes, iv_start: bytes, block_count: int) -> bytes:
    """Produce the custom-AES keystream for ``block_count`` counter IVs."""
    return bytes(_keystream_blocks(NiohAes(key), iv_start, block_count))


def _xor_into(target: bytearray, offset: int, key: bytes, iv_start: bytes,
              length: int) -> None:
    """XOR the keystream for ``length`` bytes into ``target`` at ``offset``.

    The reference generates ``ceil(length / 16)`` counter blocks and XORs only
    ``length`` bytes, so a partial final block is still covered.  Using integer
    division here silently leaves the tail of a region unencrypted.
    """
    if length <= 0:
        return
    block_count = (length + BLOCK_SIZE - 1) // BLOCK_SIZE
    stream = _keystream_blocks(NiohAes(key), iv_start, block_count)[:length]
    # Big-integer XOR keeps this at C speed instead of a per-byte Python loop.
    chunk = int.from_bytes(bytes(target[offset:offset + length]), "big") ^ \
        int.from_bytes(bytes(stream), "big")
    target[offset:offset + length] = chunk.to_bytes(length, "big")


class SaveCrypto:
    """Port of the reference ``CryptoState`` USR save encrypt/decrypt."""

    __slots__ = ("_blob", "_s_key_1", "_s_iv_1", "_s_key_2", "_s_iv_2")

    def __init__(self, blob: bytes = ROOT_CRYPTO_BLOB) -> None:
        if len(blob) != ROOT_CRYPTO_BLOB_SIZE:
            raise CryptoError(
                f"root crypto blob must be {ROOT_CRYPTO_BLOB_SIZE} bytes, "
                f"got {len(blob)}"
            )
        self._blob = bytes(blob)
        self._s_key_1 = bytearray(BLOCK_SIZE)
        self._s_iv_1 = bytearray(BLOCK_SIZE)
        self._s_key_2 = bytearray(BLOCK_SIZE)
        self._s_iv_2 = bytearray(BLOCK_SIZE)

    # -- key derivation --------------------------------------------------

    def _deconstruct_root_key_pair(self, header: bool) -> tuple[bytes, bytes]:
        blob = self._blob
        buf = bytearray(32)
        if header:
            for i in range(4):
                c1 = blob[8 + i]
                c2 = blob[0 + i]
                c3 = blob[12 + i]
                c4 = blob[4 + i]
                buf[0 + i] = c1 ^ blob[20 + i]
                buf[4 + i] = c2 ^ blob[24 + i]
                buf[8 + i] = c3 ^ blob[28 + i]
                buf[12 + i] = c4 ^ blob[32 + i]
                buf[16 + i] = c1 ^ blob[36 + i]
                buf[20 + i] = c2 ^ blob[40 + i]
                buf[24 + i] = c3 ^ blob[44 + i]
                buf[28 + i] = c4 ^ blob[48 + i]
            root_key = bytes(buf[0:16])
            root_iv = bytes(buf[16:32])
        else:
            for i in range(4):
                c1 = blob[0 + i]
                c2 = blob[8 + i]
                c3 = blob[12 + i]
                c4 = blob[56 + i]
                c5 = blob[52 + i]
                c6 = blob[60 + i]
                c7 = blob[4 + i]
                buf[0 + i] = c2 ^ blob[68 + i]
                buf[4 + i] = c1 ^ blob[72 + i]
                buf[8 + i] = c3 ^ blob[76 + i]
                buf[12 + i] = c7 ^ blob[80 + i]
                buf[16 + i] = c2 ^ c5
                buf[20 + i] = c1 ^ c4
                buf[24 + i] = c3 ^ c6
                buf[28 + i] = c7 ^ blob[64 + i]
            root_iv = bytes(buf[0:16])
            root_key = bytes(buf[16:32])
        root_key_array = bytearray(root_key)
        _flip_32bit_endianness(root_key_array)
        return bytes(root_key_array), root_iv

    def key_setup(self, header: bool, clear_header: bytes | None = None) -> None:
        """Derive the two key/IV pairs (public for tests and diagnostics)."""
        if header:
            s_key_1 = self._blob[84:100]
            s_iv_1 = self._blob[116:132]
            s_key_2 = self._blob[100:116]
            s_iv_2 = self._blob[132:148]
        else:
            if clear_header is None or len(clear_header) < 0x89:
                raise CryptoError("body key setup requires a clear header")
            s_key_1 = clear_header[0x49:0x49 + 16]
            s_iv_1 = clear_header[0x59:0x59 + 16]
            s_key_2 = clear_header[0x69:0x69 + 16]
            s_iv_2 = clear_header[0x79:0x79 + 16]

        root_key, root_iv = self._deconstruct_root_key_pair(header)
        root_aes = NiohAes(root_key)

        out = root_aes.encrypt_block(root_iv)
        key1 = bytearray(s_key_1)
        for index in range(BLOCK_SIZE):
            key1[index] ^= out[index]
        _flip_32bit_endianness(key1)

        out = root_aes.encrypt_block(root_iv)
        iv1 = bytearray(s_iv_1)
        for index in range(BLOCK_SIZE):
            iv1[index] ^= out[index]

        sub_aes = NiohAes(bytes(key1))
        out = sub_aes.encrypt_block(bytes(iv1))
        key2 = bytearray(s_key_2)
        for index in range(BLOCK_SIZE):
            key2[index] ^= out[index]
        _flip_32bit_endianness(key2)

        out = sub_aes.encrypt_block(bytes(iv1))
        iv2 = bytearray(s_iv_2)
        for index in range(BLOCK_SIZE):
            iv2[index] ^= out[index]

        self._s_key_1 = key1
        self._s_iv_1 = iv1
        self._s_key_2 = key2
        self._s_iv_2 = iv2

    # Backwards-compatible alias for earlier internal callers.
    _key_setup = key_setup

    @property
    def key_pair_1(self) -> tuple[bytes, bytes]:
        return bytes(self._s_key_1), bytes(self._s_iv_1)

    @property
    def key_pair_2(self) -> tuple[bytes, bytes]:
        return bytes(self._s_key_2), bytes(self._s_iv_2)

    # -- passes ----------------------------------------------------------

    def _xor_header(self, target: bytearray) -> None:
        """XOR both header passes in place (target already holds the input)."""
        _xor_into(target, 0, bytes(self._s_key_2), bytes(self._s_iv_2), HEADER_SIZE)
        _xor_into(target, 0, bytes(self._s_key_1), bytes(self._s_iv_1), HEADER_SIZE)

    def _xor_body(self, target: bytearray) -> None:
        """XOR both body passes in place over the crypto-covered region."""
        offset = HEADER_SIZE
        _xor_into(target, offset, bytes(self._s_key_2), bytes(self._s_iv_2),
                  USR_BODY_CRYPTO_SIZE)
        _xor_into(target, offset, bytes(self._s_key_1), bytes(self._s_iv_1),
                  USR_BODY_CRYPTO_SIZE)

    # -- public API ------------------------------------------------------

    @staticmethod
    def is_encrypted(data: bytes) -> bool:
        """Return whether ``data`` still looks encrypted."""
        if len(data) < 6:
            raise CryptoError("save data is too small to identify")
        return not (data.startswith(b"NIOH") or data.startswith(b"RNNUSR"))

    def transform(self, data: bytes) -> bytes:
        """Encrypt or decrypt based on the input's current state.

        The transformation is a symmetric XOR stream over
        ``[0, HEADER_SIZE + USR_BODY_CRYPTO_SIZE)``; the final 8 body bytes are
        copied through unchanged so the operation stays lossless.
        """
        if len(data) != USER_SAVE_SIZE:
            raise CryptoError(
                f"Expected a {USER_SAVE_SIZE:#x}-byte Nioh 3 USR save, "
                f"got {len(data):#x}"
            )
        clear = bytearray(data)  # copy; the trailing 8 bytes stay untouched

        if self.is_encrypted(data):
            # Decrypt: header keys come from the fixed blob.
            self.key_setup(header=True)
            self._xor_header(clear)
            # Body keys are stored in the now-clear header.
            self.key_setup(header=False, clear_header=bytes(clear[0:HEADER_SIZE]))
            self._xor_body(clear)
            return bytes(clear)

        # Encrypt: body keys derive from the plaintext header being written.
        self.key_setup(header=False, clear_header=data[0:HEADER_SIZE])
        self._xor_body(clear)
        self.key_setup(header=True)
        self._xor_header(clear)
        return bytes(clear)


def decrypt_user_save(data: bytes) -> bytes:
    """Return the decrypted RNNUSR save bytes (raises if impossible)."""
    out = SaveCrypto().transform(data)
    if not out.startswith(b"RNNUSR"):
        raise CryptoError("decrypted save does not start with RNNUSR")
    return out


def encrypt_user_save(data: bytes) -> bytes:
    """Encrypt a decrypted RNNUSR save (raises on invalid input)."""
    if not data.startswith(b"RNNUSR"):
        raise CryptoError("encrypt_user_save requires a decrypted RNNUSR save")
    return SaveCrypto().transform(data)
