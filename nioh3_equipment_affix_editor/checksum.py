"""Nioh 3 user-save checksum computation and patching.

Port of ``emaki_exchange.py`` from Nioh3-Scroll-Generator (verified line by
line against that source): the checksum covers the body region
``[0x190, 0x900190)`` (0x900000 bytes) as 0x400-byte blocks of little-endian
signed 64-bit sums, folded with a seed stored at ``0x900190``; the resulting
32-bit value is written at ``0x900194``.

The fold keeps C ``uint64_t`` semantics: every partial sum is masked to 64 bits
after the XOR with the seed, exactly like the reference implementation.
"""

from __future__ import annotations

import struct

from .crypto import USER_SAVE_SIZE

__all__ = [
    "USER_CHECKSUM_BODY_END",
    "USER_CHECKSUM_BODY_SIZE",
    "USER_CHECKSUM_BODY_START",
    "USER_CHECKSUM_SEED_OFFSET",
    "USER_CHECKSUM_VALUE_OFFSET",
    "USER_SAVE_SIZE",
    "compute_user_checksum",
    "patch_user_checksum",
    "verify_user_checksum",
]

USER_CHECKSUM_BODY_START = 0x190
USER_CHECKSUM_BODY_END = 0x900190
USER_CHECKSUM_BODY_SIZE = USER_CHECKSUM_BODY_END - USER_CHECKSUM_BODY_START  # 0x900000
USER_CHECKSUM_SEED_OFFSET = 0x900190
USER_CHECKSUM_VALUE_OFFSET = 0x900194

BLOCK_SIZE = 0x400
VALUES_PER_BLOCK = BLOCK_SIZE // 8
MASK_64 = 0xFFFFFFFFFFFFFFFF
MASK_32 = 0xFFFFFFFF

# Invariants of the fixed save layout; a violation means the constants above
# were edited inconsistently and every checksum would silently be wrong.
assert USER_CHECKSUM_BODY_SIZE % BLOCK_SIZE == 0, "checksum body must be block aligned"
assert USER_CHECKSUM_BODY_END == USER_CHECKSUM_SEED_OFFSET, "seed must follow the body"
assert USER_CHECKSUM_VALUE_OFFSET + 4 <= USER_SAVE_SIZE, "checksum value must fit the save"


def compute_user_checksum(body: bytes, seed: int) -> int:
    """Compute the 32-bit folded checksum over the 0x900000-byte body."""
    if len(body) != USER_CHECKSUM_BODY_SIZE:
        raise ValueError(
            f"Checksum body must be {USER_CHECKSUM_BODY_SIZE:#x} bytes, got {len(body):#x}"
        )
    if not 0 <= seed <= MASK_32:
        raise ValueError(f"checksum seed must fit in uint32, got {seed:#x}")

    total = 0
    block_sum = 0
    index = 0
    for (value,) in struct.iter_unpack("<q", body):
        block_sum += value
        index += 1
        if index == VALUES_PER_BLOCK:
            total = ((total + block_sum) ^ seed) & MASK_64
            block_sum = 0
            index = 0
    return ((total // MASK_32) + (total & MASK_32)) & MASK_32


def _require_checksum_region(data: bytes | bytearray, what: str) -> None:
    if len(data) < USER_CHECKSUM_VALUE_OFFSET + 4:
        raise ValueError(f"{what} is too small to contain the Nioh 3 user checksum")


def patch_user_checksum(data: bytearray) -> tuple[int, int]:
    """Recompute and write the user checksum in place; return ``(old, new)``."""
    _require_checksum_region(data, "input")
    if not isinstance(data, bytearray):
        raise TypeError("patch_user_checksum requires a bytearray to patch in place")
    seed = struct.unpack_from("<I", data, USER_CHECKSUM_SEED_OFFSET)[0]
    old = struct.unpack_from("<I", data, USER_CHECKSUM_VALUE_OFFSET)[0]
    new = compute_user_checksum(
        bytes(data[USER_CHECKSUM_BODY_START:USER_CHECKSUM_BODY_END]), seed
    )
    struct.pack_into("<I", data, USER_CHECKSUM_VALUE_OFFSET, new)
    return old, new


def verify_user_checksum(data: bytes | bytearray) -> bool:
    """Return whether the stored checksum matches the body.

    A mismatch means the save was produced by a different game version, was
    hand-edited, or was only partially decrypted.  The editor reports this
    before touching anything so the user can abort.
    """
    _require_checksum_region(data, "input")
    seed = struct.unpack_from("<I", data, USER_CHECKSUM_SEED_OFFSET)[0]
    stored = struct.unpack_from("<I", data, USER_CHECKSUM_VALUE_OFFSET)[0]
    computed = compute_user_checksum(
        bytes(data[USER_CHECKSUM_BODY_START:USER_CHECKSUM_BODY_END]), seed
    )
    return stored == computed
