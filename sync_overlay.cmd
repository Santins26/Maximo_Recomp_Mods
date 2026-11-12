@echo off
rem Copies the files this project changes inside the submodules (external\PS2Recomp, external\parallel-gs; the list is
rem overlay_files.txt) into runtime_overlay\, which is committed with the project. The submodules point at upstream
rem commits that do not have these changes, so a fresh clone (the launcher's Build button) gets them back from here
rem with apply_overlay.cmd. Run this before you commit and push.
setlocal
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set /a COUNT=0
for /f "usebackq eol=# delims=" %%f in ("%ROOT%\overlay_files.txt") do call :one "%%f"
echo %COUNT% files copied into runtime_overlay.
exit /b 0

:one
set "F=%~1"
set "F=%F:/=\%"
if not exist "%ROOT%\%F%" (
    echo missing: %F%
    exit /b 0
)
for %%d in ("%ROOT%\runtime_overlay\%F%") do if not exist "%%~dpd" mkdir "%%~dpd"
copy /y "%ROOT%\%F%" "%ROOT%\runtime_overlay\%F%" >nul
set /a COUNT+=1
exit /b 0
