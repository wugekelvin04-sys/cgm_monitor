@echo off
setlocal
set "DIR=%~dp0"
set "VENV=%DIR%venv_win"
set "PYTHON=%VENV%\Scripts\python.exe"
set "EDIR=%DIR%electron"

if not exist "%PYTHON%" (
    echo [CGM] 正在创建虚拟环境...
    python -m venv "%VENV%"
    if errorlevel 1 ( echo [ERROR] 未找到 Python，请安装 Python 3.10+ & pause & exit /b 1 )
)

"%PYTHON%" -m pip install -q --upgrade pip
"%PYTHON%" -m pip install -q -r "%DIR%requirements-electron.txt"
"%PYTHON%" "%EDIR%\create_icon.py"

if not exist "%EDIR%\node_modules" ( cd /d "%EDIR%" && npm install )

call "%VENV%\Scripts\activate.bat"
cd /d "%EDIR%"
npm start
