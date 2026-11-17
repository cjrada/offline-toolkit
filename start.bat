@echo off
rem Offline Survival Kit launcher (Windows 10/11, 64-bit). Usage: start.bat   or   start.bat lan
rem Picks the biggest model in models\ that fits this PC's GPU or RAM; switch models on the home page.
rem Override: set MODEL=models\x.gguf  /  set CTX=4096  /  set GPU=disable
rem llamafile 0.10.6 GPU (Vulkan) workarounds, found on an RTX 2070 Max-Q / driver 551.61:
rem   OBS Studio's implicit Vulkan layer crashes Vulkan init (0xC0000005) - disabled for llamafile with
rem   DISABLE_VULKAN_OBS_CAPTURE=1 and VK_LOADER_LAYERS_DISABLE=~implicit~ (your own values are kept);
rem   Vulkan flash attention segfaults at higher -ngl / context - GPU runs pass "-fa off" (set FA=on to undo).
rem   Also: set NGL=33 (GPU layers; default: llamafile decides).
rem If the AI exits, or is not ready in time, on the GPU it is restarted once on the CPU - unless you
rem set GPU yourself (e.g. GPU=vulkan). The home page's "Use CPU only" toggle switches CPU only / GPU.
setlocal EnableDelayedExpansion
title Offline Kit - launcher (close to stop)
cd /d "%~dp0"
set BIND=127.0.0.1
if /i "%~1"=="lan" set BIND=0.0.0.0
set "CTL=%TEMP%\offline-toolkit-control"
set "USER_CTX=%CTX%"
set "USER_MODEL=%MODEL%"
set "USER_GPU=%GPU%"
set "USER_FA=%FA%"
set "FA_GPU=%FA%"
if not defined FA_GPU set FA_GPU=off
if /i not "%FA_GPU%"=="off" if /i not "%FA_GPU%"=="on" if /i not "%FA_GPU%"=="auto" (echo FA must be on, off or auto & pause & exit /b 1)
if defined NGL (echo %NGL%| findstr /r /x "[0-9][0-9]*" >nul || (echo NGL must be a number of layers & pause & exit /b 1))
if not defined DISABLE_VULKAN_OBS_CAPTURE set DISABLE_VULKAN_OBS_CAPTURE=1
if not defined VK_LOADER_LAYERS_DISABLE set "VK_LOADER_LAYERS_DISABLE=~implicit~"
if defined GPU (echo %GPU%| findstr /r /x "[A-Za-z0-9_-]*" >nul || (echo GPU must be disable or a llamafile --gpu value & pause & exit /b 1))
if not defined HEALTH_URL set HEALTH_URL=http://127.0.0.1:8080/health
rem Load watch: tasklist + /health via curl.exe (Windows 10 1803+); LOAD_TIMEOUT comes from pick-model.ps1
rem (60 s + 1 s per 50 MB of model, or HEALTH_TIMEOUT). Without curl.exe only the exit watch runs.
set HAVE_CURL=
where curl.exe >nul 2>&1 && set HAVE_CURL=1
rem GPU_FALLBACK=1 once GPU mode has failed; later switches stay on the CPU until the home-page toggle.
set GPU_FALLBACK=
rem LLM_RUN counts llamafile starts; models.json carries it so the home page sees each restart.
set LLM_RUN=1
set LOAD_TIMEOUT=180
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
rem Model switching and the "Use CPU only" toggle: a second miniserve on 127.0.0.1 ONLY, accepting uploads
rem into a temp folder. The home page uploads switch.txt; pick-model.ps1 validates it (per-run token, an
rem existing model in models\, optional line 3 exactly cpu=on or cpu=off).
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
if defined LLM_UP (tasklist /fi "imagename eq llamafile.exe" /nh 2>nul | find /i "llamafile.exe" >nul || goto llm_exited)
if defined LLM_UP if not defined LLM_READY call :check_health
if defined LLM_HUNG goto llm_hung
if not exist "%CTL%\switch.txt" goto watch
move /y "%CTL%\switch.txt" "%CTL%\request.txt" >nul
set REQ_MODEL=
set REQ_CPU=
for /f "usebackq tokens=1,* delims==" %%a in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\pick-model.ps1" -RequestFile "%CTL%\request.txt" -CheckOnly 2^>nul`) do set "%%a=%%b"
del /q "%CTL%\*" >nul 2>&1
if not defined REQ_MODEL (echo Ignored an invalid model-switch request. & goto watch)
if not defined REQ_CPU (echo Ignored an invalid model-switch request. & goto watch)
if /i "%REQ_CPU%"=="on" (
  echo Switching to CPU only ...
  set GPU=disable
  set GPU_FALLBACK=
)
rem Back to the GPU path even after a fallback this run, so the user can try the GPU again.
if /i "%REQ_CPU%"=="off" (
  echo Switching to GPU / auto ...
  set "GPU=%USER_GPU%"
  if /i "%USER_GPU%"=="disable" set GPU=
  set GPU_FALLBACK=
)
rem A mode toggle on an auto-picked model picks again for the new mode; otherwise use the requested model.
set AUTO_PICK=
if /i not "%REQ_CPU%"=="keep" if not defined USER_MODEL if /i "%REQ_MODEL%"=="%MODEL%" set AUTO_PICK=1
if defined AUTO_PICK (set "MODEL=") else (set "MODEL=%REQ_MODEL%" & set "USER_MODEL=%REQ_MODEL%" & echo Switching model to %REQ_MODEL% ...)
set "CTX=%USER_CTX%"
set /a LLM_RUN+=1
set PICKED=
for /f "usebackq tokens=1,* delims==" %%a in (`%PICK% 2^>nul`) do (
  set "%%a=%%b"
  set PICKED=1
)
if not defined PICKED (
  rem PowerShell failed: use the requested model as-is.
  set "MODEL=%REQ_MODEL%"
  set MMPROJ=
  if not defined CTX set CTX=4096
  set "REASON=chosen on the home page"
)
taskkill /f /im llamafile.exe >nul 2>&1
ping -n 3 127.0.0.1 >nul
call :start_llm
goto watch

