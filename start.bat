@echo off
REM ============================================================
REM  XiaoZhi News Manager — запуск (без чёрного окна консоли)
REM  Используется pythonw — графический Python без консоли.
REM ============================================================
chcp 65001 >nul
cd /d %~dp0

REM Проверяем наличие pythonw (стандартный CPython с python.org)
where pythonw.exe >nul 2>&1
if errorlevel 1 (
    echo [!] pythonw.exe не найден в PATH.
    echo     Установите Python 3.10+ с https://python.org
    echo     При установке отметьте "Add Python to PATH".
    pause
    exit /b 1
)

REM Если зависимости ещё не установлены — ставим (один раз)
if not exist "%~dp0venv\" (
    if not exist "%~dp0.installed" (
        echo [*] Устанавливаю зависимости
        python -m pip install -r requirements.txt
        if errorlevel 1 (
            echo [!] Ошибка установки зависимостей.
            pause
            exit /b 1
        )
        echo done > "%~dp0.installed"
    )
)

REM Запускаем в фоне, свёрнуто (pythonw не открывает окно консоли вообще)
echo [*] Запуск XiaoZhi News Manager
start "" /B pythonw.exe "%~dp0app.py"

echo [OK] Приложение запущено в трее.
echo     Иконка появится в системном трее (правый нижний угол).
timeout /t 2 >nul
exit /b 0
