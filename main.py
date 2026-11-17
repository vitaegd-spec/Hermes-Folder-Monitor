"""Hermes – watches a folder and auto-prints new documents, then moves them to a Processed subfolder."""
import sys


def main() -> int:
    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

    app = QApplication(sys.argv)
    app.setApplicationName("Hermes")
    app.setQuitOnLastWindowClosed(False)

    if sys.platform != "win32":
        QMessageBox.critical(None, "Hermes", "Hermes only runs on Windows.")
        return 1

    from hermes.config import LOCK_FILE

    lock = QLockFile(LOCK_FILE)
    lock.setStaleLockTime(0)
    if not lock.tryLock(200):
        QMessageBox.information(None, "Hermes", "Hermes is already running – look for its icon in the system tray.")
        return 0

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.warning(None, "Hermes", "No system tray detected – Hermes will run as a normal window.")

    from hermes.logger import setup_logging
    from hermes.ui import MainWindow

    emitter = setup_logging()
    window = MainWindow(emitter, launched_minimized="--minimized" in sys.argv)  # noqa: F841
    rc = app.exec()
    lock.unlock()
    return rc


if __name__ == "__main__":
    sys.exit(main())
