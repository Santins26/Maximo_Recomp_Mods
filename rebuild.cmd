@echo off
rem Quick rebuild after you change code. Needs one full build.cmd run first; it does not read the disc again.
rem   rebuild.cmd            recompiles maximo_boot.cpp (only what changed) and the launcher, into dist
rem   rebuild.cmd launcher   rebuilds only the launcher (a few seconds, no Visual Studio needed)
rem   rebuild.cmd usa / jpn / pal   a given release's build (build-usa into dist-usa, -jpn likewise, pal: build/dist)
rem   rebuild.cmd usa launcher
rem With no release named it rebuilds the one build.cmd built last (build_region.txt).
rem Close the game and the launcher first: Windows will not overwrite a running .exe.
setlocal EnableExtensions
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "BUILD=%ROOT%\build"
set "DIST=%ROOT%\dist"
set "REGION="
if /i "%~1"=="usa" set "REGION=USA"
if /i "%~1"=="jpn" set "REGION=JPN"
if /i "%~1"=="pal" set "REGION=PAL"
if defined REGION (
    shift
) else (
    if exist "%ROOT%\build_region.txt" set /p REGION=<"%ROOT%\build_region.txt"
)
if not defined REGION set "REGION=PAL"
set "REGION=%REGION: =%"
if /i "%REGION%"=="USA" set "SUFFIX=-usa"
if /i "%REGION%"=="JPN" set "SUFFIX=-jpn"
if defined SUFFIX (
    set "BUILD=%ROOT%\build%SUFFIX%"
    set "DIST=%ROOT%\dist%SUFFIX%"
)
echo Rebuilding the %REGION% release into "%DIST%".
rem Keep runtime_overlay\ (committed with the project) in step with the submodules' changed files.
if exist "%ROOT%\sync_overlay.cmd" call "%ROOT%\sync_overlay.cmd" >nul

if /i "%~1"=="launcher" (
    call "%ROOT%\launcher\build.cmd" "%DIST%"
    exit /b %errorlevel%
)

if not exist "%BUILD%\build.ninja" (
    echo Error: no earlier build found. Run build.cmd with your disc once first.
    exit /b 1
)
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
set "VS="
if exist "%VSWHERE%" for /f "usebackq delims=" %%i in (`call "%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VS=%%i"
if "%VS%"=="" (
    echo Error: Visual Studio with the Desktop development with C++ workload was not found.
    exit /b 1
)
call "%VS%\VC\Auxiliary\Build\vcvars64.bat" >nul
if errorlevel 1 exit /b 1

echo [1/3] Compiling the game, only what changed...
cmake --build "%BUILD%" --target maximo_boot
if errorlevel 1 exit /b 1

echo [2/3] Copying the game into dist...
if not exist "%DIST%\game" mkdir "%DIST%\game"
copy /y "%BUILD%\maximo_boot.exe" "%DIST%\game\" >nul
if errorlevel 1 (
    echo Error: could not copy maximo_boot.exe. Close the game and try again.
    exit /b 1
)

echo [3/3] Building the launcher...
call "%ROOT%\launcher\build.cmd" "%DIST%"
if errorlevel 1 (
    echo Error: the launcher did not build. Close the launcher if it is open, and read the message above.
    exit /b 1
)

echo.
echo Done. Open "%DIST%\MaximoLauncher.exe".
exit /b 0
