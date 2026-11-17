"""Printing back-ends.

  * PDF         -> default PDF app (e.g. Adobe Acrobat Reader) via shell 'printto'
  * Word types  -> Microsoft Word via COM (hidden)      | fallback: shell 'printto'
  * Excel types -> Microsoft Excel via COM (hidden)     | fallback: shell 'printto'
  * Text types  -> Notepad  (notepad.exe /pt "file" "printer")
  * anything else -> shell 'printto' verb of the registered app
"""
from __future__ import annotations

import os
import subprocess
import time
import winreg

import pywintypes
import win32api
import win32print

WORD_EXTS = {".doc", ".docx", ".docm", ".dot", ".dotx", ".rtf", ".odt"}
EXCEL_EXTS = {".xls", ".xlsx", ".xlsm", ".xlsb", ".csv", ".ods"}
TEXT_EXTS = {".txt", ".text", ".log"}

DUMMY_PASSWORD = "__hermes_no_password__"  # makes protected files fail instead of popping a hidden dialog
SHELL_PRINT_SETTLE_SECONDS = 20            # give the external app time to spool before we move the file
NOTEPAD_TIMEOUT_SECONDS = 120              # Notepad /pt exits by itself once the job is spooled
CREATE_NO_WINDOW = 0x08000000


class PrintError(Exception):
    pass


# --------------------------------------------------------------------------- helpers
def list_printers() -> list[str]:
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    try:
        names = {p[2] for p in win32print.EnumPrinters(flags)}
    except pywintypes.error:
        names = set()
    return sorted(names, key=str.lower)


def default_printer() -> str:
    try:
        return win32print.GetDefaultPrinter()
    except Exception:
        return ""


def describe_error(exc: BaseException) -> str:
    """Turn COM / win32 errors into something readable."""
    try:
        if isinstance(exc, pywintypes.com_error):
            hr, msg, excepinfo, _ = exc.args
            code = f"0x{hr & 0xFFFFFFFF:08X}"
            if excepinfo and len(excepinfo) > 2 and excepinfo[2]:
                return f"{str(excepinfo[2]).strip()} ({code})"
            return f"{msg} ({code})"
        if isinstance(exc, pywintypes.error):
            winerror, func, text = exc.args
            return f"{text} [{func}, {winerror}]"
    except Exception:
        pass
    return str(exc) or exc.__class__.__name__


def _notepad_path() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    for path in (os.path.join(root, "System32", "notepad.exe"), os.path.join(root, "notepad.exe")):
        if os.path.isfile(path):
            return path
    return ""


def _com_available(progid: str) -> bool:
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid))
        return True
    except OSError:
        return False


def _excel_printer_names(printer: str) -> list[str]:
    """Excel's ActivePrinter wants 'Name on Ne01:'. Look the port up, then fall back to guesses."""
    names = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows NT\CurrentVersion\Devices") as key:
            value, _ = winreg.QueryValueEx(key, printer)  # e.g. "winspool,Ne01:"
            port = str(value).split(",")[-1].strip()
            if port:
                names.append(f"{printer} on {port}")
    except OSError:
        pass
    names.append(printer)
    names += [f"{printer} on Ne{i:02d}:" for i in range(16)]
    seen, ordered = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


def _restore_default(original: str) -> None:
    """Office can change the Windows default printer – put it back."""
    if original and default_printer() != original:
        try:
            win32print.SetDefaultPrinter(original)
        except Exception:
            pass


