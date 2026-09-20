"""Nioh 3 user-save discovery, decryption, integrity, and safe write-back.

Modeled on Nioh3-Scroll-Generator's ``savegame.py``:
* Save discovery under ``%LOCALAPPDATA%/KoeiTecmo/NIOH3/Savedata``.
* Two interchangeable crypto backends: the reference executable
  (``Nioh_Savefile_decrypt.exe``, fast and battle-tested) and a pure-Python
  port of the same custom-AES algorithm (``crypto.SaveCrypto``).
* Quiescence fingerprinting (two spaced reads must match) before any write.
* Encryption is verified by decrypting the staged file before it is installed,
  and the installed file is verified again afterwards; a failed post-write
  check restores the original bytes.
* Durable atomic replacement with fsync and a plaintext backup on disk.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import crypto as py_crypto
from . import paths

__all__ = [
    "BACKUP_MANIFEST_SCHEMA",
    "BACKUP_PLAIN_PATTERN",
    "BACKUP_SUBDIRECTORY_NAME",
    "GAME_PROCESS_NAMES",
    "SAVE_QUIESCENCE_SECONDS",
    "SAVE_SCHEMA_PROFILE",
    "SAVE_WRITE_REQUIREMENT",
    "BackupEntry",
    "SaveCrypto",
    "SaveError",
    "SaveFileFingerprint",
    "account_id_from_save_path",
    "backup_directory_for",
    "capture_quiescent_save_fingerprints",
    "capture_related_save_fingerprints",
    "create_backup",
    "decrypt_save_to_bytes",
    "default_crypto_tool",
    "discover_save_paths",
    "list_backups",
    "read_backup_plaintext",
    "require_game_not_running",
    "restore_save_from_bytes",
    "running_game_processes",
    "save_root_directory",
    "save_slot_index_from_path",
    "sha256_file",
    "validate_user_save_bytes",
    "write_encrypted_save",
]

SAVE_QUIESCENCE_SECONDS = 0.2
SAVE_SLOT_DIRECTORY_PATTERN = re.compile(r"^SAVEDATA(?P<index>\d{2})$")
BACKUP_SUBDIRECTORY_NAME = "_nioh3_accessory_backup"
BACKUP_MANIFEST_SCHEMA = "nioh3-accessory-editor/backup/v2"
SAVE_SCHEMA_PROFILE = "nioh3-pc-usr"

#: Executables that mean "the game may be holding this save in memory".
#: Matching is case-insensitive, so one spelling per game build is enough.
GAME_PROCESS_NAMES = ("Nioh3.exe", "Nioh3-Win64-Shipping.exe")

#: Bytes of the save that the crypto stream does not cover (see crypto.py).
UNCOVERED_TAIL_BYTES = 8


class SaveError(RuntimeError):
    """Raised for any save-file operation failure."""


# --------------------------------------------------------------------------
# Hashing helpers
# --------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    """Return the uppercase SHA-256 of a file's bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


# --------------------------------------------------------------------------
# Crypto backend (exe-first, pure-Python fallback)
# --------------------------------------------------------------------------

def default_crypto_tool(project_root: Path | None = None) -> Path:
    """Locate the bundled reference decrypt executable.

    The packaged ``bin/`` **next to the executable** (frozen build) or next to
    this package (source run) wins over a caller-supplied project root, so the
    backend never depends on the current directory.
    """
    candidates: list[Path] = [paths.default_crypto_exe()]
    if project_root is not None:
        candidates.append(project_root / "bin" / "Nioh_Savefile_decrypt.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "未找到内置的存档加解密组件 bin\\Nioh_Savefile_decrypt.exe；"
        "可使用 --python-crypto 走纯 Python 后端"
    )


