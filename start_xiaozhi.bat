@echo off
REM ============================================================
REM  XiaoZhi News Manager - bridge to XiaoZhi Cloud
REM
REM  Runs official mcp_pipe.py from github.com/78/mcp-calculator,
REM  linking local MCP server (http://localhost:8001/sse)
REM  with XiaoZhi Cloud via WebSocket.
REM
REM  Before start:
REM    1) Run start.bat (app.py tray must be running)
REM    2) Get MCP_ENDPOINT on xiaozhi.me
REM    3) Put it into set MCP_ENDPOINT=... below
REM    4) Run this file
REM ============================================================
chcp 65001 >nul
cd /d %~dp0

REM === YOUR MCP_ENDPOINT FROM XIAOZHI.ME ===
set MCP_ENDPOINT=wss://api.xiaozhi.me/mcp/?token=YOUR_TOKEN_HERE

REM Check mcp_pipe.py; download from repo if missing
if not exist "%~dp0mcp_pipe.py" (
    echo [*] mcp_pipe.py not found. Downloading from github.com/78/mcp-calculator
    powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/78/mcp-calculator/main/mcp_pipe.py' -OutFile '%~dp0mcp_pipe.py'"
    if errorlevel 1 (
        echo [!] Failed to download mcp_pipe.py.
        echo     Download manually from https://github.com/78/mcp-calculator
        echo     and put mcp_pipe.py next to this .bat file.
        pause
        exit /b 1
    )
)

REM Check mcp_config.json
if not exist "%~dp0mcp_config.json" (
    echo [!] mcp_config.json not found.
    pause
    exit /b 1
)

REM Check local MCP server on port 8001 (TCP probe, SSE stream never completes HTTP GET)
echo [*] Checking MCP server on http://localhost:8001/sse
powershell -NoProfile -Command "$c=New-Object Net.Sockets.TcpClient; try { $c.Connect('127.0.0.1',8001); $c.Close(); exit 0 } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
    echo [!] MCP server is not responding. Run start.bat first.
    pause
    exit /b 1
)

REM Install missing bridge dependencies
if not exist "%~dp0.xiaozhi_installed" (
    echo [*] Installing bridge dependencies (websockets, python-dotenv, mcp-proxy)
    pip install websockets python-dotenv mcp-proxy
    if errorlevel 1 (
        echo [!] Install error. Run manually:
        echo     pip install websockets python-dotenv mcp-proxy
        pause
        exit /b 1
    )
    echo done > "%~dp0.xiaozhi_installed"
)

echo.
echo ============================================================
echo  XiaoZhi bridge starting
echo  MCP_ENDPOINT: %MCP_ENDPOINT%
echo  Local MCP: http://localhost:8001/sse
echo  Config: mcp_config.json
echo  Stop: Ctrl+C in this window
echo ============================================================
echo.

py "%~dp0mcp_pipe.py"

echo.
echo [!] Bridge exited. Check MCP_ENDPOINT in this .bat file.
pause
exit /b 0
