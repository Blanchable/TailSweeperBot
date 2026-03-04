@echo off
setlocal EnableDelayedExpansion

:: ============================================================
:: Polymarket Tail Sweeper — One-Click Launcher
:: Double-click this file to start the application.
:: ============================================================

title Polymarket Tail Sweeper — Launcher

echo.
echo =============================================
echo   Polymarket Tail Sweeper - Launcher
echo =============================================
echo.

:: ----------------------------------------------------------
:: 1. Check Python is available
:: ----------------------------------------------------------
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found on PATH.
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

:: Verify Python version >= 3.10
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo [OK] Found Python %PYVER%

:: ----------------------------------------------------------
:: 2. Create virtual environment if missing
:: ----------------------------------------------------------
if not exist ".venv\Scripts\activate.bat" (
    echo [SETUP] Creating virtual environment...
    python -m venv .venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created.
    set "NEEDS_INSTALL=1"
) else (
    echo [OK] Virtual environment exists.
    set "NEEDS_INSTALL=0"
)

:: ----------------------------------------------------------
:: 3. Activate virtual environment
:: ----------------------------------------------------------
call .venv\Scripts\activate.bat

:: ----------------------------------------------------------
:: 4. Install / update requirements if needed
:: ----------------------------------------------------------
if "%NEEDS_INSTALL%"=="1" (
    echo [SETUP] Installing dependencies...
    pip install --upgrade pip >nul 2>&1
    pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
    echo [OK] Dependencies installed.
) else (
    :: Quick check: if PySide6 is not importable, reinstall
    python -c "import PySide6" >nul 2>&1
    if %errorlevel% neq 0 (
        echo [SETUP] Missing packages detected. Installing...
        pip install --upgrade pip >nul 2>&1
        pip install -r requirements.txt
        if %errorlevel% neq 0 (
            echo [ERROR] Failed to install dependencies.
            pause
            exit /b 1
        )
        echo [OK] Dependencies installed.
    )
)

:: ----------------------------------------------------------
:: 5. Create .env from .env.example if missing
:: ----------------------------------------------------------
if not exist ".env" (
    if exist ".env.example" (
        echo [SETUP] Creating .env from .env.example...
        copy .env.example .env >nul
        echo [OK] .env created. Edit it to add your credentials for live trading.
    )
)

:: ----------------------------------------------------------
:: 6. Launch the application
:: ----------------------------------------------------------
echo.
echo [LAUNCH] Starting Polymarket Tail Sweeper...
echo.
python main.py

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Application exited with an error.
    pause
)

endlocal
