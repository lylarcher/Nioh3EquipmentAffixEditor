"""Attach a windowed build to the console of the process that started it.

The frozen executable is built for the windowed subsystem, so it never creates a
console window of its own: double-clicking it shows only the GUI, and there is no
black box that could be closed (which used to take the program down with it).

A CLI subcommand still needs somewhere to print.  ``AttachConsole`` borrows the
console of the starting process — a terminal the user opened on purpose — and the
standard streams are rebound to it.  When there is no parent console (Explorer
double-click, scheduled task, …) the call fails and the build simply stays silent.

Best effort by design: none of this may raise, and nothing here is required for the
GUI to work.  On a non-frozen run (``python launch_editor.py``) there is already a
console, so this is a no-op.
"""

from __future__ import annotations

import os
import sys

#: ``AttachConsole`` argument meaning "the console of our parent process".
ATTACH_PARENT_PROCESS = -1


def has_console() -> bool:
    """True when this process already has usable standard streams."""
    return sys.stdout is not None and sys.stderr is not None


#: ``GetStdHandle`` ids and the stream each one feeds.
_STD_HANDLES = (("stdin", -10, "r"), ("stdout", -11, "w"), ("stderr", -12, "w"))
_INVALID_HANDLE_VALUE = -1


def _adopt_inherited_streams(kernel32) -> bool:
    """Use standard handles the caller gave us (a pipe or a redirected file).

    A GUI-subsystem process still inherits these when someone captures its output
    (``exe version | Out-String``, a test runner, CI, …), and that is exactly the
    case where the output must *not* be sent to a console instead.
    """
    import msvcrt  # noqa: PLC0415 - Windows-only, imported on demand

    adopted = False
    for name, handle_id, mode in _STD_HANDLES:
        if getattr(sys, name) is not None:
            continue
        try:
            handle = kernel32.GetStdHandle(handle_id)
            if handle in (0, None, _INVALID_HANDLE_VALUE):
                continue
            # 0x4000 = _O_NOINHERIT (the handle is already ours; do not leak it)
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY if mode == "r"
                                               else os.O_WRONLY | 0x4000)
            if descriptor < 0:
                continue
            stream = os.fdopen(descriptor, mode, encoding="utf-8",
                               errors="replace", buffering=1)
        except (OSError, ValueError, OverflowError):
            continue
        setattr(sys, name, stream)
        adopted = adopted or name != "stdin"
    return adopted


def attach_parent_console() -> bool:
    """Give a frozen windowed build working stdout/stderr/stdin.

    Two sources, in order: the standard handles the caller already redirected
    (pipes, files), then the console of the process that started us — a terminal
    the user opened on purpose.  With neither (Explorer double-click, task
    scheduler) every ``print`` is simply dropped, which is what the GUI wants.

    Best effort by design: returns whether anything was adopted and never raises.
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return False
    if has_console():
        return False
    try:
        import ctypes  # noqa: PLC0415 - Windows-only, imported on demand

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except Exception:  # noqa: BLE001 - best effort, never fatal
        return False
    try:
        if _adopt_inherited_streams(kernel32):
            return True
    except Exception:  # noqa: BLE001 - best effort, never fatal
        pass
    try:
        if not kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            return False
    except Exception:  # noqa: BLE001 - best effort, never fatal
        return False
    for name, device, mode in (("stdin", "CONIN$", "r"),
                               ("stdout", "CONOUT$", "w"),
                               ("stderr", "CONOUT$", "w")):
        try:
            stream = open(device, mode, encoding="utf-8", errors="replace",
                          buffering=1)
        except OSError:
            continue
        setattr(sys, name, stream)
    return has_console()
