"""Self-extraction of the files that belong next to the executable.

A frozen build is a **single file**: the configuration file (``config/``), the
affix catalogue (``data/``), the bundled crypto helper (``bin/``) and the source
data (``third_party/``) travel inside the executable as ``app-payload.zip``.
On first run they are written **next to the executable**, into the same relative
directories they occupy in a source checkout, and from then on they are ordinary
user-editable files.

Rules that keep this safe and predictable:

* A file is written only when it is missing, or when its current contents are
  still exactly what a previous run wrote (tracked in
  ``.extracted-manifest.json``) and the payload has something newer.
* A file the user edited is **never** overwritten; it is reported as ``kept``.
* Nothing is ever deleted, and entries are validated against path traversal.
* If the executable's directory is not writable the payload directory is used
  read-only instead, and the reason is reported rather than raised.

Everything is expressed as a pure function of two paths (payload zip, target
directory) so it can be unit-tested without building an executable.
"""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

__all__ = [
    "ExtractionReport",
    "MANIFEST_SCHEMA",
    "PAYLOAD_INDEX_NAME",
    "PAYLOAD_SCHEMA",
    "ensure_once",
    "ensure_side_by_side",
    "find_payload",
    "last_report",
    "reset_for_tests",
]

PAYLOAD_SCHEMA = "nioh3-accessory-editor-payload/v1"
MANIFEST_SCHEMA = "nioh3-accessory-editor-extracted/v1"

#: Index of the payload's own contents, first entry in the zip.
PAYLOAD_INDEX_NAME = "payload.json"

_CHUNK = 1 << 20
_DONE = False
_LAST_REPORT: "ExtractionReport | None" = None


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


@dataclass(slots=True)
class ExtractionReport:
    """What happened during one extraction pass."""

    target: Path
    payload: Path | None = None
    writable: bool = True
    reason: str = ""
    extracted: list[str] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.extracted or self.refreshed)

    @property
    def first_run(self) -> bool:
        return bool(self.extracted)

    def summary(self) -> str:
        parts = []
        if self.extracted:
            parts.append(f"解压 {len(self.extracted)} 个文件")
        if self.refreshed:
            parts.append(f"更新 {len(self.refreshed)} 个")
        if self.kept:
            parts.append(f"保留用户修改 {len(self.kept)} 个")
        if self.failed:
            parts.append(f"失败 {len(self.failed)} 个")
        if not parts:
            parts.append("无需改动")
        text = f"{self.target}: " + "，".join(parts)
        if not self.writable and self.reason:
            text += f"（{self.reason}）"
        return text


def find_payload(explicit: Path | None = None) -> Path | None:
    """Locate the embedded payload zip, or ``None`` when there is none."""
    if explicit is not None:
        return explicit if explicit.is_file() else None
    candidates = []
    bundle = paths.bundle_root()
    if bundle is not None:
        candidates.append(bundle / paths.BUNDLED_PAYLOAD_NAME)
    candidates.append(paths.application_root() / paths.BUNDLED_PAYLOAD_NAME)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _read_manifest(target: Path) -> dict:
    path = target / paths.EXTRACTION_MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("schema") != MANIFEST_SCHEMA:
        return {}
    files = data.get("files")
    return files if isinstance(files, dict) else {}


def _write_manifest(target: Path, files: dict[str, str], payload: Path,
                    payload_index: dict) -> None:
    document = {
        "schema": MANIFEST_SCHEMA,
        "payload": payload.name,
        "payload_version": payload_index.get("version", ""),
        "payload_commit": payload_index.get("commit", ""),
        "files": files,
    }
    (target / paths.EXTRACTION_MANIFEST_NAME).write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _safe_relative(name: str) -> Path | None:
    """Reject absolute paths, drive letters and ``..`` traversal."""
    candidate = Path(name)
    if candidate.is_absolute() or candidate.drive:
        return None
    if any(part in ("", ".", "..") for part in candidate.parts):
        return None
    return candidate


def _load_payload_index(archive: zipfile.ZipFile) -> dict:
    try:
        raw = archive.read(PAYLOAD_INDEX_NAME)
    except KeyError:
        return {}
    try:
        index = json.loads(raw.decode("utf-8"))
    except ValueError:
        return {}
    return index if isinstance(index, dict) else {}


