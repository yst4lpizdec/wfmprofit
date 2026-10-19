@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul && (py -3 wfm_profit.py %*) || (python wfm_profit.py %*)
if errorlevel 1 echo.& echo Если пишет, что Python не найден - установи его с https://www.python.org/downloads/ (галочка "Add python.exe to PATH").
pause
