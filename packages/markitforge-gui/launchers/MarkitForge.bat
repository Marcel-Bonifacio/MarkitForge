@echo off
rem SPDX-FileCopyrightText: 2026-present MarkitForge contributors
rem SPDX-License-Identifier: Apache-2.0
rem MarkitForge launcher for Windows. Double-click it in File Explorer.
rem
rem First run: creates a private Python environment in "launchers\.venv" and
rem installs MarkitForge from this folder's checkout. Later runs start straight
rem away. To reinstall from scratch, delete the "launchers\.venv" folder.

setlocal
title MarkitForge
set "HERE=%~dp0"
for %%I in ("%HERE%..\..\..") do set "REPO=%%~fI"
set "VENV=%HERE%.venv"
set "READY=%VENV%\.markitforge-ready"
set "CORE=%REPO%\packages\markitforge"
set "GUI=%REPO%\packages\markitforge-gui"

if not exist "%CORE%\pyproject.toml" goto bad_location
if exist "%READY%" goto run

echo Setting up MarkitForge for the first time.
echo This downloads a few hundred MB and can take several minutes.
echo.
if exist "%VENV%" rmdir /s /q "%VENV%"

where uv >nul 2>nul
if errorlevel 1 goto setup_pip

uv venv --python ">=3.10,<3.15" "%VENV%"
if errorlevel 1 goto fail
uv pip install --python "%VENV%\Scripts\python.exe" -e "%CORE%[all]" -e "%GUI%"
if errorlevel 1 goto fail
goto setup_done

:setup_pip
set "PY="
py -3 -c "import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] < (3, 15) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if defined PY goto have_python
python -c "import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] < (3, 15) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=python"
if defined PY goto have_python
echo MarkitForge needs Python 3.10 or newer.
echo Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH")
echo and then double-click this file again.
goto pause_exit

:have_python
%PY% -m venv "%VENV%"
if errorlevel 1 goto fail
"%VENV%\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto fail
"%VENV%\Scripts\python.exe" -m pip install -e "%CORE%[all]" -e "%GUI%"
if errorlevel 1 goto fail

:setup_done
echo ok> "%READY%"
echo.
echo Setup complete.

:run
echo Starting MarkitForge. Your browser will open in a moment.
echo Keep this window open while you use MarkitForge; close it to stop.
echo.
"%VENV%\Scripts\markitforge-gui.exe" %*
if errorlevel 1 goto pause_exit
exit /b 0

:bad_location
echo This launcher must stay inside the MarkitForge folder
echo (packages\markitforge-gui\launchers). It could not find:
echo   %CORE%
goto pause_exit

:fail
echo.
echo Setup did not finish. Nothing was changed outside this folder.
echo Check your internet connection and try again.
if exist "%VENV%" rmdir /s /q "%VENV%"

:pause_exit
echo.
pause
exit /b 1
