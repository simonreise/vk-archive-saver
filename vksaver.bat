@echo off
rem Windows. Без параметров открывается окно программы.
rem Примеры:  vksaver.bat scan all    vksaver.bat download messages --kinds photo,doc
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py -3 -m vksaver %*) else (python -m vksaver %*)
if not "%~1"=="" pause