class SaveCrypto:
    """File-level transform backed by the reference exe (fast) or Python."""

    __slots__ = ("executable", "_py")

    def __init__(
        self,
        executable: Path | None = None,
        *,
        prefer_python: bool = False,
        project_root: Path | None = None,
    ) -> None:
        if prefer_python:
            # An explicit request always wins, even if an executable was given.
            self.executable: Path | None = None
        else:
            try:
                self.executable = executable or default_crypto_tool(project_root)
            except FileNotFoundError:
                self.executable = None
        self._py = py_crypto.SaveCrypto()

    @property
    def backend_name(self) -> str:
        return f"exe:{self.executable}" if self.executable else "python"

    @staticmethod
    def is_encrypted(data: bytes) -> bool:
        """Return whether ``data`` still looks encrypted (no USR magic)."""
        return py_crypto.SaveCrypto.is_encrypted(data)

    def transform(self, source: Path, output: Path) -> None:
        """Encrypt/decrypt ``source`` into ``output`` (auto-detects state)."""
        if not source.is_file():
            raise SaveError(f"输入存档不存在：{source}")
        if source.resolve() == output.resolve():
            raise SaveError("拒绝在原文件上直接执行加解密")
        if output.exists():
            raise SaveError(f"输出文件已存在，拒绝覆盖：{output}")
        expected_size = source.stat().st_size

        if self.executable is None:
            data = source.read_bytes()
            output.write_bytes(self._py.transform(data))
            return

        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="nioh3-accessory-crypt-") as directory:
            work = Path(directory)
            staged_source = work / "input.bin"
            staged_output = work / "output.bin"
            shutil.copy2(source, staged_source)
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                result = subprocess.run(
                    [
                        str(self.executable),
                        "-i",
                        str(staged_source),
                        "-o",
                        staged_output.name,
                    ],
                    cwd=work,
                    input="\n",
                    text=True,
                    capture_output=True,
                    timeout=300,
                    creationflags=creation_flags,
                )
            except subprocess.TimeoutExpired as error:
                raise SaveError("存档加解密组件超时（300 秒）") from error
            if result.returncode != 0 or not staged_output.is_file():
                raise SaveError(
                    "存档加解密组件执行失败：\n"
                    + result.stdout[-800:]
                    + result.stderr[-400:]
                )
            produced = staged_output.stat().st_size
            if produced != expected_size:
                raise SaveError(
                    "存档加解密组件输出大小异常："
                    f"期望 {expected_size:#x} 字节，实际 {produced:#x} 字节"
                )
            shutil.copy2(staged_output, output)

        final_size = output.stat().st_size
        if final_size != expected_size:
            output.unlink(missing_ok=True)
            raise SaveError(
                f"加解密输出被截断：期望 {expected_size:#x}，实际 {final_size:#x}"
            )

    def decrypt(self, source: Path, output: Path) -> None:
        self.transform(source, output)

    def encrypt(self, source: Path, output: Path) -> None:
        self.transform(source, output)

    def transform_bytes(self, data: bytes) -> bytes:
        """In-memory transform (pure-Python path; used for validation)."""
        return self._py.transform(data)


# --------------------------------------------------------------------------
# Save discovery / identity
# --------------------------------------------------------------------------

def save_root_directory() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "KoeiTecmo" / "NIOH3" / "Savedata"


def save_slot_index_from_path(path: Path) -> int:
    match = SAVE_SLOT_DIRECTORY_PATTERN.fullmatch(path.parent.name)
    if match is None:
        raise SaveError("无法从存档路径识别游戏存档栏位")
    return int(match.group("index"), 10)


def account_id_from_save_path(path: Path) -> int:
    try:
        return int(path.parents[1].name)
    except (ValueError, IndexError) as error:
        raise SaveError("无法从存档路径识别 Steam 账号 ID") from error


def discover_save_paths(root: Path | None = None) -> list[Path]:
    """Return every ``SAVEDATA??/SAVEDATA.BIN`` under the Nioh 3 save root.

    ``root`` overrides the automatic ``%LOCALAPPDATA%\\KoeiTecmo\\...`` lookup,
    which is how ``save_root`` in ``config/editor.json`` is honoured.
    """
    root = Path(root) if root is not None else save_root_directory()
    if not root.is_dir():
        return []
    discovered: list[Path] = []
    for candidate in root.glob("*/SAVEDATA??/SAVEDATA.BIN"):
        try:
            slot = save_slot_index_from_path(candidate)
            account = account_id_from_save_path(candidate)
        except SaveError:
            continue
        if candidate.is_file():
            discovered.append((account, slot, candidate))
    discovered.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in discovered]


# --------------------------------------------------------------------------
# Game process gate
# --------------------------------------------------------------------------

