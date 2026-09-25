# -*- coding: utf-8 -*-
"""Full-file cross-check: rewritten pure-Python crypto vs the reference exe.

Builds a synthetic decrypted save, encrypts it with both backends and compares
every byte of the crypto-covered region, then checks the round trip and the
trailing-8-byte contract.
"""

import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nioh3_equipment_affix_editor.checksum import patch_user_checksum
from nioh3_equipment_affix_editor.crypto import USER_SAVE_SIZE, SaveCrypto

EXE = ROOT / "bin" / "Nioh_Savefile_decrypt.exe"


def build_plain() -> bytes:
    data = bytearray(USER_SAVE_SIZE)
    data[0:6] = b"RNNUSR"
    for index in range(6, 0x158):
        data[index] = (index * 7 + 3) & 0xFF
    seed = 0x12345678
    for index in range(0x158, USER_SAVE_SIZE):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        data[index] = (seed >> 16) & 0xFF
    struct.pack_into("<I", data, 0x900190, 0x13579BDF)
    patch_user_checksum(data)
    return bytes(data)


def main() -> int:
    plain = build_plain()
    crypto = SaveCrypto()

    start = time.perf_counter()
    encrypted = crypto.transform(plain)
    encrypt_seconds = time.perf_counter() - start

    start = time.perf_counter()
    decrypted = crypto.transform(encrypted)
    decrypt_seconds = time.perf_counter() - start

    failures = []
    if decrypted != plain:
        failures.append("pure-Python round trip is not lossless")

    with tempfile.TemporaryDirectory(prefix="nioh3-xcheck-") as directory:
        work = Path(directory)
        (work / "plain.bin").write_bytes(plain)
        result = subprocess.run(
            [str(EXE), "-i", str(work / "plain.bin"), "-o", "ref.bin"],
            cwd=work, input="\n", text=True, capture_output=True, timeout=300,
        )
        if result.returncode != 0:
            failures.append(f"reference exe failed: {result.returncode}")
        else:
            reference = (work / "ref.bin").read_bytes()
            covered = USER_SAVE_SIZE - 8
            mismatches = [
                index for index in range(covered)
                if reference[index] != encrypted[index]
            ]
            if mismatches:
                failures.append(
                    f"{len(mismatches)} byte(s) differ vs reference, first at "
                    f"{mismatches[0]:#x}"
                )
            else:
                print(f"MATCH: {covered} crypto-covered bytes identical to the exe")
            if reference[-8:] == encrypted[-8:]:
                print("tail note: exe also preserved the trailing 8 bytes")
            else:
                print("tail note: exe writes its own trailing 8 bytes (expected)")

    print(f"pure-Python encrypt {encrypt_seconds:.1f}s, decrypt {decrypt_seconds:.1f}s")

    if failures:
        print("FAILED:")
        for item in failures:
            print("  -", item)
        return 1
    print("FULL-FILE CROSS-CHECK PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
