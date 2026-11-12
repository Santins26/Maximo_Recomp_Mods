@echo off
rem Maximo-GTG: builds the game from your own disc. See README.md.
rem   build.cmd <your disc .iso, .bin/.cue or extracted disc folder> [--no-ffmpeg]
rem The PAL (SLES-50703), USA (SLUS-20017) and Japanese (SLPM-62127) discs all work. A PAL disc builds into build,
rem generated and dist as always; a USA or Japanese one into build-usa / generated-usa / dist-usa (or -jpn), so the
rem builds of different releases can sit side by side.
rem MAXIMO_CMAKE_ARGS, if set, is passed to every CMake configure.
setlocal EnableExtensions
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "BUILD=%ROOT%\build"
set "GEN=%ROOT%\generated"
set "DIST=%ROOT%\dist"
set "DISC=%~f1"
set "USE_FFMPEG=1"
if /i "%~2"=="--no-ffmpeg" set "USE_FFMPEG=0"
set "FFMPEG_NAME=ffmpeg-n7.1-241205-lgpl-amd64-shared"
set "FFMPEG_URL=https://github.com/System233/ffmpeg-msvc-prebuilt/releases/download/n7.1-241205/%FFMPEG_NAME%.zip"
set "FFMPEG_SHA256=B985887DBCA8C0A9A4C47F9D2BAC7EC3AB994335B1B7FE7000BDB1DCD772AC68"

if "%~1"=="" goto usage
if not exist "%DISC%" (
    echo Error: "%DISC%" does not exist.
    exit /b 1
)

