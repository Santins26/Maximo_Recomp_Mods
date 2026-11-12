@echo off
rem Makes mod_package\Maximo-GTG-Mods.zip: the launcher with its Build button, BUILD_GAME.cmd and the changed source
rem files. Players unzip it, open MaximoLauncher.exe and press Build with their own disc. No game code goes in.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher\package\make_package.ps1"
if errorlevel 1 (
    echo.
    echo The package was not made, see the message above.
    pause
    exit /b 1
)
pause
