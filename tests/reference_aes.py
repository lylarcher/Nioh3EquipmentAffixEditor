"""Literal, byte-by-byte transcription of the reference ``aes.c``.

This mirrors the C source statement for statement (flat 4x4 state traversed as
``state[i][j]``, in-place row/column operations, same round ordering) and is
deliberately *not* optimized.  The optimized table-driven implementation in
``nioh3_accessory_editor.nioh_aes`` must reproduce it exactly.

Keeping this in the tests means the "fast" rewrite is checked against what the
reference actually computes, not against the AES specification (which this
custom variant does not follow).
"""

from __future__ import annotations

from nioh3_accessory_editor.nioh_aes import RCON, RSBOX, SBOX

NK = 4
NR = 10
NB = 4
KEY_EXP_SIZE = 176


def key_expansion(key: bytes) -> list[int]:
    """Transcription of ``KeyExpansion`` (word-reversed initial round key)."""
    round_key = [0] * KEY_EXP_SIZE
    for i in range(NK):
        round_key[i * 4 + 0] = key[i * 4 + 3]
        round_key[i * 4 + 1] = key[i * 4 + 2]
        round_key[i * 4 + 2] = key[i * 4 + 1]
        round_key[i * 4 + 3] = key[i * 4 + 0]
    i = NK
    while i < NB * (NR + 1):
        temp = [
            round_key[(i - 1) * 4 + 0],
            round_key[(i - 1) * 4 + 1],
            round_key[(i - 1) * 4 + 2],
            round_key[(i - 1) * 4 + 3],
        ]
        if i % NK == 0:
            first = temp[0]
            temp = [temp[1], temp[2], temp[3], first]
            temp = [SBOX[value] for value in temp]
            temp[0] ^= RCON[i // NK]
        for j in range(4):
            round_key[i * 4 + j] = round_key[(i - NK) * 4 + j] ^ temp[j]
        i += 1
    return round_key


def _xtime(value: int) -> int:
    return ((value << 1) ^ (((value >> 7) & 1) * 0x1B)) & 0xFF


def _multiply(value: int, factor: int) -> int:
    return (
        ((factor & 1) * value)
        ^ ((factor >> 1 & 1) * _xtime(value))
        ^ ((factor >> 2 & 1) * _xtime(_xtime(value)))
        ^ ((factor >> 3 & 1) * _xtime(_xtime(_xtime(value))))
        ^ ((factor >> 4 & 1) * _xtime(_xtime(_xtime(_xtime(value)))))
    ) & 0xFF


def _state_from_block(block: bytes) -> list[list[int]]:
    return [[block[i * 4 + j] for j in range(4)] for i in range(4)]


def _state_to_bytes(state: list[list[int]]) -> bytes:
    return bytes(state[i][j] for i in range(4) for j in range(4))


def _make_round_ops(state: list[list[int]], round_key: list[int]):
    def add_round_key(round_index: int) -> None:
        for i in range(4):
            for j in range(4):
                state[i][j] ^= round_key[round_index * 16 + i * 4 + j]

    def sub_bytes() -> None:
        for i in range(4):
            for j in range(4):
                state[j][i] = SBOX[state[j][i]]

    def inv_sub_bytes() -> None:
        for i in range(4):
            for j in range(4):
                state[j][i] = RSBOX[state[j][i]]

    def shift_rows() -> None:
        temp = state[0][1]
        state[0][1] = state[1][1]
        state[1][1] = state[2][1]
        state[2][1] = state[3][1]
        state[3][1] = temp
        temp = state[0][2]
        state[0][2] = state[2][2]
        state[2][2] = temp
        temp = state[1][2]
        state[1][2] = state[3][2]
        state[3][2] = temp
        temp = state[0][3]
        state[0][3] = state[3][3]
        state[3][3] = state[2][3]
        state[2][3] = state[1][3]
        state[1][3] = temp

    def inv_shift_rows() -> None:
        temp = state[3][1]
        state[3][1] = state[2][1]
        state[2][1] = state[1][1]
        state[1][1] = state[0][1]
        state[0][1] = temp
        temp = state[0][2]
        state[0][2] = state[2][2]
        state[2][2] = temp
        temp = state[1][2]
        state[1][2] = state[3][2]
        state[3][2] = temp
        temp = state[0][3]
        state[0][3] = state[1][3]
        state[1][3] = state[2][3]
        state[2][3] = state[3][3]
        state[3][3] = temp

    def mix_columns() -> None:
        for i in range(4):
            t0, t1, t2, t3 = state[i][0], state[i][1], state[i][2], state[i][3]
            total = t0 ^ t1 ^ t2 ^ t3
            state[i][0] ^= _xtime(t0 ^ t1) ^ total
            state[i][1] ^= _xtime(t1 ^ t2) ^ total
            state[i][2] ^= _xtime(t2 ^ t3) ^ total
            state[i][3] ^= _xtime(t3 ^ t0) ^ total

    def inv_mix_columns() -> None:
        for i in range(4):
            a, b, c, d = state[i][0], state[i][1], state[i][2], state[i][3]
            state[i][0] = (_multiply(a, 0x0E) ^ _multiply(b, 0x0B)
                           ^ _multiply(c, 0x0D) ^ _multiply(d, 0x09))
            state[i][1] = (_multiply(a, 0x09) ^ _multiply(b, 0x0E)
                           ^ _multiply(c, 0x0B) ^ _multiply(d, 0x0D))
            state[i][2] = (_multiply(a, 0x0D) ^ _multiply(b, 0x09)
                           ^ _multiply(c, 0x0E) ^ _multiply(d, 0x0B))
            state[i][3] = (_multiply(a, 0x0B) ^ _multiply(b, 0x0D)
                           ^ _multiply(c, 0x09) ^ _multiply(d, 0x0E))

    return add_round_key, sub_bytes, inv_sub_bytes, shift_rows, inv_shift_rows, \
        mix_columns, inv_mix_columns


def cipher(round_key: list[int], block: bytes) -> bytes:
    """Transcription of the reference ``Cipher`` (AES_ECB_encrypt core)."""
    state = _state_from_block(block)
    add_round_key, sub_bytes, _inv_sub, shift_rows, _inv_shift, mix_columns, _inv_mix = \
        _make_round_ops(state, round_key)
    add_round_key(0)
    for round_index in range(1, NR):
        sub_bytes()
        shift_rows()
        mix_columns()
        add_round_key(round_index)
    sub_bytes()
    shift_rows()
    add_round_key(NR)
    return _state_to_bytes(state)


def inv_cipher(round_key: list[int], block: bytes) -> bytes:
    """Transcription of the reference ``InvCipher`` (AES_ECB_decrypt core).

    NOTE: the reference's active ``rsbox`` is a copy of its ``sbox``, so this is
    NOT the inverse of :func:`cipher`.  It is transcribed verbatim because the
    optimized port claims to match ``aes.c`` exactly.
    """
    state = _state_from_block(block)
    add_round_key, _sub, inv_sub_bytes, _shift, inv_shift_rows, _mix, inv_mix_columns = \
        _make_round_ops(state, round_key)
    add_round_key(NR)
    for round_index in range(NR - 1, 0, -1):
        inv_shift_rows()
        inv_sub_bytes()
        add_round_key(round_index)
        inv_mix_columns()
    inv_shift_rows()
    inv_sub_bytes()
    add_round_key(0)
    return _state_to_bytes(state)
