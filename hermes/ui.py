"""Main window (Status + Settings tabs) and system tray integration."""
from __future__ import annotations

import html
import logging
import os
import time
from dataclasses import replace

from PySide6.QtCore import QDir, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QStyle, QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget,
)

from . import startup
from .config import APP_NAME, APP_VERSION, LOG_FILE, Settings, resource_path
from .printing import default_printer, list_printers
from .watcher import FolderWatcher

log = logging.getLogger("hermes")

FIRST_CHECK_DELAY = 5       # seconds after launch / resume
LEVEL_COLORS = {"WARNING": "#d08700", "ERROR": "#e05252", "CRITICAL": "#e05252"}
INVALID_NAME_CHARS = set('\\/:*?"<>|')

STYLE = """
QMainWindow, QWidget { font-size: 10pt; }
QGroupBox { font-weight: 600; border: 1px solid palette(mid); border-radius: 8px; margin-top: 12px; padding: 10px 8px 8px 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QPushButton { padding: 5px 12px; border-radius: 6px; }
QPushButton#primary { font-weight: 600; }
QLabel#stateLabel { font-size: 12pt; font-weight: 700; }
QLabel[class="muted"] { color: #808080; }
QPlainTextEdit { font-family: Consolas, 'Cascadia Mono', monospace; font-size: 9pt; }
"""


def _row(*widgets) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    for item in widgets:
        if isinstance(item, int):
            lay.addStretch(item)
        else:
            lay.addWidget(item)
    return w