set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" goto no_vs
set "VS="
for /f "usebackq delims=" %%i in (`call "%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VS=%%i"
if "%VS%"=="" goto no_vs
call "%VS%\VC\Auxiliary\Build\vcvars64.bat" >nul || goto no_vs
where cmake >nul 2>nul || goto no_cmake
where ninja >nul 2>nul || goto no_cmake

if exist "%ROOT%\external\PS2Recomp\CMakeLists.txt" if exist "%ROOT%\external\parallel-gs\Granite\third_party\volk\volk.h" goto have_sources
where git >nul 2>nul || goto no_git
echo [1/6] Downloading the source dependencies...
rem core.longpaths: SPIRV-Cross has test files whose paths are too long for Windows otherwise.
git -C "%ROOT%" -c core.longpaths=true submodule update --init --recursive || exit /b 1
:have_sources

echo [2/6] Building the tools...
cmake -S "%ROOT%" -B "%BUILD%" -G Ninja -DCMAKE_BUILD_TYPE=Release -DMAXIMO_TOOLS_ONLY=ON %MAXIMO_CMAKE_ARGS% || exit /b 1
cmake --build "%BUILD%" --target ps2_recomp ps2x_vu1aot maximo_kit || exit /b 1

echo [3/6] Reading your disc...
set "REGION="
for /f "usebackq delims=" %%r in (`call "%BUILD%\maximo_kit.exe" region "%DISC%"`) do set "REGION=%%r"
if "%REGION%"=="" (
    "%BUILD%\maximo_kit.exe" region "%DISC%"
    exit /b 1
)
set "GAMEBUILD=%BUILD%"
set "REGION_ARGS=-DMAXIMO_REGION=PAL"
if /i "%REGION%"=="PAL" goto region_set
if /i "%REGION%"=="USA" set "SUFFIX=-usa"
if /i "%REGION%"=="JPN" set "SUFFIX=-jpn"
set "GEN=%ROOT%\generated%SUFFIX%"
set "DIST=%ROOT%\dist%SUFFIX%"
set "GAMEBUILD=%ROOT%\build%SUFFIX%"
set "REGION_ARGS=-G Ninja -DCMAKE_BUILD_TYPE=Release -DMAXIMO_REGION=%REGION% -DGENERATED_DIR=%GEN:\=/%/game -DMAXIMO_VU1AOT_DIR=%GEN:\=/%/vu1aot"
:region_set
echo The disc is the %REGION% release: building into "%DIST%".
rem rebuild.cmd with no release named rebuilds this one.
> "%ROOT%\build_region.txt" echo %REGION%
if exist "%GEN%" rmdir /s /q "%GEN%"
"%BUILD%\maximo_kit.exe" prepare "%DISC%" "%ROOT%" "%GEN%" || exit /b 1

echo [4/6] Recompiling the game code...
"%BUILD%\ps2recomp\ps2xRecomp\ps2_recomp.exe" "%GEN%\recomp.toml" > "%GEN%\recomp.log" 2>&1
if errorlevel 1 (
    echo Error: the recompiler failed, see "%GEN%\recomp.log".
    exit /b 1
)
for /f "usebackq delims=" %%n in ("%GEN%\vu1_images.txt") do (
    "%BUILD%\ps2recomp\ps2xRuntime\ps2x_vu1aot.exe" "%GEN%\vu1\%%n.bin" "%GEN%\vu1\%%n.pcs" "%GEN%\vu1aot\%%n.cpp" || exit /b 1
)

set "FFMPEG_ARG=-DMAXIMO_FFMPEG_DIR="
if "%USE_FFMPEG%"=="0" goto ffmpeg_done
if exist "%ROOT%\deps\ffmpeg\bin\avcodec-61.dll" goto ffmpeg_ready
if not exist "%ROOT%\deps" mkdir "%ROOT%\deps"
set "FFMPEG_ZIP=%ROOT%\deps\%FFMPEG_NAME%.zip"
if exist "%FFMPEG_ZIP%" goto ffmpeg_check
echo Downloading FFmpeg (LGPL, plays the movies faster)...
"%SystemRoot%\System32\curl.exe" -L --fail -o "%FFMPEG_ZIP%" "%FFMPEG_URL%" || exit /b 1
:ffmpeg_check
set "FFMPEG_GOT="
for /f "usebackq delims=" %%h in (`call "%BUILD%\maximo_kit.exe" sha256 "%FFMPEG_ZIP%"`) do set "FFMPEG_GOT=%%h"
if /i not "%FFMPEG_GOT%"=="%FFMPEG_SHA256%" (
    echo Error: the FFmpeg download does not match its hash. Delete "%FFMPEG_ZIP%" and try again.
    exit /b 1
)
mkdir "%ROOT%\deps\ffmpeg"
"%SystemRoot%\System32\tar.exe" -xf "%FFMPEG_ZIP%" -C "%ROOT%\deps\ffmpeg" || exit /b 1
:ffmpeg_ready
set "FFMPEG_ARG=-DMAXIMO_FFMPEG_DIR=%ROOT:\=/%/deps/ffmpeg"
:ffmpeg_done

echo [5/6] Compiling the game, which takes a while...
cmake -S "%ROOT%" -B "%GAMEBUILD%" -DMAXIMO_TOOLS_ONLY=OFF %REGION_ARGS% "%FFMPEG_ARG%" %MAXIMO_CMAKE_ARGS% || exit /b 1
cmake --build "%GAMEBUILD%" --target maximo_boot || exit /b 1

echo [6/6] Installing into dist...
if not exist "%DIST%\game" mkdir "%DIST%\game"
copy /y "%GAMEBUILD%\maximo_boot.exe" "%DIST%\game\" >nul || exit /b 1
copy /y "%GEN%\region.txt" "%DIST%\game\" >nul || exit /b 1
if "%USE_FFMPEG%"=="1" copy /y "%GAMEBUILD%\*.dll" "%DIST%\game\" >nul
call "%ROOT%\launcher\build.cmd" "%DIST%" || exit /b 1
copy /y "%ROOT%\LICENSE" "%DIST%\" >nul
copy /y "%ROOT%\THIRD_PARTY_NOTICES.txt" "%DIST%\" >nul
xcopy /y /q /i "%ROOT%\LICENSES" "%DIST%\LICENSES" >nul

echo.
echo Done. Open "%DIST%\MaximoLauncher.exe" and choose your disc under Game Files.
echo Keep the dist folder to yourself: it contains the game's code.
exit /b 0

:usage
echo usage: build.cmd ^<your disc .iso, .cue or extracted disc folder^> [--no-ffmpeg]
exit /b 2
:no_vs
echo Error: Visual Studio with the "Desktop development with C++" workload was not found.
exit /b 1
:no_cmake
echo Error: CMake or Ninja was not found. Add "C++ CMake tools for Windows" in the Visual Studio Installer.
exit /b 1
:no_git
echo Error: Git was not found, and the source dependencies are missing.
exit /b 1
