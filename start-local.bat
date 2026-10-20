@echo off
rem Double-click: opens the CIVconvert page at http://127.0.0.1:8000 (close this window to stop)
cd /d "%~dp0"
call :setup || goto :failed
".venv\Scripts\python.exe" -m civconvert serve --open
goto :eof

:setup
if not exist ".venv\Scripts\python.exe" (
  echo First run: installing Python packages ...
  python -m venv .venv || exit /b 1
)
rem also picks up packages added by an update (quick when nothing is missing)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || exit /b 1
exit /b 0

:failed
echo Setup failed - Python 3.10 or newer must be installed.
pause
