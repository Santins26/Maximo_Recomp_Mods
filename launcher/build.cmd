@echo off
rem Builds MaximoLauncher.exe with the C# compiler that ships with Windows (.NET Framework 4.8). No downloads.
rem   build.cmd [output-dir=.]
setlocal
set OUT=%~1
if "%OUT%"=="" set OUT=%~dp0
rem Players get no test switches. Set MAXIMO_LAUNCHER_DEV=1 to keep them.
set DEFINES=
if "%MAXIMO_LAUNCHER_DEV%"=="1" set DEFINES=/define:DEV_SWITCHES
"%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /target:winexe /optimize+ /platform:x64 %DEFINES% /win32icon:"%~dp0maximo.ico" ^
  /out:"%OUT%\MaximoLauncher.exe" ^
  /r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.IO.Compression.dll /r:System.IO.Compression.FileSystem.dll ^
  "%~dp0MaximoLauncher.cs" "%~dp0LauncherForm.cs" "%~dp0GameArt.cs" "%~dp0HdTextures.cs" "%~dp0Builder.cs" "%~dp0Controls.cs" "%~dp0MoreEnemies.cs" "%~dp0CrossWorld.cs" "%~dp0BuildGame.cs" "%~dp0DiscImage.cs"
if errorlevel 1 exit /b 1
xcopy /y /q /i "%~dp0art\*.png" "%OUT%\art" >nul
