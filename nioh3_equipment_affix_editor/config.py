"""The user-editable configuration file (``config/editor.json``).

Everything a user may reasonably want to change without touching the command
line lives here: where saves are found, which crypto backend is preferred,
where backups go, and the GUI's initial checkbox states.  Command-line options
always win over the file, so the file supplies *defaults*.

The file is deliberately strict -- unknown keys, wrong types and a mismatched
``schema`` are all errors -- because a silently ignored typo (``dry_run`` instead
of ``default_dry_run``) is worse than a startup complaint.  Keys starting with
``_`` are treated as documentation and ignored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import paths

__all__ = [
    "CONFIG_SCHEMA",
    "ConfigError",
    "CryptoBackend",
    "EditorConfig",
    "default_config_document",
    "load_config",
    "write_default_config",
]

CONFIG_SCHEMA = "nioh3-accessory-editor-config/v1"

_BOOL_KEYS = ("prefer_python_crypto", "default_dry_run", "default_verify")
_INT_KEYS = ("account", "save_index")
_PATH_KEYS = ("save_root", "backup_root")
_STR_KEYS = ("crypto_exe",)
_KNOWN_KEYS = frozenset(
    ("schema",) + _BOOL_KEYS + _INT_KEYS + _PATH_KEYS + _STR_KEYS
)


class ConfigError(ValueError):
    """Raised when the configuration file is unusable (fail closed)."""


@dataclass(frozen=True, slots=True)
class CryptoBackend:
    """Chosen crypto implementation for one run."""

    executable: Path | None
    prefer_python: bool
    note: str = ""


@dataclass(slots=True)
class EditorConfig:
    """Effective configuration: file contents merged over built-in defaults."""

    source: Path | None = None
    save_root: Path | None = None
    account: int | None = None
    save_index: int | None = None
    crypto_exe: str | None = "bin/Nioh_Savefile_decrypt.exe"
    prefer_python_crypto: bool = False
    backup_root: Path | None = None
    default_dry_run: bool = True
    default_verify: bool = True
    problems: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- derived

    def resolved_crypto_exe(self, root: Path | None = None) -> Path | None:
        """Absolute path of the crypto helper, or ``None`` for the built-in.

        Relative paths are resolved against the application directory, so
        ``bin/Nioh_Savefile_decrypt.exe`` keeps working wherever the folder is
        moved to.
        """
        if self.prefer_python_crypto or not self.crypto_exe:
            return None
        raw = Path(self.crypto_exe).expanduser()
        if raw.is_absolute():
            return raw
        base = root if root is not None else paths.application_root()
        return (base / raw).resolve()

    def resolved_save_root(self, root: Path | None = None) -> Path | None:
        """Directory searched for saves, or ``None`` for automatic discovery."""
        if self.save_root is None:
            return None
        raw = Path(self.save_root).expanduser()
        if raw.is_absolute():
            return raw
        base = root if root is not None else paths.application_root()
        return (base / raw).resolve()

    def resolved_backup_root(self, root: Path | None = None) -> Path:
        """Directory that holds ``_nioh3_accessory_backup``."""
        if self.backup_root is None:
            return root if root is not None else paths.default_state_root()
        raw = Path(self.backup_root).expanduser()
        if raw.is_absolute():
            return raw
        base = root if root is not None else paths.application_root()
        return (base / raw).resolve()

    def crypto_backend(self) -> "CryptoBackend":
        """Which crypto implementation to use, with fallbacks already applied.

        ``crypto_exe: null`` (or ``prefer_python_crypto``) is an explicit request
        for the built-in pure-Python implementation; a configured path that does
        not exist falls back to the bundled helper, then to pure Python.
        """
        if self.prefer_python_crypto or not self.crypto_exe:
            return CryptoBackend(None, True,
                                 "已按配置使用内置纯 Python 实现")
        configured = self.resolved_crypto_exe()
        if configured is not None and configured.is_file():
            return CryptoBackend(configured, False, "")
        bundled = paths.default_crypto_exe()
        if bundled.is_file():
            note = ""
            if configured is not None:
                note = f"配置的加密组件不存在（{configured}），改用内置随附组件"
            return CryptoBackend(bundled, False, note)
        note = "未找到随附加密组件，改用内置纯 Python 实现（较慢）"
        if configured is not None:
            note = f"配置的加密组件不存在（{configured}）；{note}"
        return CryptoBackend(None, True, note)

    def with_overrides(self, **values) -> "EditorConfig":
        """Return a copy with the non-``None`` overrides applied.

        Command-line options are layered on top of the file this way: an option
        the user did not pass is ``None`` and leaves the file's value alone.
        """
        fields = set(EditorConfig.__dataclass_fields__)
        applied = {key: value for key, value in values.items()
                   if value is not None and key in fields}
        return replace(self, **applied) if applied else self

    def to_dict(self) -> dict:
        return {
            "schema": CONFIG_SCHEMA,
            "save_root": str(self.save_root) if self.save_root else None,
            "account": self.account,
            "save_index": self.save_index,
            "crypto_exe": self.crypto_exe,
            "prefer_python_crypto": self.prefer_python_crypto,
            "backup_root": str(self.backup_root) if self.backup_root else None,
            "default_dry_run": self.default_dry_run,
            "default_verify": self.default_verify,
        }

    def describe(self) -> list[str]:
        """Human-readable lines for ``config --show`` and the GUI dialog."""
        crypto = self.resolved_crypto_exe()
        lines = [
            f"配置文件  : {self.source if self.source else '（未使用，全部为内置默认值）'}",
            f"存档目录  : {self.resolved_save_root() if self.save_root else '自动发现（KoeiTecmo/NIOH3/Savedata）'}",
            f"账号过滤  : {self.account if self.account is not None else '不限'}",
            f"槽位过滤  : {self.save_index if self.save_index is not None else '不限'}",
            f"备份目录  : {self.resolved_backup_root()}",
            ("加密组件  : 内置纯 Python 实现"
             if crypto is None else f"加密组件  : {crypto}"),
            f"GUI 演练  : 默认{'开启' if self.default_dry_run else '关闭'}",
            f"GUI 校验  : 默认{'开启' if self.default_verify else '关闭'}",
        ]
        if self.problems:
            lines.extend(f"提示      : {problem}" for problem in self.problems)
        return lines


def default_config_document(version: str = "", commit: str = "") -> dict:
    """The document written by ``config --init`` / shipped in the payload."""
    return {
        "schema": CONFIG_SCHEMA,
        "_readme": [
            "本文件是「仁王3 装备词条修改器」的参数配置文件，首次运行时随程序解压到 exe 同目录。",
            "改完后重启程序生效；命令行参数始终优先于本文件。",
            "路径可以是绝对路径，也可以是相对 exe 目录的相对路径。",
            "crypto_exe 设为 null 或把 prefer_python_crypto 设为 true 可改用内置纯 Python 实现。",
            "只用测试学习，不要用于联机影响游戏平衡。",
        ],
        "_generated_for": {"version": version, "commit": commit},
        **EditorConfig().to_dict(),
    }


def _as_path(value, key: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} 必须是非空字符串或 null，实际是 {value!r}")
    return Path(value.strip())


def _as_bool(value, key: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{key} 必须是 true/false，实际是 {value!r}")
    return value


def _as_int(value, key: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} 必须是整数或 null，实际是 {value!r}")
    if value < 0:
        raise ConfigError(f"{key} 不能为负数，实际是 {value!r}")
    return value


def load_config(explicit: Path | None = None,
                root: Path | None = None) -> EditorConfig:
    """Load the configuration file, or return defaults when there is none.

    ``explicit`` (``--config PATH``) must exist; the default location may be
    absent, which simply means "all defaults".
    """
    if explicit is not None:
        path = Path(explicit)
        if not path.is_file():
            raise ConfigError(f"找不到配置文件: {path}")
    else:
        path = (root or paths.application_root()) / "config" / "editor.json"
        if not path.is_file():
            return EditorConfig()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ConfigError(f"配置文件不是合法 JSON: {path}\n  {error}") from error
    except OSError as error:
        raise ConfigError(f"无法读取配置文件: {path}\n  {error}") from error

    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件顶层必须是对象: {path}")

    schema = raw.get("schema")
    if schema != CONFIG_SCHEMA:
        raise ConfigError(
            f"配置文件 schema 不匹配: 期望 {CONFIG_SCHEMA!r}，实际 {schema!r}（{path}）"
        )

    unknown = sorted(key for key in raw
                     if key not in _KNOWN_KEYS and not key.startswith("_"))
    if unknown:
        raise ConfigError(
            "配置文件包含未知键（拼写错误不会被静默忽略）: "
            + "、".join(unknown) + f"（{path}）"
        )

    config = EditorConfig(source=path)
    for key in _BOOL_KEYS:
        if key in raw:
            setattr(config, key, _as_bool(raw[key], key))
    for key in _INT_KEYS:
        if key in raw:
            setattr(config, key, _as_int(raw[key], key))
    for key in _PATH_KEYS:
        if key in raw:
            setattr(config, key, _as_path(raw[key], key))
    if "crypto_exe" in raw:
        value = raw["crypto_exe"]
        if value is not None and not isinstance(value, str):
            raise ConfigError(f"crypto_exe 必须是字符串或 null，实际是 {value!r}")
        config.crypto_exe = value.strip() if isinstance(value, str) else None
    if config.account is not None:
        config.problems.append(f"账号过滤已启用: {config.account}")
    return config


def write_default_config(path: Path | None = None, *, version: str = "",
                         commit: str = "", overwrite: bool = False) -> Path:
    """Write the default configuration file; refuse to clobber silently."""
    target = Path(path) if path is not None else paths.default_config_path()
    if target.exists() and not overwrite:
        raise ConfigError(f"配置文件已存在（需要 --force 才会覆盖）: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    document = default_config_document(version=version, commit=commit)
    # Explicit newline: the file is identical on every platform (and shares its
    # bytes with the copy embedded in the executable's payload).
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    return target
