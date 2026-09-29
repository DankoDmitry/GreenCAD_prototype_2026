@echo off
setlocal
cd /d "%~dp0"
echo GreenCAD - create an isolated Python environment
if exist ".venv\Scripts\python.exe" goto install
py -3.12 -m venv .venv
if not errorlevel 1 goto install
py -3.13 -m venv .venv
if not errorlevel 1 goto install
python -m venv .venv
if errorlevel 1 goto failed
:install
".venv\Scripts\python.exe" -c "import sys; assert sys.version_info >= (3,12), 'Python 3.12+ is required'; assert sys.maxsize > 2**32, '64-bit Python is required'"
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -c "import sys,tkinter,ezdxf; assert sys.version_info >= (3,12); print('Ready:',sys.version)"
if errorlevel 1 goto failed
echo.
echo Setup completed. Run START_WINDOWS.bat
pause
exit /b 0
:failed
echo.
echo Setup failed. Install Python 3.12 x64 with Tcl/Tk and pip, then retry.
echo Internet access is needed only to install dependencies.
pause
exit /b 1
