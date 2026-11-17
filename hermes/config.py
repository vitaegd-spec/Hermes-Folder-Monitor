"""Settings persistence and path helpers for Hermes."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields

APP_NAME = "Hermes"
APP_VERSION = "1.0.2"


def app_data_dir() -> str:
    """%APPDATA%\\Hermes – where settings and logs live."""
    root = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(root, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def base_dir() -> str:
    """Folder containing Hermes.exe (frozen) or the project root (script)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_path(rel: str) -> str:
    """Path to bundled resources (works for PyInstaller one-file builds)."""
    root = getattr(sys, "_MEIPASS", None) or base_dir()
    return os.path.join(root, rel)


CONFIG_FILE = os.path.join(app_data_dir(), "settings.json")
LOG_FILE = os.path.join(app_data_dir(), "hermes.log")
LOCK_FILE = os.path.join(app_data_dir(), "hermes.lock")


@dataclass
class Settings:
    watch_folder: str = ""
    printer: str = ""
    interval_minutes: int = 2
    extensions: str = "doc, docx, xls, xlsx, pdf"
    processed_folder: str = "Processed"
    failed_folder: str = "Failed"
    max_attempts: int = 3
    stable_seconds: int = 10
    start_with_windows: bool = False
    start_minimized: bool = True
    monitoring_enabled: bool = True
    show_notifications: bool = True

    # ------------------------------------------------------------------ helpers
    def extension_list(self) -> list[str]:
        """'doc, .PDF;xlsx' -> ['.doc', '.pdf', '.xlsx']"""
        raw = self.extensions.replace(";", ",").replace(" ", ",").split(",")
        exts = {"." + e.strip().lower().lstrip(".") for e in raw if e.strip().strip(".")}
        return sorted(exts)

    def _clamp(self) -> None:
        self.interval_minutes = max(1, min(1440, int(self.interval_minutes)))
        self.max_attempts = max(1, min(50, int(self.max_attempts)))
        self.stable_seconds = max(0, min(3600, int(self.stable_seconds)))
        if not self.processed_folder.strip():
            self.processed_folder = "Processed"
        if not self.failed_folder.strip():
            self.failed_folder = "Failed"

    # --------------------------------------------------------------- load/save
    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            known = {f.name: f.type for f in fields(cls)}
            for key, value in data.items():
                if key in known:  # unknown/legacy keys (e.g. old sumatra_path) are ignored
                    setattr(s, key, value)
            s._clamp()
        except FileNotFoundError:
            pass
        except Exception:
            # Corrupt file – keep defaults, but don't overwrite until user saves.
            pass
        return s

    def save(self) -> None:
        self._clamp()
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2)
        os.replace(tmp, CONFIG_FILE)