def _windows_process_names() -> set[str] | None:
    """Return running process image names, or ``None`` if unavailable."""
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    th32cs_snapprocess = 0x00000002
    invalid_handle = ctypes.c_void_p(-1).value
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W))
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapprocess, 0)
    if not snapshot or snapshot == invalid_handle:
        return None
    names: set[str] = set()
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(ProcessEntry32W)
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                names.add(str(entry.szExeFile))
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    except OSError:
        return None
    finally:
        kernel32.CloseHandle(snapshot)
    return names


def running_game_processes(names: tuple[str, ...] = GAME_PROCESS_NAMES) -> tuple[str, ...]:
    """Return which known game executables are currently running.

    Returns an empty tuple when the check is unavailable (non-Windows or the
    process snapshot failed), so an unsupported host is never blocked.
    """
    processes = _windows_process_names()
    if processes is None:
        return ()
    lowered = {name.lower() for name in processes}
    return tuple(name for name in names if name.lower() in lowered)


class GameRunningError(SaveError):
    """Raised when the game is running and the caller did not opt in."""


#: Shown before every save-file write.  Edits land in the file on disk, so the
#: running game must not hold that save in memory -- otherwise its next save
#: overwrites our edits (or the two fight over the slot).  Kept as one constant
#: so the CLI, the GUI and the error message below always say the same thing.
SAVE_WRITE_REQUIREMENT = (
    "写入前置条件：请先【完全退出游戏】，或退回到游戏【标题界面】"
    "（即当前没有读取任何存档、不在游戏内）。\n"
    "在游戏内（存档已载入内存）写入，游戏之后保存会用内存数据覆盖本次修改，"
    "可能造成修改丢失或存档冲突。"
)


def require_game_not_running(*, allow_running: bool = False) -> tuple[str, ...]:
    """Fail closed when Nioh 3 is running: it may overwrite our edits.

    Returns the detected process names (empty when the game is not running) so
    callers can report the gate state without probing the process list twice.
    """
    running = running_game_processes()
    if running and not allow_running:
        raise GameRunningError(
            "检测到仁王3 正在运行（"
            + "、".join(running)
            + "）。\n"
            + SAVE_WRITE_REQUIREMENT
            + "\n如已退到标题界面且确认要写入，请显式使用 --at-title-screen"
              "（旧名 --force-while-running）；GUI 里对应窗口底部那个"
              "「我确认：游戏正在运行，但停留在标题界面」勾选框。"
        )
    return running


# --------------------------------------------------------------------------
# Quiescence fingerprinting + durable write
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SaveFileFingerprint:
    role: str
    path: Path
    exists: bool
    size: int
    sha256: str


def _related_save_files(save_path: Path) -> tuple[tuple[str, Path], ...]:
    """Main save, its game-side backup, and the account-level system save."""
    return (
        ("main_save", save_path),
        ("game_backup", save_path.parent / "BACKUP.BIN"),
        ("system_save", save_path.parent.parent / "SYSTEMSAVEDATA00" / "SAVEDATA.BIN"),
    )


def _fingerprint_one(role: str, path: Path) -> SaveFileFingerprint:
    """Hash one file, rejecting a read that raced with a writer."""
    try:
        before = path.stat()
    except OSError:
        return SaveFileFingerprint(role=role, path=path, exists=False, size=0, sha256="")
    if not stat.S_ISREG(before.st_mode):
        return SaveFileFingerprint(role=role, path=path, exists=False, size=0, sha256="")
    digest = sha256_file(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise SaveError(
            "SAVE_SYNC_ACTIVE: 存档在读取期间发生变化，拒绝写入。"
            "请回到标题界面停留片刻后重试。"
        )
    return SaveFileFingerprint(
        role=role, path=path, exists=True, size=after.st_size, sha256=digest
    )


def capture_related_save_fingerprints(save_path: Path) -> tuple[SaveFileFingerprint, ...]:
    return tuple(
        _fingerprint_one(role, path) for role, path in _related_save_files(save_path)
    )


def capture_quiescent_save_fingerprints(
    save_path: Path,
    *,
    interval_seconds: float = SAVE_QUIESCENCE_SECONDS,
) -> tuple[SaveFileFingerprint, ...]:
    """Require a quiet save generation before any external write."""
    first = capture_related_save_fingerprints(save_path)
    if interval_seconds > 0:
        time.sleep(interval_seconds)
    second = capture_related_save_fingerprints(save_path)
    if first != second:
        raise SaveError(
            "SAVE_SYNC_ACTIVE: Nioh 3 正在同步存档文件，拒绝写入。"
            "请回到标题界面停留片刻后重试。"
        )
    return second


def _replace_file_durable(source: Path, target: Path) -> None:
    """Atomically replace ``target`` with ``source`` and flush the metadata."""
    if os.name == "nt":
        move_file_ex = ctypes.windll.kernel32.MoveFileExW
        move_file_ex.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint)
        move_file_ex.restype = ctypes.c_int
        replace_existing = 0x1
        write_through = 0x8
        if not move_file_ex(str(source), str(target), replace_existing | write_through):
            raise ctypes.WinError()
        return
    os.replace(source, target)


