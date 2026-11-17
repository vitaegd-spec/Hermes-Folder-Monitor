@echo off
REM Builds a single-file dist\Hermes.exe with icon (no console window)
setlocal
cd /d "%~dp0"

echo Installing build requirements...
python -m pip install -r requirements.txt pyinstaller --disable-pip-version-check
if errorlevel 1 goto :fail

echo Building Hermes.exe...
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name Hermes ^
  --icon "assets\hermes.ico" ^
  --add-data "assets;assets" ^
  main.py
if errorlevel 1 goto :fail

echo.
echo Done:  dist\Hermes.exe
echo Tip: if "Start with Windows" was enabled while running from source,
echo      open the EXE once - it refreshes the startup entry automatically.
pause
exit /b 0

:fail
echo.
echo BUILD FAILED
pause
exit /b 1
