@echo off
rem Build MinecraftDashboard.exe (single file, no console) next to config.json.
cd /d "%~dp0"
py -m pip install -q -r requirements.txt pyinstaller || goto :fail
py -m PyInstaller --noconfirm --clean --onefile --windowed --name MinecraftDashboard ^
  --icon "%~dp0static\icon.ico" ^
  --add-data "%~dp0templates;templates" --add-data "%~dp0static;static" ^
  --distpath "%~dp0." --workpath build --specpath build app.py || goto :fail
rmdir /s /q build
echo.
echo Built MinecraftDashboard.exe - double-click it, or right-click it and "Pin to Start" / "Pin to taskbar".
pause
exit /b 0
:fail
echo Build failed.
pause
exit /b 1
