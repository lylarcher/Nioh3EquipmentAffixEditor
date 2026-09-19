"""Shared fixtures for the Nioh3AccessoryEditor test-suite.

The suite runs against a *synthetic* decrypted save: the crypto layer is the
only part that can be validated against the real game (via the reference exe),
while the record region is exercised with hand-built records.  This keeps the
record tests fast, deterministic and independent of any real save file, and it
documents the assumed layout in executable form.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nioh3_accessory_editor import records
from nioh3_accessory_editor.checksum import patch_user_checksum
from nioh3_accessory_editor.crypto import HEADER_SIZE, USER_SAVE_SIZE

EXE_PATH = PROJECT_ROOT / "bin" / "Nioh_Savefile_decrypt.exe"
HAVE_EXE = EXE_PATH.is_file()

#: Optional read-only sibling checkout of the reference project.  When present
#: the tests compare the ported crypto tables against the original C source.
REFERENCE_AES_C = (
    PROJECT_ROOT.parent / "Nioh3-Scroll-Generator"
    / "third_party" / "nioh_savefile_decrypt" / "aes.c"
)
HAVE_REFERENCE_SOURCE = REFERENCE_AES_C.is_file()


def reference_sbox_tables() -> tuple[list[int], list[int]] | None:
    """Parse the ACTIVE ``sbox``/``rsbox`` tables out of the reference aes.c.

    Returns ``None`` when the sibling checkout is unavailable.  The standard AES
    tables in that file are commented out, and the custom ``sbox`` is declared
    without an explicit size, so the patterns below are deliberately loose.
    """
    if not HAVE_REFERENCE_SOURCE:
        return None
    import re

    text = REFERENCE_AES_C.read_text(encoding="utf-8", errors="replace")

    def parse(pattern: str) -> list[int]:
        match = re.search(pattern, text, re.S)
        if match is None:
            raise AssertionError(f"table not found in {REFERENCE_AES_C}: {pattern}")
        body = match.group(1).split("//", 1)[0]
        return [int(value, 16) for value in re.findall(r"0x([0-9A-Fa-f]{1,2})", body)]

    return (
        parse(r"\n\s*static const uint8_t sbox\[\]\s*=\s*\{(.*?)\};"),
        parse(r"\n\s*static const uint8_t rsbox\[256\]\s*=\s*\{(.*?)\};"),
    )

#: The pure-Python backend needs ~32 s per full-file transform, so the tests
#: that exercise it end-to-end are opt-in via this environment variable.
PURE_CRYPTO_ENABLED = os.environ.get("NIOH3_PURE_CRYPTO_TESTS") == "1"

SAVE_CHECKSUM_SEED = 0x13579BDF
SAVE_CHECKSUM_SEED_OFFSET = 0x900190

#: The trailing 8 bytes sit outside the crypto stream: the reference exe writes
#: its own bytes there, and the writer preserves whatever the on-disk file had.
#: Comparisons across an exe round trip must therefore ignore this tail.
UNCOVERED_TAIL_BYTES = 8
CRYPTO_COVERED_SIZE = USER_SAVE_SIZE - UNCOVERED_TAIL_BYTES


def covered(data: bytes) -> bytes:
    """Return the crypto-covered prefix of a save byte string."""
    return data[:CRYPTO_COVERED_SIZE]


def build_record(
    *,
    record_type: int = 0x4001,
    level: int = 160,
    rarity: int = 5,
    account_low32: int = 0x89ABCDEF,
    effects: tuple[tuple[int, int, int], ...] = (),
    prefix: int = 0x00010002,
    tail_0: int = 0,
    tail_1: int = 0,
) -> bytes:
    """Build a 0xE8-byte synthetic item record with ``effects`` in slots 0..n.

    ``effects`` entries are ``(effect_id, value, metadata)``.
    """
    if len(effects) > records.EFFECT_COUNT:
        raise ValueError("too many effects for one record")
    record = bytearray(records.SCROLL_RECORD_SIZE)
    struct.pack_into("<H", record, records.RECORD_TYPE_OFFSET, record_type)
    struct.pack_into("<H", record, records.RECORD_MIRROR_TYPE_OFFSET, record_type)
    struct.pack_into("<H", record, records.RECORD_ITEM_COUNT_OFFSET, 1)
    struct.pack_into("<H", record, records.RECORD_LEVEL_OFFSET, level)
    struct.pack_into("<H", record, records.RECORD_MIRROR_LEVEL_OFFSET, level)
    struct.pack_into("<I", record, records.RECORD_ACCOUNT_LOW_OFFSET, account_low32)
    record[records.RECORD_RARITY_OFFSET] = rarity & 0x0F
    for index in range(records.EFFECT_COUNT):
        base = records.EFFECT_START + index * records.EFFECT_STRIDE
        struct.pack_into("<I", record, base, prefix)
        struct.pack_into("<I", record, base + 4, records.EMPTY_EFFECT_ID)
        struct.pack_into("<I", record, base + 0x10, tail_0)
        struct.pack_into("<I", record, base + 0x14, tail_1)
    for index, (effect_id, value, metadata) in enumerate(effects):
        base = records.EFFECT_START + index * records.EFFECT_STRIDE
        struct.pack_into("<I", record, base + 4, effect_id)
        struct.pack_into("<I", record, base + 8, value)
        struct.pack_into("<I", record, base + 0x0C, metadata)
    return bytes(record)


def expected_account_id(record_type: int = 0x4001, account_low32: int = 0x89ABCDEF) -> int:
    """Account id implied by the (dual-purpose) record header fields."""
    return (record_type << 48) | (1 << 32) | account_low32


def legacy_layout() -> "records.InventoryLayout":
    """The layout the synthetic fixtures write: the captured 0x176CCE array."""
    return records.InventoryLayout(
        anchor=records.LEGACY_GROUP_OFFSET,
        slot_count=records.SCROLL_SLOT_COUNT,
    )


def build_plain_save(
    *,
    records_by_slot: dict[int, bytes] | None = None,
    seed: int = SAVE_CHECKSUM_SEED,
    pattern_body: bool = True,
    anchor: int | None = None,
) -> bytes:
    """Build a synthetic decrypted (RNNUSR) USR save of the exact real size.

    ``anchor`` moves the inventory array, which is how the tests cover a game
    build whose save layout shifted the array away from the captured 0x176CCE.
    """
    data = bytearray(USER_SAVE_SIZE)
    data[0:6] = b"RNNUSR"
    for index in range(6, HEADER_SIZE):
        data[index] = (index * 7 + 3) & 0xFF
    if pattern_body:
        state = 0x12345678
        for index in range(HEADER_SIZE, USER_SAVE_SIZE):
            state = (state * 1103515245 + 12345) & 0x7FFFFFFF
            data[index] = (state >> 16) & 0xFF
    # Keep the record region deterministic: only caller-provided slots are set.
    data[records.SCROLL_GROUP_OFFSET:records.SCROLL_GROUP_END] = bytes(
        records.SCROLL_GROUP_END - records.SCROLL_GROUP_OFFSET
    )
    layout = records.InventoryLayout(
        anchor=records.LEGACY_GROUP_OFFSET if anchor is None else anchor,
        slot_count=records.SCROLL_SLOT_COUNT,
    )
    for slot_index, record in (records_by_slot or {}).items():
        if len(record) != records.SCROLL_RECORD_SIZE:
            raise ValueError("record must be 0xE8 bytes")
        offset = records.record_offset(slot_index, layout=layout)
        data[offset:offset + records.SCROLL_RECORD_SIZE] = record
    struct.pack_into("<I", data, SAVE_CHECKSUM_SEED_OFFSET, seed)
    patch_user_checksum(data)
    return bytes(data)


def make_fake_save_tree(root: Path, *, account: int = 76561198000000000, slots: int = 2) -> list[Path]:
    """Create a fake ``Savedata`` tree and return the SAVEDATA.BIN paths."""
    created: list[Path] = []
    for slot in range(slots):
        directory = root / str(account) / f"SAVEDATA{slot:02d}"
        directory.mkdir(parents=True, exist_ok=True)
        main = directory / "SAVEDATA.BIN"
        main.write_bytes(b"ciphertext-placeholder")
        (directory / "BACKUP.BIN").write_bytes(b"backup-placeholder")
        created.append(main)
    system = root / str(account) / "SYSTEMSAVEDATA00"
    system.mkdir(parents=True, exist_ok=True)
    (system / "SAVEDATA.BIN").write_bytes(b"system-placeholder")
    return created


# --------------------------------------------------------------------------
# Build/version helpers
# --------------------------------------------------------------------------

def load_tool_module(name: str):
    """Import a module from ``tools/`` by name (that directory is not a package)."""
    import importlib.util

    path = PROJECT_ROOT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_tool_{name}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load tool module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BUILD_INFO_MODULE_NAME = "nioh3_accessory_editor._buildinfo"


def install_build_info_module(source: str):
    """Install ``source`` as the package's ``_buildinfo`` module and return it.

    Lets tests exercise the frozen-build path without running a real build.
    """
    import importlib.util

    module = type(sys)(BUILD_INFO_MODULE_NAME)
    module.__file__ = "<generated _buildinfo for tests>"
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    sys.modules[BUILD_INFO_MODULE_NAME] = module

    package = sys.modules.get("nioh3_accessory_editor")
    if package is not None:
        setattr(package, "_buildinfo", module)
    return module


def forget_build_info_module() -> None:
    """Remove any injected ``_buildinfo`` so tests stay independent."""
    sys.modules.pop(BUILD_INFO_MODULE_NAME, None)
    package = sys.modules.get("nioh3_accessory_editor")
    if package is not None and hasattr(package, "_buildinfo"):
        delattr(package, "_buildinfo")
