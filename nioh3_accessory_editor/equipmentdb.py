"""武器 / 防具的目录（P1）：词条表、物品总目录全类别、按类别收集的字段范围。

全部是**只为显示与筛选**的数据：能不能写仍然由 :mod:`editor` 的合法规则决定。
本模块对现有饰品/魂核路径毫无影响（它们继续用自己的 JSON 与 :class:`AffixDb`)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .affixdb import AffixDb, AffixError, ItemDb, resource_root

#: 近战武器词条表（近战词条 + 绿色星号词条的「近战」行）——
#: 用户规则：【近战】只出现在武器上，且**不含远程武器（弓 / 火枪 / 大炮）**。
DEFAULT_MELEE_WEAPON_CATALOG = resource_root() / "data" / "melee_weapon_affixes.json"
#: 远程武器词条表（远程词条 + 绿色星号词条的「远程 / 弓 / 火枪 / 大炮」行）。
DEFAULT_RANGED_WEAPON_CATALOG = resource_root() / "data" / "ranged_weapon_affixes.json"
#: 防具词条表（防具词条 + 绿色星号词条的防具行）。
DEFAULT_ARMOR_CATALOG = resource_root() / "data" / "armor_affixes.json"
#: 物品总目录（全部大类：武器 / 防具 / 饰品 / 魂核 / …）。
DEFAULT_EQUIPMENT_ITEM_CATALOG = resource_root() / "data" / "equipment_items.json"
#: 按类别收集的字段范围（等级 / +値 / 词条槽数），来自参考存档的实测。
DEFAULT_EQUIPMENT_RANGES = resource_root() / "data" / "equipment_ranges.json"

MELEE_WEAPON_CATALOG_SCHEMA = "nioh3-melee-weapon-affixes/v1"
RANGED_WEAPON_CATALOG_SCHEMA = "nioh3-ranged-weapon-affixes/v1"
ARMOR_CATALOG_SCHEMA = "nioh3-armor-affixes/v1"
EQUIPMENT_ITEM_SCHEMA = "nioh3-equipment-items/v1"
EQUIPMENT_RANGES_SCHEMA = "nioh3-equipment-ranges/v1"

#: 大类 -> 中文说明（筛选与页签用）。
EQUIPMENT_BIG_CLASSES = ("武器", "防具", "饰品", "魂核", "远程武器")


@dataclass(frozen=True, slots=True)
class EquipmentItem:
    """物品总目录的一行：一个具体的物品种类（种类 = id + 名称）。"""

    item_id: int
    name: str
    #: 中类（武士防具 / 忍者防具 / 武士武器 / 忍者武器 / 远程武器 / 武士饰品 …）。
    category: str
    #: 小类 —— 用户说的「类型」：刀 / 大太刀 / 胸甲（身体）/ 腿甲（腿部）…
    small: str = ""
    #: 大类：武器 / 防具 / 饰品 / 魂核 / …
    big: str = ""
    source: str = ""

    @property
    def label(self) -> str:
        return f"{self.item_id:#06x} {self.name}"

    @property
    def school(self) -> str:
        """武士 / 忍者（中类去掉「武器 / 防具 / 饰品」后缀）。"""
        for suffix in ("武器", "防具", "饰品"):
            if self.category.endswith(suffix):
                return self.category[: -len(suffix)]
        return self.category


class EquipmentItemDb:
    """按 id / 大类 / 小类 / 中类查询物品种类（只读）。"""

    __slots__ = ("_entries", "_by_id", "catalog_path", "error")

    def __init__(self, entries: list[EquipmentItem] | None = None,
                 catalog_path: Path = DEFAULT_EQUIPMENT_ITEM_CATALOG,
                 *, error: str = "") -> None:
        self.catalog_path = catalog_path
        self.error = error
        self._entries = tuple(entries) if entries is not None else ()
        by_id: dict[int, EquipmentItem] = {}
        for entry in self._entries:
            by_id.setdefault(entry.item_id, entry)
        self._by_id = by_id

    @property
    def is_loaded(self) -> bool:
        return bool(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def all(self) -> tuple[EquipmentItem, ...]:
        return self._entries

    def lookup(self, item_id: int) -> EquipmentItem | None:
        return self._by_id.get(item_id)

    def labels(self) -> tuple[str, ...]:
        return tuple(entry.label for entry in self._entries)

    def big_classes(self) -> tuple[str, ...]:
        seen: list[str] = []
        for entry in self._entries:
            if entry.big and entry.big not in seen:
                seen.append(entry.big)
        return tuple(seen)

    def small_classes(self, big: str = "") -> tuple[str, ...]:
        """某个大类下的「类型」取值（武器 19 类、防具 5 类）。"""
        seen: list[str] = []
        for entry in self._entries:
            if big and entry.big != big:
                continue
            if entry.small and entry.small not in seen:
                seen.append(entry.small)
        return tuple(seen)

    def schools(self, big: str = "") -> tuple[str, ...]:
        seen: list[str] = []
        for entry in self._entries:
            if big and entry.big != big:
                continue
            school = entry.school
            if school and school not in seen:
                seen.append(school)
        return tuple(seen)


def load_equipment_items(path: Path | None = None) -> list[EquipmentItem]:
    """Load the 物品总目录 table (all 大类)."""
    target = DEFAULT_EQUIPMENT_ITEM_CATALOG if path is None else path
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise AffixError(f"找不到物品总目录表 {target}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise AffixError(f"物品种类表无法解析：{error}") from error
    raw = payload.get("items")
    if not isinstance(raw, list) or not raw:
        raise AffixError("物品种类表缺少 items 列表")
    entries: list[EquipmentItem] = []
    for index, item in enumerate(raw):
        where = f"{target} 第 {index + 1} 条"
        if not isinstance(item, dict):
            raise AffixError(f"{where}: 必须是对象")
        item_id = item.get("item_id")
        name = item.get("name")
        if not isinstance(item_id, int) or not 0 <= item_id <= 0xFFFFFFFF:
            raise AffixError(f"{where}: item_id 不合法")
        if not isinstance(name, str) or not name.strip():
            raise AffixError(f"{where}: 缺少名称")
        entries.append(EquipmentItem(
            item_id=item_id, name=name.strip(),
            category=str(item.get("category", "")).strip(),
            small=str(item.get("small", "")).strip(),
            big=str(item.get("big", "")).strip(),
            source=str(item.get("source", "")).strip(),
        ))
    return entries


def load_equipment_item_db(path: Path | None = None) -> EquipmentItemDb:
    """Load the item table, degrading to an empty one carrying the reason."""
    target = DEFAULT_EQUIPMENT_ITEM_CATALOG if path is None else path
    try:
        return EquipmentItemDb(load_equipment_items(target), target)
    except AffixError as error:
        return EquipmentItemDb(catalog_path=target, error=str(error))


#: 远程武器的「类型」（小类）；其余武器类型都按近战处理。
RANGED_WEAPON_SMALLS = ("弓", "火枪", "大炮")


def pool_of_item(item: "EquipmentItem | None", small: str = "",
                 category: str = "") -> str:
    """这件武器属于近战还是远程：以物品总目录的 中类/小类 为准。

    ``远程武器`` 中类，或 弓 / 火枪 / 大炮 这三种小类 -> ``"ranged"``；
    其余武器 -> ``"melee"``。
    """
    if item is not None:
        category = item.category or category
        small = item.small or small
    if category == "远程武器" or small in RANGED_WEAPON_SMALLS:
        return "ranged"
    return "melee"


def load_melee_weapon_db(path: Path | None = None) -> AffixDb:
    """Load the 近战武器 affix table."""
    target = DEFAULT_MELEE_WEAPON_CATALOG if path is None else path
    return AffixDb.from_file(target)


def load_ranged_weapon_db(path: Path | None = None) -> AffixDb:
    """Load the 远程武器 affix table."""
    target = DEFAULT_RANGED_WEAPON_CATALOG if path is None else path
    return AffixDb.from_file(target)


def weapon_db_for(item: "EquipmentItem | None", *, melee: AffixDb | None = None,
                  ranged: AffixDb | None = None) -> AffixDb:
    """Pick the right weapon affix table for an item (远程 -> ranged)."""
    if pool_of_item(item) == "ranged":
        return ranged if ranged is not None else load_ranged_weapon_db()
    return melee if melee is not None else load_melee_weapon_db()


def load_armor_db(path: Path | None = None) -> AffixDb:
    """Load the 防具 affix table."""
    target = DEFAULT_ARMOR_CATALOG if path is None else path
    return AffixDb.from_file(target)


def load_equipment_ranges(path: Path | None = None) -> dict:
    """Load the per-类别 observed field ranges (等级 / +値 / 词条槽数)."""
    target = DEFAULT_EQUIPMENT_RANGES if path is None else path
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise AffixError(f"找不到按类别收集的范围表 {target}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise AffixError(f"范围表无法解析：{error}") from error
    if not isinstance(payload.get("classes"), dict):
        raise AffixError("范围表缺少 classes")
    return payload