# --------------------------------------------------------------------------- printer
class DocumentPrinter:
    def __init__(self, printer: str):
        self.printer = printer

    def print_file(self, path: str) -> str:
        """Print synchronously. Returns a short description of the method used. Raises on failure."""
        ext = os.path.splitext(path)[1].lower()
        if ext in WORD_EXTS and _com_available("Word.Application"):
            return self._print_word(path)
        if ext in EXCEL_EXTS and _com_available("Excel.Application"):
            return self._print_excel(path)
        if ext in TEXT_EXTS:
            return self._print_notepad(path)
        return self._print_shell(path)

    # --------------------------------------------------------------- Notepad
    def _print_notepad(self, path: str) -> str:
        notepad = _notepad_path()
        if not notepad:
            raise PrintError("notepad.exe was not found on this PC")
        cmd = [notepad, "/pt", path, self.printer]
        try:
            proc = subprocess.Popen(cmd, cwd=os.path.dirname(path), creationflags=CREATE_NO_WINDOW)
        except OSError as e:
            raise PrintError(f"Could not start Notepad: {e}")
        try:
            rc = proc.wait(timeout=NOTEPAD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise PrintError("Notepad did not finish printing in time "
                             "(a printer that asks for a file name, e.g. 'Microsoft Print to PDF', can cause this)")
        if rc not in (0, None):
            raise PrintError(f"Notepad exited with code {rc}")
        time.sleep(3)  # let the spooler pick the job up before the file is moved
        return "Notepad"

    # ------------------------------------------------------------------ Word
    def _print_word(self, path: str) -> str:
        import win32com.client

        original_default = default_printer()
        word = win32com.client.DispatchEx("Word.Application")  # private hidden instance
        doc = None
        try:
            word.Visible = False
            word.DisplayAlerts = 0  # wdAlertsNone
            try:
                word.AutomationSecurity = 3  # msoAutomationSecurityForceDisable – never run macros
            except Exception:
                pass
            doc = word.Documents.Open(
                FileName=path, ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False,
                PasswordDocument=DUMMY_PASSWORD, Visible=False, NoEncodingDialog=True,
            )
            self._select_word_printer(word)
            doc.PrintOut(Background=False)
            for _ in range(600):  # up to ~5 min safety net
                if int(word.BackgroundPrintingStatus) == 0:
                    break
                time.sleep(0.5)
        finally:
            if doc is not None:
                try:
                    doc.Close(SaveChanges=0)
                except Exception:
                    pass
            try:
                word.Quit(SaveChanges=0)
            except Exception:
                pass
            _restore_default(original_default)
        return "Microsoft Word"

    def _select_word_printer(self, word) -> None:
        # Preferred: change Word's printer without touching the Windows default.
        try:
            word.WordBasic.FilePrintSetup(Printer=self.printer, DoNotSetAsSysDefault=1)
        except Exception:
            pass
        try:
            current = str(word.ActivePrinter)
        except Exception:
            current = ""
        if not current.lower().startswith(self.printer.lower()):
            word.ActivePrinter = self.printer  # default gets restored afterwards

    # ----------------------------------------------------------------- Excel
    def _print_excel(self, path: str) -> str:
        import win32com.client

        original_default = default_printer()
        excel = win32com.client.DispatchEx("Excel.Application")
        wb = None
        try:
            excel.Visible = False
            excel.DisplayAlerts = False
            for attr, val in (("ScreenUpdating", False), ("AskToUpdateLinks", False),
                              ("EnableEvents", False), ("AutomationSecurity", 3)):
                try:
                    setattr(excel, attr, val)
                except Exception:
                    pass
            wb = excel.Workbooks.Open(
                Filename=path, UpdateLinks=0, ReadOnly=True, Password=DUMMY_PASSWORD,
                IgnoreReadOnlyRecommended=True, Notify=False, AddToMru=False,
            )
            last_error = None
            for name in _excel_printer_names(self.printer):
                try:
                    wb.PrintOut(ActivePrinter=name)
                    last_error = None
                    break
                except pywintypes.com_error as e:
                    last_error = e
            if last_error is not None:
                raise PrintError(f"Excel could not print to '{self.printer}': {describe_error(last_error)}")
        finally:
            if wb is not None:
                try:
                    wb.Close(SaveChanges=False)
                except Exception:
                    pass
            try:
                excel.Quit()
            except Exception:
                pass
            _restore_default(original_default)
        return "Microsoft Excel"

    # ----------------------------------------------------------------- Shell
    def _print_shell(self, path: str) -> str:
        folder = os.path.dirname(path)
        try:
            win32api.ShellExecute(0, "printto", path, f'"{self.printer}"', folder, 0)
            method = "default app (printto)"
        except pywintypes.error as e:
            if self.printer == default_printer():
                try:
                    win32api.ShellExecute(0, "print", path, None, folder, 0)
                    method = "default app (print)"
                except pywintypes.error as e2:
                    raise PrintError(f"No app is registered to print this file type ({describe_error(e2)})")
            else:
                ext = os.path.splitext(path)[1].lower()
                hint = (" Set Adobe Acrobat Reader as the default PDF app." if ext == ".pdf"
                        else " Install Microsoft Office for Word/Excel files.")
                raise PrintError(f"No app can print '{ext}' files to a specific printer ({describe_error(e)}).{hint}")
        time.sleep(SHELL_PRINT_SETTLE_SECONDS)
        return method
