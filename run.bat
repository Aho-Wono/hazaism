@echo off
rem ScrapNest launcher. Double-click to start; the browser opens automatically.
rem (ASCII only on purpose: cmd.exe can misread non-ASCII text in .bat files.)
setlocal
cd /d "%~dp0"
title ScrapNest

where uv >nul 2>nul
if not errorlevel 1 goto have_uv
if exist "%USERPROFILE%\.local\bin\uv.exe" goto add_path

echo [ScrapNest] First run: installing "uv" (it downloads Python and the libraries)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
if errorlevel 1 goto no_uv

:add_path
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where uv >nul 2>nul
if errorlevel 1 goto no_uv

:have_uv
echo [ScrapNest] Starting... (the first run downloads libraries and can take a few minutes)
echo [ScrapNest] To quit, close this window or press Ctrl+C.
uv run --no-dev python -m scrapnest %*
if errorlevel 1 pause
exit /b

:no_uv
echo.
echo [ScrapNest] Could not install or find "uv".
echo             Install it from https://docs.astral.sh/uv/ and run this file again.
pause
exit /b 1
