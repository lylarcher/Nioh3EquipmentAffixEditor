"""合法修改上限的**唯一来源**（按大类），以及它的来历。

为什么单独立一个模块
--------------------

这些上限以前散落在 ``records.MAX_ITEM_LEVEL`` / ``records.MAX_RECORD_PLUS``、
``equipmentdb.PLUS_CAP_BY_BIG`` 以及若干处硬编码的"稀有度 0..5"里。上限是**会随
游戏进度变的**（见下），散着放迟早会漏改一处，所以收敛到这里：其它模块要么直接
用本模块的函数，要么把常量 re-export 出去（``records`` / ``equipmentdb`` 就是这样），
**不再各自定义数值**。

上限的来历（用户口径，2026-09 核对）
------------------------------------

游戏目前开放到**三周目**（本体 + DLC1，DLC2 未出），所以：

======  ======  =======  ========
大类    等级    +値      稀有度
======  ======  =======  ========
武器    ≤180    ≤30      ≤4
防具    ≤180    ≤30      ≤4
饰品    ≤180    ≤30      ≤4
魂核    ≤180    **≤15**  **≤3**
绘卷    ≤180    **无**   ≤4
======  ======  =======  ========

* **等级 180** 同时也是存档可序列化的上限（参考存档实测最高正好 180）。
* **+値**：魂核明显比其它物品低（参考存档实测 0..15，其它物品到 30）；绘卷没有 +値。
* **稀有度**：四种物品到 4（神器），魂核到 3（特大名器）；``records.RARITY_NAMES``
  里第 5 档"神宝"目前不可达。
* **绘卷**本工具还没有页签，这里只记录上限，不开放编辑。

稀有度的**游戏内颜色**（用户口径，2026-09 提供）
------------------------------------------------

数值 -> 颜色是**一一对应**的，与上限是两件事：``RARITY_COLOR_BY_VALUE`` 记的是"这个
数值在游戏里显示成什么颜色"，``RARITY_CAP_BY_BIG`` 记的是"这一大类**允许**改到几"。
所以 5 = 橙色在映射里存在，但**当前周目不可达**（三周目 / 本体 + DLC1 的上限是 4，
魂核 3）；DLC2 开放后把上限表提到 5，橙色自然就可达了。颜色名**只用于显示**，
任何写入校验都不看它。

**DLC2 / 四周目开放之后**，等级、+値、稀有度上限都会提高 —— 届时**只改本文件下面
这三张表**（``LEVEL_CAP`` / ``PLUS_CAP_BY_BIG`` / ``RARITY_CAP_BY_BIG``）即可，
其余代码与测试都从这张表取数。
"""

from __future__ import annotations

__all__ = [
    "LEVEL_CAP",
    "MIN_LEVEL",
    "PLUS_CAP_BY_BIG",
    "RARITY_CAP_BY_BIG",
    "RARITY_COLOR_BY_VALUE",
    "DEFAULT_PLUS_CAP",
    "DEFAULT_RARITY_CAP",
    "CAP_ORIGIN",
    "level_cap",
    "plus_cap",
    "rarity_cap",
    "rarity_color_name",
    "describe_origin",
]

#: 等级上限（所有物品一致；也是存档可序列化的上限）。
#: DLC2 / 四周目开放后在此上调。
LEVEL_CAP = 180

#: 等级下限（0 级的空记录不算物品，所以从 1 起）。
MIN_LEVEL = 1

#: ``+値`` 上限：按大类。``None`` 表示该大类**没有** +値（绘卷）。
#: DLC2 / 四周目开放后在此上调。
PLUS_CAP_BY_BIG: dict[str, int | None] = {
    "武器": 30,
    "防具": 30,
    "饰品": 30,
    "魂核": 15,
    "绘卷": None,
}

#: 稀有度上限：按大类（0=粗物 … 4=神器；名表见 ``records.RARITY_NAMES``）。
#: DLC2 / 四周目开放后在此上调（那时 5=神宝 才会可达）。
RARITY_CAP_BY_BIG: dict[str, int] = {
    "武器": 4,
    "防具": 4,
    "饰品": 4,
    "魂核": 3,
    "绘卷": 4,
}

#: 表里查不到的大类退回这两个文档值（与"多数物品"的口径一致）。
DEFAULT_PLUS_CAP = 30
DEFAULT_RARITY_CAP = 4

#: 稀有度数值 -> 游戏内颜色（用户实测口径，0..5 一一对应）。
#:
#: **只用于显示**：写入上限与它无关（见 :data:`RARITY_CAP_BY_BIG`），所以 5 = 橙色
#: 在当前三周目（本体 + DLC1）**不可达** —— 武器/防具/饰品/绘卷 的上限是 4（绿色）、
#: 魂核是 3（紫色）；DLC2 之后上限提到 5，橙色才会可达。
RARITY_COLOR_BY_VALUE: dict[int, str] = {
    0: "白色",
    1: "黄色",
    2: "蓝色",
    3: "紫色",
    4: "绿色",
    5: "橙色",
}

#: 给使用者/拒绝信息看的一句话来历（界面与文档引用它，避免各写一份）。
CAP_ORIGIN = ("当前游戏为三周目（本体 + DLC1）：等级上限 180；+值上限武器/防具/饰品 30、"
              "魂核 15、绘卷无 +值；稀有度上限武器/防具/饰品/绘卷 4、魂核 3。"
              "稀有度的游戏内颜色：0 白 / 1 黄 / 2 蓝 / 3 紫 / 4 绿 / 5 橙，"
              "其中橙色（5）在当前周目不可达，DLC2 之后才可能开放。"
              "DLC2 / 四周目开放后上限会提高，届时只需改这一张表。")


def level_cap(big: str | None = None) -> int:
    """等级上限（目前所有大类一致，``big`` 只是留给以后分化时用）。"""
    return LEVEL_CAP


def plus_cap(big: str | None) -> int | None:
    """``+値`` 上限；``None`` = 该大类没有 +値（绘卷）。

    表里没有的大类退回 :data:`DEFAULT_PLUS_CAP`；``big`` 为 ``None``（不知道大类）
    时同样退回文档值，调用方应自己决定要不要更保守。
    """
    if big is None:
        return DEFAULT_PLUS_CAP
    if big in PLUS_CAP_BY_BIG:
        return PLUS_CAP_BY_BIG[big]
    return DEFAULT_PLUS_CAP


def rarity_cap(big: str | None) -> int:
    """稀有度上限：武器/防具/饰品/绘卷 4、魂核 3。"""
    if big is None:
        return DEFAULT_RARITY_CAP
    return RARITY_CAP_BY_BIG.get(big, DEFAULT_RARITY_CAP)


def rarity_color_name(value: object) -> str:
    """稀有度数值对应的游戏内颜色名；**未知值返回空串**（fail-soft，不抛异常）。

    只做显示：调用方（界面 / 列表）拿它拼「4 绿色」，拿不到就什么都不显示。
    数值上限与它无关 —— 校验一律走 :func:`rarity_cap`。
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return ""
    return RARITY_COLOR_BY_VALUE.get(value, "")


def describe_origin() -> str:
    """上限来历的一句话（拒绝信息与界面提示用）。"""
    return CAP_ORIGIN