def _copy_file_durable(source: Path, destination: Path) -> None:
    """Copy bytes to ``destination``, fsync them, then swap the name in."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        _replace_file_durable(temporary, destination)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _write_json_durable(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace_file_durable(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------
# Backup / restore
# --------------------------------------------------------------------------

def backup_directory_for(save_path: Path, state_root: Path) -> Path:
    account = account_id_from_save_path(save_path)
    slot = save_slot_index_from_path(save_path)
    return state_root / BACKUP_SUBDIRECTORY_NAME / f"account-{account}" / f"slot-{slot:02d}"


def _fingerprint_by_role(
    fingerprints: tuple[SaveFileFingerprint, ...],
) -> dict[str, SaveFileFingerprint]:
    return {fingerprint.role: fingerprint for fingerprint in fingerprints}


def create_backup(
    save_path: Path,
    *,
    state_root: Path,
    crypto: SaveCrypto,
    expected_fingerprints: tuple[SaveFileFingerprint, ...] | None = None,
) -> Path:
    """Back up the main save (plaintext) plus a manifest; return backup dir."""
    if expected_fingerprints is None:
        expected_fingerprints = capture_quiescent_save_fingerprints(save_path)
    roles = _fingerprint_by_role(expected_fingerprints)

    backup_dir = backup_directory_for(save_path, state_root)
    backup_dir.mkdir(parents=True, exist_ok=True)
    unique = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    plain = backup_dir / f"SAVEDATA-{unique}-plain.bin"

    with tempfile.TemporaryDirectory(prefix="nioh3-accessory-bak-") as directory:
        decrypted_path = Path(directory) / "decrypted.bin"
        crypto.decrypt(save_path, decrypted_path)
        _copy_file_durable(decrypted_path, plain)

    manifest = {
        "backup_manifest_schema": BACKUP_MANIFEST_SCHEMA,
        "save_schema_profile": SAVE_SCHEMA_PROFILE,
        "steam_account_id": account_id_from_save_path(save_path),
        "save_slot_index": save_slot_index_from_path(save_path),
        "created_at": unique,
        "main_save_sha256": roles.get(
            "main_save", SaveFileFingerprint("main_save", save_path, False, 0, "")
        ).sha256,
        "plain_backup": plain.name,
        "plain_backup_sha256": sha256_file(plain),
    }
    _write_json_durable(backup_dir / "backup-manifest.json", manifest)
    return backup_dir


#: ``SAVEDATA-<created_at>-plain.bin`` as written by :func:`create_backup`.
BACKUP_PLAIN_PATTERN = re.compile(
    r"^SAVEDATA-(?P<created>\d{8}-\d{6}-[0-9a-f]+)-plain\.bin$")


@dataclass(frozen=True)
class BackupEntry:
    """One plaintext backup found on disk, with its integrity verdict."""

    plain_path: Path
    created_at: str
    plain_size: int
    plain_sha256: str
    expected_sha256: str = ""
    original_save_sha256: str = ""
    account_id: int | None = None
    slot_index: int | None = None
    manifest_path: Path | None = None

    @property
    def integrity_ok(self) -> bool:
        """True when the file still matches the hash recorded beside it."""
        return not self.expected_sha256 or self.expected_sha256 == self.plain_sha256

    @property
    def when(self) -> str:
        """``YYYY-mm-dd HH:MM:SS`` when the name carries a timestamp."""
        match = re.match(r"^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})",
                         self.created_at)
        if match is None:
            return self.created_at
        year, month, day, hour, minute, second = match.groups()
        return f"{year}-{month}-{day} {hour}:{minute}:{second}"

    def describe(self) -> str:
        state = "" if self.integrity_ok else "  ⚠ 校验不符"
        size = f"{self.plain_size / 1024 / 1024:.1f} MB"
        return f"{self.when}  {size}{state}"


def list_backups(save_path: Path, state_root: Path) -> tuple[BackupEntry, ...]:
    """Every plaintext backup for one save, newest first.

    A backup without a readable manifest is still listed (with
    ``integrity_ok=False``): refusing to show it would hide the only copy a user
    has, and the restore path re-checks the bytes anyway.
    """
    directory = backup_directory_for(save_path, state_root)
    if not directory.is_dir():
        return ()

    manifest_path = directory / "backup-manifest.json"
    manifest: dict[str, object] = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                manifest = loaded
        except (OSError, ValueError):
            manifest = {}

    entries: list[BackupEntry] = []
    for path in sorted(directory.glob("*-plain.bin")):
        if not path.is_file():
            continue
        match = BACKUP_PLAIN_PATTERN.match(path.name)
        created = match.group("created") if match else path.stem
        expected = ""
        recorded = manifest.get("plain_backup")
        if isinstance(recorded, str) and recorded == path.name:
            expected = str(manifest.get("plain_backup_sha256") or "")
        try:
            size = path.stat().st_size
        except OSError:
            continue
        entries.append(BackupEntry(
            plain_path=path,
            created_at=created,
            plain_size=size,
            plain_sha256=sha256_file(path),
            expected_sha256=expected,
            original_save_sha256=str(manifest.get("main_save_sha256") or ""),
            account_id=_optional_int(manifest.get("steam_account_id")),
            slot_index=_optional_int(manifest.get("save_slot_index")),
            manifest_path=manifest_path if manifest else None,
        ))
    entries.sort(key=lambda entry: (entry.created_at, entry.plain_path.name),
                 reverse=True)
    return tuple(entries)


def _optional_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def validate_user_save_bytes(data: bytes, *, what: str = "存档数据") -> bytes:
    """Require a decrypted USR save (magic + exact size), else raise."""
    if not data.startswith(b"RNNUSR"):
        raise SaveError(
            f"{what}不是有效的仁王3 存档（缺少 RNNUSR 魔数）。"
            "请确认选择的是 USR 存档而非系统存档。"
        )
    if len(data) != py_crypto.USER_SAVE_SIZE:
        raise SaveError(
            f"{what}大小异常：期望 {py_crypto.USER_SAVE_SIZE:#x} 字节，"
            f"实际 {len(data):#x} 字节"
        )
    return data


def read_backup_plaintext(entry: BackupEntry) -> bytes:
    """Read and validate one plaintext backup, refusing a corrupted copy."""
    try:
        data = entry.plain_path.read_bytes()
    except OSError as error:
        raise SaveError(f"无法读取备份文件 {entry.plain_path}: {error}") from error
    if not entry.integrity_ok:
        raise SaveError(
            f"备份文件已损坏或与清单记录不一致，拒绝用它恢复：\n{entry.plain_path}\n"
            f"期望 SHA-256 {entry.expected_sha256}\n实际 SHA-256 {entry.plain_sha256}"
        )
    return validate_user_save_bytes(data, what=f"备份 {entry.plain_path.name} ")


def restore_save_from_bytes(save_path: Path, original: bytes) -> None:
    """Durably write ``original`` bytes back to ``save_path`` (rollback)."""
    with tempfile.TemporaryDirectory(prefix="nioh3-accessory-restore-") as directory:
        staged = Path(directory) / save_path.name
        staged.write_bytes(original)
        with staged.open("r+b") as handle:
            os.fsync(handle.fileno())
        _copy_file_durable(staged, save_path)


# --------------------------------------------------------------------------
# Decrypt / encrypt convenience
# --------------------------------------------------------------------------

def decrypt_save_to_bytes(save_path: Path, crypto: SaveCrypto) -> bytes:
    """Return the decrypted bytes of one save file."""
    with tempfile.TemporaryDirectory(prefix="nioh3-accessory-read-") as directory:
        decrypted_path = Path(directory) / "decrypted.bin"
        crypto.decrypt(save_path, decrypted_path)
        data = decrypted_path.read_bytes()
    return validate_user_save_bytes(data, what="解密结果")


def _verify_encrypted_file(
    encrypted_path: Path,
    expected_plain: bytes,
    crypto: SaveCrypto,
    scratch: Path,
) -> None:
    """Decrypt ``encrypted_path`` and require the crypto region to match."""
    check_path = scratch / f"verify-{uuid.uuid4().hex}.bin"
    crypto.decrypt(encrypted_path, check_path)
    recovered = check_path.read_bytes()
    check_path.unlink(missing_ok=True)
    covered = len(expected_plain) - UNCOVERED_TAIL_BYTES
    if len(recovered) != len(expected_plain):
        raise SaveError(
            f"写入校验失败：解密后大小 {len(recovered):#x} != 期望 {len(expected_plain):#x}"
        )
    if recovered[:covered] != expected_plain[:covered]:
        raise SaveError("写入校验失败：重新加密后的存档无法还原为修改后的数据，已中止写入")


def write_encrypted_save(
    save_path: Path,
    decrypted_bytes: bytes,
    *,
    crypto: SaveCrypto,
    expected_fingerprints: tuple[SaveFileFingerprint, ...],
    verify: bool = True,
    tail: bytes | None = None,
) -> str:
    """Atomically replace the main save with the re-encrypted data.

    Flow: encrypt to a staging file -> force the trailing bytes -> verify the
    staged ciphertext decrypts back to ``decrypted_bytes`` -> re-check the
    quiescence fingerprints -> atomically replace -> verify the installed file,
    rolling back the original bytes if the check fails.  Returns the new file's
    SHA-256.

    The final :data:`UNCOVERED_TAIL_BYTES` bytes are not covered by the crypto
    stream, and **the reference tool zeroes them on encryption** (the pure-Python
    backend keeps them, so the two backends disagree).  They are therefore always
    written explicitly:

    * ``tail=None`` -- a normal edit: keep whatever the game last wrote there.
    * ``tail=b"..."`` -- a **restore**: write the tail recorded in the backup, so
      the file comes back exactly as it was.
    """
    if not decrypted_bytes.startswith(b"RNNUSR"):
        raise SaveError("内部错误：待写入数据不是有效的解密存档 (RNNUSR)")
    if len(decrypted_bytes) != py_crypto.USER_SAVE_SIZE:
        raise SaveError(
            f"内部错误：待写入数据大小 {len(decrypted_bytes):#x} 不是标准存档大小"
        )
    if tail is not None and len(tail) != UNCOVERED_TAIL_BYTES:
        raise SaveError(
            f"内部错误：尾字节长度 {len(tail)} != {UNCOVERED_TAIL_BYTES}"
        )

    current = capture_quiescent_save_fingerprints(save_path)
    if tuple(expected_fingerprints) != current:
        raise SaveError(
            "SAVE_SYNC_ACTIVE: 存档在准备期间发生变化，拒绝写入。"
            "请回到标题界面停留片刻后重试。"
        )

    # Keep the original bytes for rollback (and for the trailing-8 contract).
    original_encrypted = save_path.read_bytes()

    if tail is None:
        if len(original_encrypted) == len(decrypted_bytes):
            tail = original_encrypted[-UNCOVERED_TAIL_BYTES:]
        else:  # pragma: no cover - defensive: unexpected on-disk size
            tail = decrypted_bytes[-UNCOVERED_TAIL_BYTES:]

    with tempfile.TemporaryDirectory(prefix="nioh3-accessory-write-") as directory:
        work = Path(directory)
        plain_path = work / "patched.bin"
        enc_path = work / "encrypted.bin"
        plain_path.write_bytes(decrypted_bytes)
        crypto.encrypt(plain_path, enc_path)

        with enc_path.open("r+b") as handle:
            handle.seek(-UNCOVERED_TAIL_BYTES, os.SEEK_END)
            handle.write(tail)

        if verify:
            _verify_encrypted_file(enc_path, decrypted_bytes, crypto, work)

        _copy_file_durable(enc_path, save_path)

        if verify:
            try:
                _verify_encrypted_file(save_path, decrypted_bytes, crypto, work)
            except SaveError:
                # Put the untouched original back before surfacing the failure.
                restore_save_from_bytes(save_path, original_encrypted)
                raise
    return sha256_file(save_path)
