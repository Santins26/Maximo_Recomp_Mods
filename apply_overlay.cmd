@echo off
rem Puts this project's changes to the submodules (runtime_overlay\, made by sync_overlay.cmd) over a fresh checkout of
rem external\PS2Recomp and external\parallel-gs. A file is only copied when it differs, so a build tree that already
rem has them is not touched (and nothing is rebuilt for it). The launcher's Build button runs this after downloading.
setlocal
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
if not exist "%ROOT%\runtime_overlay\external" (
    echo No runtime_overlay folder: nothing to apply.
    exit /b 0
)
set /a COUNT=0
for /f "usebackq eol=# delims=" %%f in ("%ROOT%\overlay_files.txt") do call :one "%%f"
echo %COUNT% changed runtime files applied.
exit /b 0

:one
set "F=%~1"
set "F=%F:/=\%"
if not exist "%ROOT%\runtime_overlay\%F%" exit /b 0
fc /b "%ROOT%\runtime_overlay\%F%" "%ROOT%\%F%" >nul 2>&1
if not errorlevel 1 exit /b 0
for %%d in ("%ROOT%\%F%") do if not exist "%%~dpd" mkdir "%%~dpd"
copy /y "%ROOT%\runtime_overlay\%F%" "%ROOT%\%F%" >nul || exit /b 1
set /a COUNT+=1
exit /b 0
