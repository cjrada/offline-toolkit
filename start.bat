@echo off
rem Offline Survival Kit launcher (Windows 10/11, 64-bit). Usage: start.bat   or   start.bat lan
rem Picks the biggest model in models\ that fits this PC's GPU or RAM; switch models on the home page.
rem Override: set MODEL=models\x.gguf  /  set CTX=4096  /  set GPU=disable
setlocal EnableDelayedExpansion
title Offline Kit - launcher (close to stop)
cd /d "%~dp0"
set BIND=127.0.0.1
if /i "%~1"=="lan" set BIND=0.0.0.0
set "CTL=%TEMP%\offline-toolkit-control"
set "USER_CTX=%CTX%"
rem Thinking off by default (Qwen3 1.7B/8B otherwise burn the answer budget on hidden reasoning).
if not defined REASONING set REASONING=off
if /i not "%REASONING%"=="off" if /i not "%REASONING%"=="on" if /i not "%REASONING%"=="auto" (echo REASONING must be on, off or auto & pause & exit /b 1)
rem Per-run secret for model-switch requests; only www\models.json carries it (other sites cannot read it).
set SWITCH_TOKEN=
for /f %%t in ('powershell -NoProfile -Command "[guid]::NewGuid().ToString('N')"') do set "SWITCH_TOKEN=%%t"
set "PICK=powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\pick-model.ps1" -Json www\models.json"
set REASON=
set PICKED=
for /f "usebackq tokens=1,* delims==" %%a in (`%PICK% 2^>nul`) do (
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
call :start_llm
set ZIMS=
for %%z in (library\*.zim) do set ZIMS=!ZIMS! "%%z"
if not "%ZIMS%"=="" (echo Starting library ... & start "Offline Kit - Library" /min bin\windows\kiwix-serve.exe --address=%BIND% --port=8081 %ZIMS%)
echo Starting home page, manuals and maps ...
start "Offline Kit - Web" /min bin\windows\miniserve.exe www --index index.html -i %BIND% -p 8000 -q
rem Model switching: a second miniserve on 127.0.0.1 ONLY, accepting uploads into a temp folder. The home
rem page uploads switch.txt; pick-model.ps1 reads and validates it (must be an existing model in models\).
if exist "%CTL%" rmdir /s /q "%CTL%"
mkdir "%CTL%"
start "Offline Kit - Switch" /min bin\windows\miniserve.exe "%CTL%" -i 127.0.0.1 -p 8079 -u / -o overwrite -q
ping -n 4 127.0.0.1 >nul
start "" http://localhost:8000/
echo.
echo   Open http://localhost:8000/  (the AI needs 10-60 seconds to load; switch models on the home page)
if "%BIND%"=="0.0.0.0" (echo   Phones on the same Wi-Fi: http://THIS-PC-IP:8000/  - allow the firewall prompts & ipconfig | findstr IPv4)
echo   Run stop.bat (or close this window and the minimised ones) to stop.

:watch
ping -n 2 127.0.0.1 >nul
if not exist "%CTL%\switch.txt" goto watch
move /y "%CTL%\switch.txt" "%CTL%\request.txt" >nul
set "CTX=%USER_CTX%"
set PICKED=
for /f "usebackq tokens=1,* delims==" %%a in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\pick-model.ps1" -Json www\models.json -RequestFile "%CTL%\request.txt" 2^>nul`) do (
  set "%%a=%%b"
  set PICKED=1
)
del /q "%CTL%\*" >nul 2>&1
if not defined PICKED (echo Ignored an invalid model-switch request. & goto watch)
echo Switching model ...
taskkill /f /im llamafile.exe >nul 2>&1
ping -n 3 127.0.0.1 >nul
call :start_llm
goto watch

:start_llm
set MMARG=
if defined MMPROJ set MMARG=--mmproj "%MMPROJ%"
set GPUARG=
if defined GPU set GPUARG=--gpu %GPU%
echo Starting AI model (%MODEL%, context %CTX%) - %REASON% ...
start "Offline Kit - AI" /min bin\llamafile.exe --server -m "%MODEL%" %MMARG% --host %BIND% --port 8080 -c %CTX% --reasoning %REASONING% %GPUARG%
exit /b 0
