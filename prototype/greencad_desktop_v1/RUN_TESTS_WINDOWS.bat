@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
 echo First run setup_windows.bat
 pause
 exit /b 1
)
".venv\Scripts\python.exe" -m unittest discover -s tests -v
pause
