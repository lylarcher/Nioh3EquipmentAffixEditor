"""Nioh 3 Equipment Affix Editor —— 仁王3 装备词条修改器（仅供测试学习用）。

参考 Nioh3-Scroll-Generator 的工程组织方式：
- 纯 Python（仅标准库），加解密走「参考 exe 子进程（默认）/ 纯 Python 定制 AES」双后端。
- 存档读写采用「先解密 -> 修改 -> 校验和 -> 加密 -> 原子写回 + 备份」的安全管线。
- 词条合法性以《仁王3词条装备库v2.21.xlsx》饰品词条表为唯一来源（fail-closed）。

本工具仅供测试学习用，请勿用于联机环境或影响游戏平衡。
"""

from .version import __version__

__all__ = ["__version__"]
