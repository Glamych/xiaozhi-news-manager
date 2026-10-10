@echo off
REM ============================================================
REM  XiaoZhi News Manager — удаление автозапуска Windows
REM  Удаляет ярлык XiaoZhiNews.lnk из папки "Автозагрузка".
REM ============================================================
chcp 65001 >nul

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$p = [Environment]::GetFolderPath('Startup') + '\XiaoZhiNews.lnk'; " ^
  "if (Test-Path $p) { Remove-Item $p -Force; Write-Host 'Ярлык удалён: ' $p } " ^
  "else { Write-Host 'Ярлык не найден — уже удалён.' }"

if errorlevel 1 (
    echo [!] Не удалось удалить ярлык.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Автозапуск удалён. XiaoZhi News больше не будет
echo  запускаться автоматически при входе в Windows.
echo ============================================================
echo.
pause
exit /b 0