class MainWindow(QMainWindow):
    request_scan = Signal(object)

    def __init__(self, log_emitter, launched_minimized: bool = False):
        super().__init__()
        self.settings = Settings.load()
        self.printed_total = 0
        self.failed_total = 0
        self.busy = False
        self.last_check: str | None = None
        self.next_due = time.monotonic() + FIRST_CHECK_DELAY
        self._quitting = False
        self._tray_hint_shown = False

        icon = QIcon(resource_path(os.path.join("assets", "hermes.ico")))
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DriveNetIcon)
        self.icon_active = icon
        self.icon_paused = QIcon(icon.pixmap(64, QIcon.Mode.Disabled))

        self.setWindowTitle(f"{APP_NAME} – Folder Auto-Print  v{APP_VERSION}")
        self.setWindowIcon(self.icon_active)
        self.resize(860, 600)
        self.setStyleSheet(STYLE)

        tabs = QTabWidget()
        tabs.addTab(self._build_status_tab(), "Status")
        tabs.addTab(self._build_settings_tab(), "Settings")
        self.setCentralWidget(tabs)
        self.tabs = tabs

        self._build_tray()
        self._start_worker()
        log_emitter.message.connect(self._append_log)

        self._load_settings_into_form()
        self._refresh_status()

        # Keep the Run-key path current (e.g. after switching from script to EXE)
        if self.settings.start_with_windows:
            try:
                startup.set_startup(True)
            except OSError as e:
                log.warning("Could not update Windows startup entry: %s", e)

        self.ticker = QTimer(self)
        self.ticker.timeout.connect(self._tick)
        self.ticker.start(1000)

        log.info("%s %s started. Settings: %s", APP_NAME, APP_VERSION, os.path.dirname(LOG_FILE))

        needs_setup = not self.settings.watch_folder or not self.settings.printer
        if needs_setup:
            self.tabs.setCurrentIndex(1)
            self.show()
        elif not (launched_minimized or self.settings.start_minimized):
            self.show()

    # =================================================================== UI build
    def _build_status_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)

        box = QGroupBox("Status")
        grid = QGridLayout(box)
        self.lbl_state = QLabel()
        self.lbl_state.setObjectName("stateLabel")
        self.lbl_folder = QLabel()
        self.lbl_folder.setWordWrap(True)
        self.lbl_printer = QLabel()
        self.lbl_next = QLabel()
        self.lbl_last = QLabel("—")
        self.lbl_counts = QLabel()
        rows = [("State", self.lbl_state), ("Watch folder", self.lbl_folder), ("Printer", self.lbl_printer),
                ("Next check", self.lbl_next), ("Last check", self.lbl_last), ("This session", self.lbl_counts)]
        for r, (title, widget) in enumerate(rows):
            t = QLabel(title + ":")
            t.setProperty("class", "muted")
            grid.addWidget(t, r, 0)
            grid.addWidget(widget, r, 1)
        grid.setColumnStretch(1, 1)
        outer.addWidget(box)

        self.btn_check = QPushButton("Check now")
        self.btn_check.setObjectName("primary")
        self.btn_check.clicked.connect(lambda: self._trigger_scan(manual=True))
        self.btn_pause = QPushButton()
        self.btn_pause.clicked.connect(self._toggle_pause)
        btn_open = QPushButton("Open watch folder")
        btn_open.clicked.connect(lambda: self._open_path(self.settings.watch_folder))
        btn_proc = QPushButton("Open Processed")
        btn_proc.clicked.connect(lambda: self._open_path(
            os.path.join(self.settings.watch_folder, self.settings.processed_folder), create=True))
        btn_failed = QPushButton("Open Failed")
        btn_failed.clicked.connect(lambda: self._open_path(
            os.path.join(self.settings.watch_folder, self.settings.failed_folder), create=True))
        outer.addWidget(_row(self.btn_check, self.btn_pause, 1, btn_open, btn_proc, btn_failed))

        logbox = QGroupBox("Activity log")
        lv = QVBoxLayout(logbox)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        lv.addWidget(self.log_view)
        btn_clear = QPushButton("Clear view")
        btn_clear.clicked.connect(self.log_view.clear)
        btn_logfile = QPushButton("Open log file")
        btn_logfile.clicked.connect(lambda: self._open_path(LOG_FILE))
        lv.addWidget(_row(1, btn_clear, btn_logfile))
        outer.addWidget(logbox, 1)
        return page

    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)

        # --- Folder & printer
        g1 = QGroupBox("Folder && printer")
        f1 = QFormLayout(g1)
        self.ed_folder = QLineEdit()
        self.ed_folder.setPlaceholderText(r"e.g. G:\My Drive\Print Queue  or  C:\Users\you\OneDrive\Print")
        b_browse = QPushButton("Browse…")
        b_browse.clicked.connect(self._browse_folder)
        f1.addRow("Watch folder:", _row(self.ed_folder, b_browse))

        self.cb_printer = QComboBox()
        self.cb_printer.setMinimumWidth(320)
        b_refresh = QPushButton("Refresh")
        b_refresh.clicked.connect(lambda: self._populate_printers(self.cb_printer.currentData()))
        f1.addRow("Printer:", _row(self.cb_printer, b_refresh, 1))
        outer.addWidget(g1)

        # --- Schedule & files
        g2 = QGroupBox("Schedule && files")
        f2 = QFormLayout(g2)
        self.sp_interval = QSpinBox()
        self.sp_interval.setRange(1, 1440)
        self.sp_interval.setSuffix(" min")
        f2.addRow("Check every:", _row(self.sp_interval, 1))
        self.ed_exts = QLineEdit()
        self.ed_exts.setPlaceholderText("doc, docx, xls, xlsx, pdf")
        f2.addRow("File types:", self.ed_exts)
        self.ed_processed = QLineEdit()
        f2.addRow("Processed subfolder:", self.ed_processed)
        self.ed_failed = QLineEdit()
        f2.addRow("Failed subfolder:", self.ed_failed)
        self.sp_attempts = QSpinBox()
        self.sp_attempts.setRange(1, 50)
        self.sp_attempts.setToolTip("After this many failed print attempts the file is moved to the Failed subfolder.")
        f2.addRow("Max print attempts:", _row(self.sp_attempts, 1))
        self.sp_stable = QSpinBox()
        self.sp_stable.setRange(0, 3600)
        self.sp_stable.setSuffix(" s")
        self.sp_stable.setToolTip("Only print a file once it hasn't changed for this long (lets cloud sync finish).")
        f2.addRow("File must be unchanged for:", _row(self.sp_stable, 1))
        outer.addWidget(g2)

        # --- Windows
        g4 = QGroupBox("Windows")
        v4 = QVBoxLayout(g4)
        self.chk_startup = QCheckBox("Start Hermes when Windows starts")
        self.chk_minimized = QCheckBox("Start minimized to the system tray")
        self.chk_enabled = QCheckBox("Monitoring enabled")
        self.chk_notify = QCheckBox("Show tray notifications when files are printed / fail")
        for c in (self.chk_startup, self.chk_minimized, self.chk_enabled, self.chk_notify):
            v4.addWidget(c)
        outer.addWidget(g4)

        outer.addStretch(1)
        b_revert = QPushButton("Revert")
        b_revert.clicked.connect(self._load_settings_into_form)
        b_save = QPushButton("Save settings")
        b_save.setObjectName("primary")
        b_save.clicked.connect(self._save_settings)
        outer.addWidget(_row(1, b_revert, b_save))
        return page

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self.icon_active, self)
        menu = QMenu()
        self.act_open = QAction("Open Hermes", self, triggered=self._show_window)
        self.act_check = QAction("Check now", self, triggered=lambda: self._trigger_scan(manual=True))
        self.act_pause = QAction("Pause monitoring", self, triggered=self._toggle_pause)
        act_folder = QAction("Open watch folder", self,
                             triggered=lambda: self._open_path(self.settings.watch_folder))
        act_exit = QAction("Exit", self, triggered=self._quit)
        menu.addAction(self.act_open)
        menu.addSeparator()
        menu.addAction(self.act_check)
        menu.addAction(self.act_pause)
        menu.addAction(act_folder)
        menu.addSeparator()
        menu.addAction(act_exit)
        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _start_worker(self) -> None:
        self.worker_thread = QThread(self)
        self.worker = FolderWatcher()
        self.worker.moveToThread(self.worker_thread)
        self.request_scan.connect(self.worker.scan)
        self.worker.scan_started.connect(self._on_scan_started)
        self.worker.scan_finished.connect(self._on_scan_finished)
        self.worker.file_printed.connect(self._on_file_printed)
        self.worker.file_failed.connect(self._on_file_failed)
        self.worker_thread.start()

    # =================================================================== settings form
    def _populate_printers(self, select: str | None = None) -> None:
        select = select if select is not None else self.settings.printer
        self.cb_printer.clear()
        default = default_printer()
        printers = list_printers()
        self.cb_printer.addItem("— select a printer —", "")
        for p in printers:
            self.cb_printer.addItem(f"{p}  (Windows default)" if p == default else p, p)
        if select and select not in printers:
            self.cb_printer.addItem(f"{select}  (NOT FOUND)", select)
        idx = self.cb_printer.findData(select or "")
        self.cb_printer.setCurrentIndex(max(0, idx))

    def _load_settings_into_form(self) -> None:
        s = self.settings
        self.ed_folder.setText(s.watch_folder)
        self._populate_printers(s.printer)
        self.sp_interval.setValue(s.interval_minutes)
        self.ed_exts.setText(s.extensions)
        self.ed_processed.setText(s.processed_folder)
        self.ed_failed.setText(s.failed_folder)
        self.sp_attempts.setValue(s.max_attempts)
        self.sp_stable.setValue(s.stable_seconds)
        self.chk_startup.setChecked(s.start_with_windows)
        self.chk_minimized.setChecked(s.start_minimized)
        self.chk_enabled.setChecked(s.monitoring_enabled)
        self.chk_notify.setChecked(s.show_notifications)

    def _save_settings(self) -> None:
        s = replace(self.settings)
        s.watch_folder = os.path.normpath(self.ed_folder.text().strip()) if self.ed_folder.text().strip() else ""
        s.printer = self.cb_printer.currentData() or ""
        s.interval_minutes = self.sp_interval.value()
        s.extensions = self.ed_exts.text().strip()
        s.processed_folder = self.ed_processed.text().strip()
        s.failed_folder = self.ed_failed.text().strip()
        s.max_attempts = self.sp_attempts.value()
        s.stable_seconds = self.sp_stable.value()
        s.start_with_windows = self.chk_startup.isChecked()
        s.start_minimized = self.chk_minimized.isChecked()
        s.monitoring_enabled = self.chk_enabled.isChecked()
        s.show_notifications = self.chk_notify.isChecked()

        errors = []
        if not s.watch_folder:
            errors.append("Choose a watch folder.")
        elif not os.path.isdir(s.watch_folder):
            errors.append("The watch folder does not exist.")
        if not s.printer:
            errors.append("Choose a printer.")
        if not s.extension_list():
            errors.append("Enter at least one file type.")
        for label, val in (("Processed", s.processed_folder), ("Failed", s.failed_folder)):
            if not val or any(c in INVALID_NAME_CHARS for c in val):
                errors.append(f"{label} subfolder name is empty or contains invalid characters.")
        if s.processed_folder.lower() == s.failed_folder.lower():
            errors.append("Processed and Failed subfolders must have different names.")
        if errors:
            QMessageBox.warning(self, APP_NAME, "\n".join(errors))
            return

        try:
            startup.set_startup(s.start_with_windows)
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Could not update Windows startup setting:\n{e}")

        try:
            s.save()
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"Could not save settings:\n{e}")
            return

        interval_changed = s.interval_minutes != self.settings.interval_minutes
        self.settings = s
        if interval_changed or not self.busy:
            self.next_due = time.monotonic() + FIRST_CHECK_DELAY
        self._refresh_status()
        log.info("Settings saved. Watching '%s' every %d min → %s (types: %s).",
                 s.watch_folder, s.interval_minutes, s.printer, ", ".join(s.extension_list()))

    def _browse_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose folder to watch", self.ed_folder.text() or "")
        if path:
            self.ed_folder.setText(os.path.normpath(path))

    # =================================================================== scanning
    def _tick(self) -> None:
        if self.settings.monitoring_enabled and not self.busy and time.monotonic() >= self.next_due:
            self._trigger_scan()
        self._update_countdown()

    def _trigger_scan(self, manual: bool = False) -> None:
        if self.busy:
            if manual:
                log.info("A check is already running.")
            return
        self.busy = True
        self.next_due = time.monotonic() + self.settings.interval_minutes * 60
        if manual:
            log.info("Manual check requested.")
        self.request_scan.emit(replace(self.settings))
        self._update_countdown()

    def _on_scan_started(self) -> None:
        self.busy = True
        self.btn_check.setEnabled(False)
        self.act_check.setEnabled(False)

    def _on_scan_finished(self, printed: int, failed: int) -> None:
        self.busy = False
        self.last_check = time.strftime("%H:%M:%S")
        self.next_due = time.monotonic() + self.settings.interval_minutes * 60
        self.btn_check.setEnabled(True)
        self.act_check.setEnabled(True)
        self._refresh_status()

    def _on_file_printed(self, name: str) -> None:
        self.printed_total += 1
        self._refresh_status()
        if self.settings.show_notifications:
            self.tray.showMessage(APP_NAME, f"Printed: {name}", QSystemTrayIcon.MessageIcon.Information, 4000)

    def _on_file_failed(self, name: str, reason: str) -> None:
        self.failed_total += 1
        self._refresh_status()
        if self.settings.show_notifications:
            self.tray.showMessage(APP_NAME, f"Could not print: {name}\n{reason}",
                                  QSystemTrayIcon.MessageIcon.Warning, 8000)

    # =================================================================== status
    def _toggle_pause(self) -> None:
        self.settings.monitoring_enabled = not self.settings.monitoring_enabled
        try:
            self.settings.save()
        except OSError:
            pass
        self.chk_enabled.setChecked(self.settings.monitoring_enabled)
        if self.settings.monitoring_enabled:
            self.next_due = time.monotonic() + FIRST_CHECK_DELAY
            log.info("Monitoring resumed.")
        else:
            log.info("Monitoring paused.")
        self._refresh_status()

    def _refresh_status(self) -> None:
        s = self.settings
        on = s.monitoring_enabled
        self.lbl_state.setText("● Monitoring" if on else "❚❚ Paused")
        self.lbl_state.setStyleSheet(f"color: {'#2e9e5b' if on else '#d08700'};")
        self.lbl_folder.setText(s.watch_folder or "<i>not set</i>")
        self.lbl_printer.setText(s.printer or "<i>not set</i>")
        self.lbl_last.setText(self.last_check or "—")
        self.lbl_counts.setText(f"{self.printed_total} printed, {self.failed_total} failed")
        self.btn_pause.setText("Pause" if on else "Resume")
        self.act_pause.setText("Pause monitoring" if on else "Resume monitoring")
        self.tray.setIcon(self.icon_active if on else self.icon_paused)
        self.tray.setToolTip(f"{APP_NAME} – {'monitoring' if on else 'paused'}\n"
                             f"{self.printed_total} printed this session")
        self._update_countdown()

    def _update_countdown(self) -> None:
        if not self.settings.monitoring_enabled:
            txt = "paused"
        elif self.busy:
            txt = "checking now…"
        else:
            rem = max(0, int(self.next_due - time.monotonic()))
            txt = f"in {rem // 60:02d}:{rem % 60:02d}  (every {self.settings.interval_minutes} min)"
        self.lbl_next.setText(txt)

    def _append_log(self, level: str, text: str) -> None:
        safe = html.escape(text)
        color = LEVEL_COLORS.get(level)
        self.log_view.appendHtml(f'<span style="color:{color}">{safe}</span>' if color else safe)

    # =================================================================== window / tray
    def _open_path(self, path: str, create: bool = False) -> None:
        if not path:
            QMessageBox.information(self, APP_NAME, "Set a watch folder first.")
            return
        if create and not os.path.exists(path) and os.path.isdir(os.path.dirname(path)):
            os.makedirs(path, exist_ok=True)
        path = os.path.normpath(path)
        if not os.path.exists(path):
            QMessageBox.information(self, APP_NAME, f"Not found:\n{path}")
            return
        try:
            os.startfile(path)  # Explorer opens the exact folder (works with G:\My Drive etc.)
        except OSError:
            QDesktopServices.openUrl(QUrl.fromLocalFile(QDir.fromNativeSeparators(path)))

    def _show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            if self.isVisible() and not self.isMinimized():
                self.hide()
            else:
                self._show_window()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._quitting:
            event.accept()
            return
        event.ignore()
        self.hide()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.showMessage(APP_NAME, "Still running in the system tray. Right-click the icon to exit.",
                                  QSystemTrayIcon.MessageIcon.Information, 3000)

    def _quit(self) -> None:
        if self.busy:
            res = QMessageBox.question(self, APP_NAME, "A print job is in progress. Exit anyway once it finishes?")
            if res != QMessageBox.StandardButton.Yes:
                return
        self._quitting = True
        log.info("Hermes exiting.")
        self.ticker.stop()
        self.worker.stop()
        self.worker_thread.quit()
        self.worker_thread.wait(15000)
        self.tray.hide()
        self.close()
        QApplication.quit()
