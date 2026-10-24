@echo off
cd /d "%~dp0"
rem Use the mtg1 conda env's interpreter directly (python isn't on PATH; avoids the Microsoft Store stub).
set "MTG_PY=%USERPROFILE%\miniconda3\envs\mtg1\python.exe"
if not exist "%MTG_PY%" (
    echo Could not find %MTG_PY%
    echo Edit MTG_PY in run.bat to point at the mtg1 conda env's python.exe.
    pause
    exit /b 1
)
"%MTG_PY%" run.py %*
echo.
pause
