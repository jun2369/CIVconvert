@echo off
rem Drag one or more Commercial Invoice PDFs onto this file.
rem The CSV ("CargoWise Input - <pdf name>.csv") is saved next to each PDF.
cd /d "%~dp0"
if "%~1"=="" (
  echo Drag Commercial Invoice PDF files onto convert.bat
  pause
  goto :eof
)
if not exist ".venv\Scripts\python.exe" (
  echo First run: installing Python packages ...
  python -m venv .venv && ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
)
".venv\Scripts\python.exe" -m civconvert convert %*
pause
