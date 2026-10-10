@echo off
REM ============================================================
REM  XiaoZhi News Manager — остановка
REM  Находит процесс pythonw с app.py в командной строке
REM  и завершает его. Fallback: taskkill всех pythonw.
REM ============================================================
chcp 65001 >nul

echo [*] Поиск процесса pythonw с app.py…

REM Метод 1: wmic — точечное завершение по командной строке
wmic process where "CommandLine like '%%app.py%%' and Name='pythonw.exe'" delete >nul 2>&1

if errorlevel 1 (
    echo [!] Точный поиск не удался. Пробуем taskkill…
    REM Метод 2: жёсткий fallback — все pythonw
    taskkill /F /IM pythonw.exe >nul 2>&1
)

if errorlevel 1 (
    echo [!] Не удалось найти запущенный процесс.
    echo     Возможно, приложение уже остановлено.
) else (
    echo [OK] XiaoZhi News Manager остановлен.
)

timeout /t 2 >nul
exit /b 0
