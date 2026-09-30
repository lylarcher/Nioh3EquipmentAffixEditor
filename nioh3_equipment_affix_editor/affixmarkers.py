"""Per-affix record markers (``prefix u32@+0x00``) measured from a natural save.

An effect slot's ``prefix`` ("条目标记" / group key) belongs to the affix that occupies
the slot: replacing the affix without updating it leaves the *old* affix's marker
behind, which an audit can read as a replaced effect.  ``tools/measure_affix_markers.py``
measured the marker of every affix id on a reference save this tool never wrote to, and
found the low 16 bits to be one constant per affix id (unanimous across all samples for
1328/1328 ids) with the high 16 bits 0 for the large majority.

This module is the read-only lookup for that table.  A missing table (or an affix the
reference save never showed) simply yields no marker, and the caller then leaves the
slot's ``+0x00`` exactly as it was — the tool never invents a marker.

The metadata low byte ("roll") is deliberately **not** part of this table: measurement
showed it is a per-drop quality percentage (0..100) that is neither a function of the
affix id nor of (id, value), so the tool keeps the byte it found.
"""

from __future__ import annotations

import json
from pathlib import Path

from .affixdb import AffixError, resource_root

#: Shipped marker table (see ``tools/measure_affix_markers.py``).
DEFAULT_MARKERS_PATH = resource_root() / "data" / "affix_markers.json"

MARKERS_SCHEMA = "nioh3-affix-markers/v1"


def load_affix_markers(path: Path | None = None) -> dict[int, int]:
    """Load ``effect_id -> prefix`` from the shipped marker table.

    Only ids with a measured marker are returned; every value is the *majority* full
    32-bit prefix, which already carries the unanimous low 16 bits.
    """
    target = DEFAULT_MARKERS_PATH if path is None else path
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise AffixError(f"找不到词条标记表 {target}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise AffixError(f"词条标记表无法解析：{error}") from error
    schema = payload.get("schema")
    if schema != MARKERS_SCHEMA:
        raise AffixError(f"词条标记表 schema 不受支持：{schema!r}")
    raw = payload.get("prefix_majority_by_effect_id")
    if not isinstance(raw, dict) or not raw:
        raise AffixError("词条标记表缺少 prefix_majority_by_effect_id")
    markers: dict[int, int] = {}
    for key, value in raw.items():
        try:
            effect_id = int(str(key), 0)
        except (TypeError, ValueError) as error:
            raise AffixError(f"词条标记表的 id 不合法：{key!r}") from error
        if not isinstance(value, int) or isinstance(value, bool):
            raise AffixError(f"词条标记表的标记不合法：{key!r} -> {value!r}")
        if not 0 <= value <= 0xFFFFFFFF:
            raise AffixError(f"词条标记超出 uint32：{key!r} -> {value:#x}")
        markers[effect_id] = value
    return markers


class AffixMarkers:
    """``effect_id -> prefix`` lookup that degrades to "no marker" when unavailable."""

    __slots__ = ("_markers", "catalog_path", "error")

    def __init__(self, markers: dict[int, int] | None = None,
                 catalog_path: Path = DEFAULT_MARKERS_PATH, *, error: str = "") -> None:
        self.catalog_path = catalog_path
        self.error = error
        self._markers = dict(markers) if markers else {}

    @classmethod
    def best_effort(cls, path: Path | None = None) -> "AffixMarkers":
        target = DEFAULT_MARKERS_PATH if path is None else path
        try:
            return cls(load_affix_markers(target), target)
        except AffixError as error:
            return cls(catalog_path=target, error=str(error))

    @property
    def is_loaded(self) -> bool:
        return bool(self._markers)

    def __len__(self) -> int:
        return len(self._markers)

    def prefix_for(self, effect_id: int) -> int | None:
        """The measured ``+0x00`` marker of ``effect_id``, or ``None`` when unknown."""
        return self._markers.get(effect_id)
