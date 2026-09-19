"""Where the application lives on disk, in both source and frozen layouts.

Two layouts are supported:

* **source** -- running from a checkout: ``python launch_editor.py``.  The
  project root is the directory above this package, and resources (``data/``,
  ``bin/``, ``third_party/``) sit next to the sources.
* **frozen** -- running from a single-file executable built by PyInstaller.
  ``sys.executable`` is the ``.exe``; resources are extracted **next to it** by
  :mod:`nioh3_accessory_editor.bootstrap`, so users can read and edit the affix
  catalogue, the configuration file and the bundled crypto helper.

Nothing here touches the filesystem beyond ``stat``-level checks; discovery and
extraction live in :mod:`nioh3_accessory_editor.bootstrap`.
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = [
    "BUNDLED_PAYLOAD_NAME",
    "EXTRACTION_MANIFEST_NAME",
    "RESOURCE_DIRECTORIES",
    "application_root",
    "bundle_root",
    "default_catalog_path",
    "default_config_path",
    "default_crypto_exe",
    "default_state_root",
    "is_frozen",
    "resource_path",
    "resource_root",
]

#: Name of the zip embedded in the executable (see ``tools/make_payload.py``).
BUNDLED_PAYLOAD_NAME = "app-payload.zip"

#: Records which files this application has written next to the executable.
EXTRACTION_MANIFEST_NAME = ".extracted-manifest.json"

#: Directory names the payload populates next to the executable.
RESOURCE_DIRECTORIES = ("bin", "config", "data", "third_party")


def is_frozen() -> bool:
    """True when running from a built executable rather than from sources."""
    return bool(getattr(sys, "frozen", False))


def application_root() -> Path:
    """Directory the user sees as "the application".

    Frozen: the directory holding the ``.exe`` (never a temporary extraction
    directory).  Source: the checkout root.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def bundle_root() -> Path | None:
    """Temporary directory the bootloader unpacked *its own* libraries into.

    Only meaningful in a frozen build (PyInstaller's ``sys._MEIPASS``).  It is
    used to *read* the embedded payload, never as the live resource root.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    if is_frozen():  # pragma: no cover - defensive: frozen without _MEIPASS
        return Path(sys.executable).resolve().parent
    return None


def resource_root() -> Path:
    """Directory that resources (``data/``, ``bin/``, ...) are read from.

    This is always :func:`application_root`, i.e. the side-by-side tree: in a
    frozen build, :func:`~nioh3_accessory_editor.bootstrap.ensure_side_by_side`
    guarantees the files exist there.
    """
    return application_root()


def resource_path(*parts: str) -> Path:
    """Return ``resource_root()/parts...`` as a path (not checked for existence)."""
    return resource_root().joinpath(*parts)


def default_state_root() -> Path:
    """Where mutable state (backups) goes when the user did not choose.

    Frozen builds default to the executable's directory, so a double-clicked
    shortcut never scatters backups into whatever the working directory happens
    to be.  Source runs keep the historical behaviour (current directory).
    """
    if is_frozen():
        return application_root()
    return Path.cwd()


def default_crypto_exe() -> Path:
    """Bundled crypto helper next to the resources."""
    return resource_path("bin", "Nioh_Savefile_decrypt.exe")


def default_catalog_path() -> Path:
    """Bundled accessory affix catalogue."""
    return resource_path("data", "accessory_affixes.json")


def default_config_path() -> Path:
    """User-editable configuration file."""
    return resource_path("config", "editor.json")
