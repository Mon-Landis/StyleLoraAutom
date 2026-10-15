@echo off
rem StyleLoraAutom one-click launcher.
rem   start.bat            first run: create .venv, install deps, build frontend, then serve
rem   start.bat --rebuild  force rebuild of frontend\dist (after pulling frontend changes)
rem Extra serve flags can be passed via env var STYLELORA_ARGS (e.g. --no-browser).
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
set "PORT=8000"

if not exist "%PY%" (
  echo [setup] Creating Python virtual environment...
  python -m venv .venv || goto :fail
  "%PY%" -m pip install -r requirements.txt || goto :fail
  "%PY%" -m pip install -e . --no-deps || goto :fail
)

set "REBUILD="
if /i "%~1"=="--rebuild" set "REBUILD=1"
if not exist "frontend\dist\index.html" set "REBUILD=1"
if defined REBUILD (
  echo [setup] Building frontend...
  pushd frontend
  if not exist "node_modules" (
    call npm.cmd ci || (popd & goto :fail)
  )
  call npm.cmd run build || (popd & goto :fail)
  popd
)

echo [run] Serving on http://127.0.0.1:%PORT%  (close this window or press Ctrl+C to stop)
"%PY%" -m stylelora serve --port %PORT% %STYLELORA_ARGS%
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" exit /b 0
if "%RC%"=="4" (
  echo [run] An instance is already running; opening it in the browser...
  "%PY%" -m stylelora ui && exit /b 0
)
goto :fail

:fail
echo.
echo [error] Launch failed (exit code %RC%).
pause
exit /b 1
