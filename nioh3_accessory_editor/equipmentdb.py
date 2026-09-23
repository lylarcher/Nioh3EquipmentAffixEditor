"""武器 / 防具的目录（P1 数据 + P2 的判定数据）：词条表、物品总目录全类别、
按类别收集的字段范围。

本模块只提供**判定所需的数据**与纯粹的查表函数（词条池、装备种类标签 token、
按类别的实测范围、``+値`` 的大类固定上限表）：最终「能不能写」一律由 :mod:`editor`
的合法规则决定，本模块自己不写任何东西。本模块对现有饰品/魂核路径毫无影响（它们继续
用自己的 JSON 与 :class:`AffixDb`)。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .affixdb import AffixDb, AffixError, ItemDb, resource_root
from .records import MAX_ITEM_LEVEL, MAX_RECORD_PLUS

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


# --------------------------------------------------------------------------
# 词条池（P2）：一张武器/防具词条表 + 它自带的「装备种类」标签
# --------------------------------------------------------------------------

#: 「装备种类」标签的分隔符：源表用 ``近战/手臂`` 表示这条词条能出在哪些装备上。
EQUIPMENT_TAG_SEPARATOR = "/"

#: 词条池的键：武器按近战 / 远程分两池，防具单独一池（与源表的分表一致）。
POOL_MELEE = "melee"
POOL_RANGED = "ranged"
POOL_ARMOR = "armor"
#: 池键 -> 中文名（拒绝信息与界面显示用）。
POOL_LABELS = {POOL_MELEE: "近战武器", POOL_RANGED: "远程武器", POOL_ARMOR: "防具"}
#: 池键 -> 随包 JSON 路径。
POOL_PATHS = {
    POOL_MELEE: DEFAULT_MELEE_WEAPON_CATALOG,
    POOL_RANGED: DEFAULT_RANGED_WEAPON_CATALOG,
    POOL_ARMOR: DEFAULT_ARMOR_CATALOG,
}


def split_equipment_tags(label: str) -> tuple[str, ...]:
    """``近战/手臂`` -> ``("近战", "手臂")``；空串得到空元组。"""
    if not label:
        return ()
    return tuple(part.strip() for part in label.split(EQUIPMENT_TAG_SEPARATOR)
                 if part.strip())


def load_equipment_tags(path: Path) -> dict[int, tuple[str, ...]]:
    """读一张 P1 词条表的 ``equipment_tags``：词条 id -> 标签 token。

    标签说的是「这条词条能出在哪些装备上」，与词条的 类别（``category``，例如
    造成伤害）是两件事，所以它不能替换 ``category``，只能作为**额外的**写入门槛。
    文件没有这个键、或某条没有标签时一律不放进结果：调用方据此 fail closed，
    而不是把「标签缺失」当成「哪儿都能写」。
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise AffixError(f"找不到词条表 {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise AffixError(f"词条表无法解析：{error}") from error
    raw = payload.get("equipment_tags")
    if not isinstance(raw, dict):
        return {}
    tags: dict[int, tuple[str, ...]] = {}
    for key, value in raw.items():
        try:
            effect_id = int(str(key), 16)
        except ValueError:
            continue
        if not isinstance(value, str):
            continue
        tokens = split_equipment_tags(value)
        if tokens:
            tags[effect_id] = tokens
    return tags


@dataclass(frozen=True, slots=True)
class EquipmentPool:
    """一张武器/防具词条表，连同它的装备种类标签（同一个 JSON 文件里的两半）。"""

    key: str
    db: AffixDb
    tags: Mapping[int, tuple[str, ...]]
    path: Path

    @property
    def label(self) -> str:
        return POOL_LABELS.get(self.key, self.key)

    def tags_of(self, effect_id: int) -> tuple[str, ...]:
        """这条词条的装备种类 token；表里没有它时返回空元组（fail closed）。"""
        return tuple(self.tags.get(effect_id, ()))

    def describe_tags(self, effect_id: int) -> str:
        """``近战/手臂`` 这样的可读标签；没有标签时说清楚「没有」。"""
        tokens = self.tags_of(effect_id)
        return EQUIPMENT_TAG_SEPARATOR.join(tokens) if tokens else "（没有装备种类标签）"


_POOL_CACHE: dict[str, EquipmentPool] = {}


def load_pool(key: str, path: Path | None = None) -> EquipmentPool:
    """按池键加载词条表 + 标签；默认路径的结果缓存，JSON 只解析一次。

    给了 ``path`` 就现读现用、不进缓存（测试与工具可以指向临时文件）。
    """
    if key not in POOL_PATHS:
        raise AffixError(f"未知的词条池 {key!r}")
    target = POOL_PATHS[key] if path is None else path
    if path is not None:
        return EquipmentPool(key=key, db=AffixDb.from_file(target),
                             tags=load_equipment_tags(target), path=target)
    cached = _POOL_CACHE.get(key)
    if cached is None:
        cached = EquipmentPool(key=key, db=AffixDb.from_file(target),
                               tags=load_equipment_tags(target), path=target)
        _POOL_CACHE[key] = cached
    return cached


def pool_name_for(item: "EquipmentItem") -> str:
    """这件**武器/防具**用哪一池词条：防具单独一池，武器按近战 / 远程分。

    只对 大类 = 武器 / 防具 的物品有意义：饰品 / 魂核 各有自己的词条表，
    别拿这个函数的结果去查它们的词条。
    """
    if item.big == "防具":
        return POOL_ARMOR
    return POOL_RANGED if pool_of_item(item) == "ranged" else POOL_MELEE


def pool_for_item(item: "EquipmentItem") -> EquipmentPool:
    """这件武器/防具的词条池（表 + 标签）。"""
    return load_pool(pool_name_for(item))


def acceptable_equipment_tokens(item: "EquipmentItem") -> frozenset[str]:
    """这件装备能接受的「装备种类」token 集合（用户规则 1）。

    = 大类名（``武器`` / ``防具``）
    + 小类（武器的具体类型 ``弓`` / ``火枪`` / ``大炮`` …、防具的部位 ``手臂`` …）
    + 武器的近战 / 远程归属（``近战`` / ``远程``）。

    判定「能不能写」只比 token 交集：有交集就允许，完全没交集就拒绝。
    """
    tokens: set[str] = set()
    if item.big:
        tokens.add(item.big)
    if item.small:
        tokens.add(item.small)
    if item.big == "武器":
        tokens.add("近战" if pool_of_item(item) == "melee" else "远程")
    return frozenset(tokens)


# --------------------------------------------------------------------------
# 按类别取上限（P2 规则 2 的数据来源）
# --------------------------------------------------------------------------

#: 类别不在实测范围表里时用的文档值；直接引用记录层常量，避免两处漂移。
DOCUMENTED_MAX_LEVEL = MAX_ITEM_LEVEL
DOCUMENTED_MAX_PLUS = MAX_RECORD_PLUS

#: ``+値`` 上限的**大类固定表**（用户口径，不是实测值）：武器 / 防具 / 饰品 30、
#: 魂核 15。
#:
#: ``data/equipment_ranges.json`` 里的 ``plus`` 是"这份参考存档观测到的最大值"
#: （忍刀 25、忍者防具/手臂 23 …），**观测值不等于游戏上限**：武器 / 防具 / 饰品的
#: ``+値`` 上限就是 30，只有魂核收紧到 15。所以写入上限只查这张表，实测范围表退到
#: 证据的位置（:meth:`editor.ClassLimits.describe` 引用它的样本数）。饰品这一行是口径
#: 记录：饰品不走按类别的闸门，仍用扁平的 0..30（同一个数，行为不变）。
PLUS_CAP_BY_BIG: dict[str, int] = {
    "武器": MAX_RECORD_PLUS,
    "防具": MAX_RECORD_PLUS,
    "饰品": MAX_RECORD_PLUS,
    "魂核": 15,
}


def plus_cap_for_big(big: str) -> int | None:
    """这个大类的 ``+値`` 上限；表里没有的大类返回 ``None``（调用方退回文档值）。"""
    return PLUS_CAP_BY_BIG.get(big)


def equipment_class_key(item: "EquipmentItem") -> str:
    """范围表里的类别键，与 ``tools/measure_equipment_ranges.py`` 写入的键一致。"""
    if item.small:
        return f"{item.big}/{item.category}/{item.small}"
    return f"{item.big}/{item.category}"


def equipment_class_range(item: "EquipmentItem",
                          ranges: Mapping | None = None) -> Mapping | None:
    """这个物品所在类别的实测范围；类别不在表里（或表坏了）时返回 ``None``。"""
    table = load_equipment_ranges() if ranges is None else ranges
    classes = table.get("classes") if isinstance(table, Mapping) else None
    if not isinstance(classes, Mapping):
        return None
    bucket = classes.get(equipment_class_key(item))
    return bucket if isinstance(bucket, Mapping) else None


def equipment_caps(item: "EquipmentItem",
                   ranges: Mapping | None = None) -> tuple[int, int]:
    """``(等级上限, +値上限)``：该物品所在类别的**实测**上限，取不到就退回文档值。

    **这是观测数据，不是写入上限**：能不能写由 :mod:`editor` 决定 —— 等级默认用文档值
    180，``+値`` 用大类固定表 :data:`PLUS_CAP_BY_BIG`（武器 / 防具 / 饰品 30、魂核 15）。
    实测值来自 ``data/equipment_ranges.json``（每个类别都带样本数），作为证据保留：
    武器 / 防具观测到的 +値 各种类并不相同（22..30），魂核只到 15；等级上限多数类别观测
    到 180，少数类别低一些（例如弓 170、大太刀 172、忍者防具/足部 174）。
    """
    bucket = equipment_class_range(item, ranges)
    if bucket is None:
        return DOCUMENTED_MAX_LEVEL, DOCUMENTED_MAX_PLUS
    return (_span_max(bucket.get("level"), DOCUMENTED_MAX_LEVEL),
            _span_max(bucket.get("plus"), DOCUMENTED_MAX_PLUS))


def _span_max(span: object, fallback: int) -> int:
    """范围表里的一对 ``[最小, 最大]`` -> 上限；形状不对就退回文档值。"""
    if isinstance(span, (list, tuple)) and len(span) == 2:
        try:
            return int(span[1])
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return fallback
    return fallback
