@echo off
rem Start Menu shortcut that runs app.py via signed pyw.exe (no console) - works under Smart App Control.
cd /d "%~dp0"
py -m pip install -q -r requirements.txt || goto :fail
powershell -NoProfile -Command "foreach($d in 'Programs','Desktop'){ $s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath($d)+'\Minecraft Dashboard.lnk'); $s.TargetPath=(Get-Command pyw).Source; $s.Arguments='\"%~dp0app.py\"'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0static\icon.ico'; $s.Save() }" || goto :fail
echo.
echo Added "Minecraft Dashboard" to the Start Menu and Desktop - right-click it to "Pin to Start" / "Pin to taskbar".
pause
exit /b 0
:fail
echo Failed.
pause
exit /b 1
