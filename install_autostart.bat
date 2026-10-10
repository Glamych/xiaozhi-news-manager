@echo off
REM ============================================================
REM  XiaoZhi News Manager — установка автозапуска Windows
REM  Создаёт ярлык в shell:startup (папка "Автозагрузка"),
REM  указывающий на start.bat, с WindowStyle=Minimized (7).
REM ============================================================
chcp 65001 >nul
cd /d %~dp0

REM Ярлык создаём через PowerShell
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$WshShell = New-Object -ComObject WScript.Shell; " ^
  "$Shortcut = $WshShell.CreateShortcut([Environment]::GetFolderPath('Startup') + '\XiaoZhiNews.lnk'); " ^
  "$Shortcut.TargetPath = '%~dp0start.bat'; " ^
  "$Shortcut.WorkingDirectory = '%~dp0'; " ^
  "$Shortcut.WindowStyle = 7; " ^
  "$Shortcut.IconLocation = '%~dp0static\icon.png,0'; " ^
  "$Shortcut.Description = 'XiaoZhi News Manager'; " ^
  "$Shortcut.Save(); " ^
  "Write-Host 'Ярлык создан: ' $Shortcut.FullName"

if errorlevel 1 (
    echo [!] Не удалось создать ярлык автозапуска.
    echo     Проверьте права и наличие PowerShell.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Автозапуск установлен. XiaoZhi News будет запускаться
echo  при входе в Windows.
echo  Расположение ярлыка: shell:startup
echo ============================================================
echo.
pause
exit /b 0
