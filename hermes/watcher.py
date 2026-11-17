"""Folder scanning worker – lives in its own QThread so the UI never freezes."""
from __future__ import annotations

import logging
import os
import shutil
import time

import pythoncom
import pywintypes
import win32con
import win32file
from PySide6.QtCore import QObject, Signal, Slot

from .config import Settings
from .printing import DocumentPrinter, describe_error, list_printers

log = logging.getLogger("hermes")


# --------------------------------------------------------------------------- file helpers
def _is_ignored(name: str) -> bool:
    low = name.lower()
    return low.startswith("~$") or low.startswith(".") or low in ("desktop.ini", "thumbs.db")


def _is_locked(path: str) -> bool:
    """True if another process (Word, Drive/OneDrive sync, etc.) has the file open."""
    try:
        handle = win32file.CreateFile(path, win32con.GENERIC_READ, 0, None,
                                      win32con.OPEN_EXISTING, win32con.FILE_ATTRIBUTE_NORMAL, None)
        handle.Close()
        return False
    except pywintypes.error:
        return True


def file_is_ready(path: str, stable_seconds: int) -> tuple[bool, str]:
    try:
        st = os.stat(path)
    except OSError:
        return False, "cannot read file"
    if st.st_size == 0:
        return False, "file is empty / still syncing"
    age = time.time() - st.st_mtime
    if age < stable_seconds:
        return False, f"modified {max(0, int(age))}s ago"
    if _is_locked(path):
        return False, "file is open in another program or still syncing"
    return True, ""


def move_to_subfolder(path: str, folder_name: str) -> str:
    dest_dir = os.path.join(os.path.dirname(path), folder_name)
    os.makedirs(dest_dir, exist_ok=True)
    name = os.path.basename(path)
    dest = os.path.join(dest_dir, name)
    if os.path.exists(dest):
        stem, ext = os.path.splitext(name)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(dest_dir, f"{stem}_{stamp}{ext}")
        n = 2
        while os.path.exists(dest):
            dest = os.path.join(dest_dir, f"{stem}_{stamp}_{n}{ext}")
            n += 1
    last_error = None
    for _ in range(10):  # Office/sync/Acrobat may hold the file briefly after printing
        try:
            shutil.move(path, dest)
            return dest
        except (PermissionError, OSError) as e:
            last_error = e
            time.sleep(3)
    raise last_error  # type: ignore[misc]


# --------------------------------------------------------------------------- worker
class FolderWatcher(QObject):
    scan_started = Signal()
    scan_finished = Signal(int, int)   # printed, failed (this pass)
    file_printed = Signal(str)
    file_failed = Signal(str, str)     # name, reason

    def __init__(self):
        super().__init__()
        self._attempts: dict[str, int] = {}
        self._pending_move: set[str] = set()     # printed OK but move failed – never reprint these
        self._waiting_notified: set[str] = set()
        self._busy = False
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    @Slot(object)
    def scan(self, settings: Settings) -> None:
        if self._busy:
            return
        self._busy = True
        self.scan_started.emit()
        printed = failed = 0
        com_ready = False
        try:
            pythoncom.CoInitialize()
            com_ready = True
            printed, failed = self._scan(settings)
        except Exception as e:
            log.error("Unexpected error during folder check: %s", describe_error(e))
        finally:
            if com_ready:
                pythoncom.CoUninitialize()
            self._busy = False
            self.scan_finished.emit(printed, failed)

    def _scan(self, s: Settings) -> tuple[int, int]:
        folder = s.watch_folder.strip()
        if not folder:
            log.warning("No watch folder configured – open Settings and choose one.")
            return 0, 0
        if not os.path.isdir(folder):
            log.warning("Watch folder not found: %s (drive not synced/mounted?)", folder)
            return 0, 0
        printer = s.printer.strip()
        if not printer:
            log.warning("No printer selected – open Settings and choose one.")
            return 0, 0
        if printer not in list_printers():
            log.error("Printer '%s' is not installed on this PC.", printer)
            return 0, 0

        exts = set(s.extension_list())
        files: list[str] = []
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    if not entry.is_file(follow_symlinks=False) or _is_ignored(entry.name):
                        continue
                    if os.path.splitext(entry.name)[1].lower() in exts:
                        files.append(entry.path)
        except OSError as e:
            log.error("Cannot read watch folder: %s", describe_error(e))
            return 0, 0

        # Forget state for files that are gone
        present = {os.path.normcase(p) for p in files}
        for store in (self._pending_move, self._waiting_notified):
            store.intersection_update(present)
        for k in list(self._attempts):
            if k not in present:
                del self._attempts[k]

        if not files:
            return 0, 0

        def mtime(p: str) -> float:
            try:
                return os.path.getmtime(p)
            except OSError:
                return 0.0

        files.sort(key=mtime)  # oldest first
        dp = DocumentPrinter(printer)
        printed = failed = 0

        for path in files:
            if self._stop:
                break
            name = os.path.basename(path)
            key = os.path.normcase(path)

            # Printed in an earlier pass but couldn't be moved – just retry the move.
            if key in self._pending_move:
                try:
                    move_to_subfolder(path, s.processed_folder)
                    self._pending_move.discard(key)
                    log.info("Moved previously printed '%s' to %s.", name, s.processed_folder)
                except Exception as e:
                    log.error("Still cannot move '%s': %s", name, describe_error(e))
                continue

            ready, reason = file_is_ready(path, s.stable_seconds)
            if not ready:
                if key not in self._waiting_notified:
                    log.info("Waiting on '%s' (%s) – will retry next check.", name, reason)
                    self._waiting_notified.add(key)
                continue
            self._waiting_notified.discard(key)

            log.info("Printing '%s' → %s …", name, printer)
            try:
                method = dp.print_file(path)
            except Exception as e:
                reason = describe_error(e)
                n = self._attempts.get(key, 0) + 1
                self._attempts[key] = n
                if n >= s.max_attempts:
                    self._attempts.pop(key, None)
                    failed += 1
                    try:
                        move_to_subfolder(path, s.failed_folder)
                        log.error("FAILED '%s' after %d attempt(s): %s → moved to %s.",
                                  name, n, reason, s.failed_folder)
                    except Exception as me:
                        log.error("FAILED '%s' after %d attempt(s): %s (and could not move it: %s)",
                                  name, n, reason, describe_error(me))
                    self.file_failed.emit(name, reason)
                else:
                    log.warning("Attempt %d/%d failed for '%s': %s", n, s.max_attempts, name, reason)
                continue

            self._attempts.pop(key, None)
            printed += 1
            try:
                move_to_subfolder(path, s.processed_folder)
                log.info("Printed '%s' via %s → moved to %s.", name, method, s.processed_folder)
            except Exception as e:
                self._pending_move.add(key)
                log.error("Printed '%s' but could not move it yet (%s). Will retry the move without reprinting.",
                          name, describe_error(e))
            self.file_printed.emit(name)

        return printed, failed