def ensure_side_by_side(payload: Path | None = None,
                        target: Path | None = None) -> ExtractionReport:
    """Extract the payload's files next to the executable.

    Never raises for filesystem reasons: a read-only target is reported through
    ``report.writable``/``report.reason`` so the caller can degrade gracefully.
    """
    target = Path(target) if target is not None else paths.resource_root()
    report = ExtractionReport(target=target)

    payload_path = find_payload(payload)
    if payload_path is None:
        report.writable = False
        report.reason = "未找到内嵌载荷（源码运行属正常）"
        return report
    report.payload = payload_path

    try:
        archive = zipfile.ZipFile(payload_path)
    except (OSError, zipfile.BadZipFile) as error:
        report.writable = False
        report.reason = f"载荷无法读取: {error}"
        return report

    with archive:
        index = _load_payload_index(archive)
        entries = index.get("files") if isinstance(index.get("files"), dict) else {}
        names = sorted(n for n in archive.namelist()
                       if n != PAYLOAD_INDEX_NAME and not n.endswith("/"))
        if not entries:
            entries = {name: {} for name in names}

        recorded = _read_manifest(target)
        new_records: dict[str, str] = dict(recorded)
        for name in names:
            relative = _safe_relative(name)
            if relative is None:
                report.failed.append(name)
                continue
            destination = target / relative
            meta = entries.get(name, {})
            payload_hash = meta.get("sha256") if isinstance(meta, dict) else None
            try:
                data = archive.read(name)
            except (OSError, zipfile.BadZipFile) as error:
                report.failed.append(f"{name}: {error}")
                continue
            if payload_hash is None:
                payload_hash = _sha256_bytes(data)
            self_made = recorded.get(name)

            if not destination.exists():
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
                except OSError as error:
                    report.writable = False
                    report.reason = f"{destination.parent} 不可写: {error}"
                    report.failed.append(name)
                    break
                report.extracted.append(name)
                new_records[name] = payload_hash
                continue

            current = _sha256_file(destination)
            if current is None:
                report.failed.append(f"{name}: 无法读取现有文件")
                continue
            if current == payload_hash:
                report.unchanged.append(name)
                new_records[name] = payload_hash
                continue
            if self_made is not None and current == self_made:
                # Still exactly what we wrote last time: safe to update.
                try:
                    destination.write_bytes(data)
                except OSError as error:
                    report.writable = False
                    report.reason = f"{destination} 不可写: {error}"
                    report.failed.append(name)
                    break
                report.refreshed.append(name)
                new_records[name] = payload_hash
                continue
            # The user edited (or replaced) it: never clobber.
            report.kept.append(name)
            new_records[name] = current

        if report.writable and (report.extracted or report.refreshed
                                or not (target / paths.EXTRACTION_MANIFEST_NAME).exists()):
            try:
                _write_manifest(target, new_records, payload_path, index)
            except OSError as error:
                report.reason = f"无法写入清单: {error}"
    return report


def ensure_once(payload: Path | None = None,
                target: Path | None = None) -> ExtractionReport | None:
    """Run :func:`ensure_side_by_side` at most once per process.

    Returns ``None`` for source runs (nothing to extract) or when the pass has
    already happened, so entry points can call it unconditionally.
    """
    global _DONE, _LAST_REPORT
    if _DONE:
        return None
    _DONE = True
    if not paths.is_frozen() and payload is None:
        _LAST_REPORT = None
        return None
    report = ensure_side_by_side(payload=payload, target=target)
    _LAST_REPORT = report
    if report.first_run:
        print(f"已解压随附文件到 {report.target}", file=sys.stderr)
    elif report.refreshed:
        print(f"已更新随附文件（{len(report.refreshed)} 个）于 {report.target}",
              file=sys.stderr)
    elif not report.writable and report.reason:
        print(f"提示：随附文件未解压（{report.reason}）", file=sys.stderr)
    return report


def last_report() -> ExtractionReport | None:
    """Report from :func:`ensure_once`, for diagnostics and the GUI."""
    return _LAST_REPORT


def reset_for_tests() -> None:
    """Forget the once-per-process latch (tests only)."""
    global _DONE, _LAST_REPORT
    _DONE = False
    _LAST_REPORT = None