rem llamafile exited by itself (we only kill it to switch models or stop). start "..." hides its exit
rem code, so tasklist is how we notice. With a GPU the usual cause is a Vulkan/driver failure or running
rem out of GPU memory, so retry once on the CPU, re-picking the model against RAM. Skipped if GPU was
rem set by hand (GPU=vulkan etc.). The home-page toggle can try the GPU again.
:llm_exited
set LLM_UP=
if defined GPU goto llm_dead
set "FAIL_WHY=AI process exited"
goto cpu_fallback

rem Alive but never answered /health within LOAD_TIMEOUT: a hang during load. Retry on the CPU if this
rem was a GPU start; a slow CPU start is only reported (there is nothing better to try).
:llm_hung
set LLM_HUNG=
if defined GPU (
  echo The AI model is still loading after %LOAD_TIMEOUT% s - leaving it running.
  set LLM_READY=slow
  goto watch
)
set "FAIL_WHY=AI not ready after %LOAD_TIMEOUT% s"
set LLM_UP=
taskkill /f /im llamafile.exe >nul 2>&1
ping -n 3 127.0.0.1 >nul
goto cpu_fallback

:cpu_fallback
echo GPU start failed (%FAIL_WHY%) - fell back to the CPU.
set GPU=disable
set GPU_FALLBACK=1
set "MODEL=%USER_MODEL%"
set "CTX=%USER_CTX%"
set /a LLM_RUN+=1
set PICKED=
for /f "usebackq tokens=1,* delims==" %%a in (`%PICK% 2^>nul`) do (
  set "%%a=%%b"
  set PICKED=1
)
if not defined PICKED (
  rem PowerShell unavailable: keep the model that was running.
  set "MODEL=%LAST_MODEL%"
  if not defined CTX set CTX=4096
  set "REASON=GPU mode failed, running on the CPU"
)
call :start_llm
goto watch

:llm_dead
echo The AI model stopped. The library, manuals and maps keep working.
echo Pick another model on the home page, or close everything and run start.bat again.
goto watch

:check_health
rem Each watch pass is ~1 s (ping) plus tasklist/curl time, so this count runs slow, never fast.
set /a LLM_AGE+=1
curl.exe -sf -m 2 -o nul "%HEALTH_URL%" >nul 2>&1
if not errorlevel 1 (
  set LLM_READY=1
  echo   AI model ready.
  exit /b 0
)
if !LLM_AGE! GEQ %LOAD_TIMEOUT% set LLM_HUNG=1
exit /b 0

:start_llm
set MMARG=
if defined MMPROJ set MMARG=--mmproj "%MMPROJ%"
set GPUARG=
if /i "%GPU%"=="disable" (
  set "GPUARG=--gpu disable"
  if defined USER_FA set "GPUARG=--gpu disable -fa %USER_FA%"
) else (
  set "GPUARG=-fa %FA_GPU%"
  if defined GPU set "GPUARG=--gpu %GPU% -fa %FA_GPU%"
  if defined NGL set "GPUARG=!GPUARG! -ngl %NGL%"
)
echo Starting AI model (%MODEL%, context %CTX%, %GPUARG%) - %REASON% ...
start "Offline Kit - AI" /min bin\llamafile.exe --server -m "%MODEL%" %MMARG% --host %BIND% --port 8080 -c %CTX% --reasoning %REASONING% %GPUARG%
set "LAST_MODEL=%MODEL%"
set LLM_UP=1
set LLM_AGE=0
set LLM_HUNG=
set LLM_READY=
if not defined HAVE_CURL set LLM_READY=unknown
exit /b 0
