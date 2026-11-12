@echo off
rem Maximo-GTG mods: builds the game from YOUR OWN disc. Nothing from the game is in this package.
rem The launcher's Build / Update button runs this; it also works by hand:
rem   BUILD_GAME.cmd "C:\path\to\your\Maximo.iso"   full build (or a folder with the disc's files)
rem   BUILD_GAME.cmd --update                       after unzipping a newer package: recompiles only what changed
rem It downloads the Maximo-GTG project into .\source (at the commit in overlay\BASE.txt), puts the files in .\overlay
rem over it, runs its build.cmd with your disc and sets the disc up for the launcher it built (.\source\dist).
rem Exit code 3: this update needs a full build (choose the disc).
setlocal EnableExtensions
set "HERE=%~dp0"
set "HERE=%HERE:~0,-1%"
set "REPO=%HERE%\source"
set "BASE="
if exist "%HERE%\overlay\BASE.txt" set /p BASE=<"%HERE%\overlay\BASE.txt"
if "%BASE%"=="" (
    echo Error: overlay\BASE.txt is missing. Unzip the whole package again.
    exit /b 1
)
if "%~1"=="" (
    echo Usage: BUILD_GAME.cmd "C:\path\to\your\Maximo.iso"   or   BUILD_GAME.cmd --update
    exit /b 2
)
set "HEAD="
if exist "%REPO%\.git" for /f "usebackq delims=" %%h in (`git -C "%REPO%" rev-parse HEAD 2^>nul`) do set "HEAD=%%h"
if /i "%~1"=="--update" goto update

set "DISC=%~f1"
if not exist "%DISC%" (
    echo Error: "%DISC%" does not exist.
    exit /b 1
)
where git >nul 2>nul || (
    echo Error: Git is not installed. Get it from https://git-scm.com/ and press Build again.
    exit /b 1
)
if not exist "%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe" (
    echo Error: Visual Studio was not found. Install Visual Studio Community with "Desktop development with C++"
    echo        ^(including "C++ CMake tools for Windows"^) from https://visualstudio.microsoft.com/ and press Build again.
    exit /b 1
)

echo [1/4] Downloading the Maximo-GTG project...
if /i "%HEAD%"=="%BASE%" (
    echo Already there.
    goto have_project
)
if not exist "%REPO%\.git" (
    git clone --progress https://github.com/BioRhythmicStudios/Maximo-GTG.git "%REPO%" || exit /b 1
)
git -C "%REPO%" fetch --progress origin || exit /b 1
git -C "%REPO%" checkout --force %BASE% || exit /b 1
git -C "%REPO%" -c core.longpaths=true submodule update --init --recursive --force --progress || exit /b 1
:have_project

echo [2/4] Adding the mods...
call :overlay || exit /b 1

echo [3/4] Building the game from your disc (the first time takes a good while)...
call "%REPO%\build.cmd" "%DISC%"
if errorlevel 1 (
    echo Error: the build stopped, see the messages above.
    exit /b 1
)

echo [4/4] Setting up your disc for the launcher...
start "" /wait "%REPO%\dist\MaximoLauncher.exe" --setup "%DISC%"
if not exist "%REPO%\dist\disc\SLES_507.03" (
    echo Error: the disc could not be set up. Open the launcher, go to Game Files and choose your disc there.
    exit /b 1
)
goto done

:update
rem Same project commit and an earlier build: only the changed files are copied and recompiled (like rebuild.cmd).
if not exist "%REPO%\build\build.ninja" goto need_full
if not exist "%REPO%\dist\disc\SLES_507.03" goto need_full
if not exist "%REPO%\rebuild.cmd" goto need_full
if /i not "%HEAD%"=="%BASE%" goto need_full
echo [1/2] Adding the new mod files...
call :overlay || exit /b 1
echo [2/2] Recompiling what changed...
call "%REPO%\rebuild.cmd"
if errorlevel 1 (
    echo Error: the update stopped, see the messages above. Close the game if it is open and press Update again.
    exit /b 1
)
goto done

:need_full
echo This update needs a full build (the base project changed, or there is no earlier build).
echo Choose your disc to build it.
exit /b 3

:overlay
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%\apply_overlay.ps1" "%HERE%\overlay" "%REPO%"
if errorlevel 1 (
    echo Error: could not copy the mod files into "%REPO%".
    exit /b 1
)
exit /b 0

:done
copy /y "%HERE%\overlay\VERSION.txt" "%REPO%\built_version.txt" >nul
echo.
echo Done. Open MaximoLauncher.exe: it goes straight to the game.
echo Keep the source folder to yourself: it contains the game's code once built.
exit /b 0
