@echo off
rem Offline Survival Kit launcher (Windows 10/11, 64-bit). Usage: start.bat   or   start.bat lan
rem Picks the biggest model in models\ that fits this PC's RAM. Override: set MODEL=models\x.gguf  /  set CTX=4096
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set BIND=127.0.0.1
if /i "%~1"=="lan" set BIND=0.0.0.0
set REASON=
set PICKED=
for /f "usebackq tokens=1,* delims==" %%a in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\pick-model.ps1" 2^>nul`) do (
  set "%%a=%%b"
  set PICKED=1
)
if not defined PICKED (
  rem PowerShell unavailable: fall back to the first model found.
  if not defined MODEL for %%m in (models\*.gguf) do (echo %%m | findstr /i mmproj >nul || if not defined MODEL set "MODEL=%%m")
  if not defined CTX set CTX=8192
  set REASON=fallback: first model found
  if defined MODEL if exist "!MODEL:.gguf=.mmproj.gguf!" set "MMPROJ=!MODEL:.gguf=.mmproj.gguf!"
)
if not defined MODEL (echo No .gguf model found in models\ & pause & exit /b 1)
set MMARG=
if defined MMPROJ set MMARG=--mmproj "%MMPROJ%"
set ZIMS=
for %%z in (library\*.zim) do set ZIMS=!ZIMS! "%%z"
echo Starting AI model (%MODEL%, context %CTX%) - %REASON% ...
start "Offline Kit - AI (close to stop)" /min bin\llamafile.exe --server -m "%MODEL%" %MMARG% --host %BIND% --port 8080 -c %CTX%
if not "%ZIMS%"=="" (echo Starting library ... & start "Offline Kit - Library" /min bin\windows\kiwix-serve.exe --address=%BIND% --port=8081 %ZIMS%)
echo Starting home page, manuals and maps ...
start "Offline Kit - Web" /min bin\windows\miniserve.exe www --index index.html -i %BIND% -p 8000 -q
timeout /t 3 >nul
start "" http://localhost:8000/
echo.
echo   Open http://localhost:8000/  (the AI needs 10-60 seconds to load)
if "%BIND%"=="0.0.0.0" (echo   Phones on the same Wi-Fi: http://THIS-PC-IP:8000/  - allow the firewall prompts & ipconfig | findstr IPv4)
echo   Run stop.bat (or close the three minimised windows) to stop.
pause
