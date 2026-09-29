@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
 echo First run setup_windows.bat
 pause
 exit /b 1
)
".venv\Scripts\python.exe" run_editor.py %*
if errorlevel 1 (
 echo.
 echo GreenCAD stopped with an error. See logs\editor.log
 pause
)
