@echo off
rem Offline Survival Kit launcher (Windows 10/11, 64-bit). Usage: start.bat   or   start.bat lan
rem Picks the biggest model in models\ that fits this PC's GPU or RAM; switch models on the home page.
rem Override: set MODEL=models\x.gguf  /  set CTX=4096  /  set GPU=disable
rem llamafile 0.10.6 GPU (Vulkan) workarounds, found on an RTX 2070 Max-Q / driver 551.61:
rem   OBS Studio's implicit Vulkan layer crashes Vulkan init (0xC0000005) - disabled for llamafile with
rem   DISABLE_VULKAN_OBS_CAPTURE=1 and VK_LOADER_LAYERS_DISABLE=~implicit~ (your own values are kept);
rem   Vulkan flash attention segfaults at higher -ngl / context - GPU runs pass "-fa off" (set FA=on to undo).
rem   Vulkan coopmat2 (NV_cooperative_matrix2) throws an uncaught C++ exception (shown as SIGSEGV) on long
rem   prompts (RTX 2070 Max-Q, driver 621.14) - on NVIDIA compute capability 7.5 (Turing: RTX 20xx, GTX 16xx)
rem   GGML_VK_DISABLE_COOPMAT2=1 is set. Set it yourself to 0 (keep coopmat2) or 1 (always off).
rem   Also: set NGL=33 (GPU layers; default: the engine decides).
rem GPU engine: if bin\llama-vulkan\llama-server.exe exists (llama.cpp b12626 Vulkan build), GPU starts (a GPU
rem   detected - GPU_MB > 0 - or GPU=vulkan) use it
rem   (newer ggml: no coopmat2 crash, ~2x faster prompt reading); flash attention defaults to auto there and
rem   the coopmat2 workaround is not applied. CPU starts (GPU=disable, fallback) and llamafile-only --gpu
rem   values (GPU=nvidia/amd/...) still use llamafile. set ENGINE=llamafile to use llamafile on the GPU too.
rem The AI's output goes to %TEMP%\offline-kit-ai.log; a crash is kept as offline-kit-ai.crash.log.
rem If the AI exits, or is not ready in time, while a GPU is in use it is restarted once on the CPU -
rem unless you set GPU yourself (e.g. GPU=vulkan). With no GPU (GPU_MB=0) a slow load is left running. The home page's "Use CPU only" toggle switches CPU only / GPU.
setlocal EnableDelayedExpansion
rem Refuse to start a second copy (checked before this window gets the launcher title): llama-server can
rem bind port 8080 next to a running one without any error, and two launchers fight over the AI.
tasklist /fi "WINDOWTITLE eq Offline Kit - launcher*" /nh 2>nul | find /i "cmd.exe" >nul && goto already_running
for %%e in (llamafile.exe llama-server.exe) do (tasklist /fi "imagename eq %%e" /nh 2>nul | find /i "%%e" >nul && goto already_running)
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
rem Your own GGML_VK_DISABLE_COOPMAT2 (1 = off, 0 = on) wins; otherwise :start_llm decides per GPU (GPU_CC).
set "USER_COOPMAT2=%GGML_VK_DISABLE_COOPMAT2%"
set GPU_CC=
set "AILOG=%TEMP%\offline-kit-ai.log"
if not defined ENGINE set ENGINE=auto
if /i not "%ENGINE%"=="auto" if /i not "%ENGINE%"=="llamafile" (echo ENGINE must be auto or llamafile & pause & exit /b 1)
set LLM_EXE=llamafile.exe
rem ENGINE_FALLBACK=1 once llama-server failed this run: GPU starts use llamafile until the home-page toggle.
set ENGINE_FALLBACK=
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
set GPU_MB=
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
if defined LLM_UP (tasklist /fi "imagename eq %LLM_EXE%" /nh 2>nul | find /i "%LLM_EXE%" >nul || goto llm_exited)
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
  set ENGINE_FALLBACK=
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
  rem PowerShell failed: use the requested model as-is (GPU memory unknown, so no automatic fallback).
  set "MODEL=%REQ_MODEL%"
  set GPU_MB=0
  set MMPROJ=
  if not defined CTX set CTX=4096
  set "REASON=chosen on the home page"
)
call :kill_llm
ping -n 3 127.0.0.1 >nul
call :start_llm
goto watch

rem llamafile exited by itself (we only kill it to switch models or stop). start "..." hides its exit
rem code, so tasklist is how we notice. With a GPU the usual cause is a Vulkan/driver failure or running
rem out of GPU memory, so retry once on the CPU, re-picking the model against RAM. Skipped if GPU was
rem set by hand (GPU=vulkan etc.). The home-page toggle can try the GPU again.
:llm_exited
set LLM_UP=
call :exit_reason
if /i "%LLM_EXE%"=="llama-server.exe" goto engine_fallback
if defined GPU goto llm_dead
rem No GPU memory seen for this start (no GPU, or PowerShell missing): not a GPU failure, no fallback.
if "%LLM_VRAM%"=="0" goto llm_dead
goto cpu_fallback

rem Why llamafile exited, from its log: FAIL_WHY (summary) and FAIL_LAST (last log line). The log is
rem copied to offline-kit-ai.crash.log so the next start (CPU fallback) does not overwrite it.
:exit_reason
set "FAIL_WHY=AI process exited"
set FAIL_LAST=
set FAIL_CODE=
if not exist "%AILOG%" exit /b 0
rem tools\run-ai.bat writes the exit code line just after the engine ends; give it a moment.
ping -n 2 127.0.0.1 >nul
copy /y "%AILOG%" "%TEMP%\offline-kit-ai.crash.log" >nul
for /f "usebackq delims=" %%l in (`powershell -NoProfile -Command "Get-Content -LiteralPath $env:AILOG | Where-Object { $_ -notmatch 'launcher\] AI process exit code' } | Select-Object -Last 1" 2^>nul`) do set "FAIL_LAST=%%l"
for /f "tokens=6" %%c in ('findstr /c:"[launcher] AI process exit code" "%AILOG%"') do set "FAIL_CODE=%%c"
findstr /c:"SIGSEGV" "%AILOG%" >nul && set "FAIL_WHY=AI crashed (SIGSEGV - usually a Vulkan/GPU driver fault)"
findstr /i /c:"out of memory" /c:"ErrorOutOfDeviceMemory" "%AILOG%" >nul && set "FAIL_WHY=AI ran out of GPU memory"
if defined FAIL_CODE set "FAIL_WHY=%FAIL_WHY%, exit code %FAIL_CODE%"
echo AI process exited: %FAIL_WHY%
if defined FAIL_LAST echo   Last log line: !FAIL_LAST!
echo   Full log: %TEMP%\offline-kit-ai.crash.log
exit /b 0

rem Alive but never answered /health within LOAD_TIMEOUT: a hang during load. Retry on the CPU only if a
rem GPU was in use for this start (GPU_MB > 0, GPU not pinned by hand). Any other slow load (CPU only,
rem no GPU) is reported once and left running; the health poll goes on so "ready" is still shown.
rem A hung llama-server (auto GPU start) moves to llamafile on the GPU instead of the CPU.
:llm_hung
set LLM_HUNG=
if defined GPU goto llm_slow
if "%LLM_VRAM%"=="0" goto llm_slow
set "FAIL_WHY=AI not ready after %LOAD_TIMEOUT% s"
set LLM_UP=
call :kill_llm
ping -n 3 127.0.0.1 >nul
if /i "%LLM_EXE%"=="llama-server.exe" goto engine_fallback
goto cpu_fallback

rem llama-server failed or hung on the GPU: try llamafile on the GPU (with its Vulkan workarounds) before
rem giving up on the GPU. Re-picked like the CPU fallback (same GPU memory, so the same model) so
rem models.json gets the new run number and says why; ENGINE_FALLBACK=1 adds that to REASON.
:engine_fallback
echo llama-server failed (%FAIL_WHY%) - trying llamafile on the GPU.
set ENGINE_FALLBACK=1
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
  set "REASON=llama-server failed, llamafile on the GPU"
)
call :start_llm
goto watch

:kill_llm
taskkill /f /im llamafile.exe >nul 2>&1
taskkill /f /im llama-server.exe >nul 2>&1
exit /b 0

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
  set GPU_MB=0
  if not defined CTX set CTX=4096
  set "REASON=GPU mode failed, running on the CPU"
)
call :start_llm
goto watch

:llm_slow
echo The AI model is still loading after %LOAD_TIMEOUT% s - leaving it running (no restart).
set LLM_WARNED=1
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
if not defined LLM_WARNED if !LLM_AGE! GEQ %LOAD_TIMEOUT% set LLM_HUNG=1
exit /b 0

:start_llm
set MMARG=
if defined MMPROJ set MMARG=--mmproj "%MMPROJ%"
rem llama-server only for real GPU starts: GPU unset and a GPU detected (GPU_MB > 0; empty = PowerShell
rem missing, unknown), or GPU=vulkan set by hand - when it is in the kit and ENGINE is not llamafile.
rem Without a detected GPU, llamafile runs as before (and a slow load is left running).
set USE_SRV=
if /i "%ENGINE%"=="auto" if not defined ENGINE_FALLBACK if exist "bin\llama-vulkan\llama-server.exe" (
  if not defined GPU if not "%GPU_MB%"=="" if not "%GPU_MB%"=="0" set USE_SRV=1
  if /i "%GPU%"=="vulkan" set USE_SRV=1
)
if defined USE_SRV goto start_srv
set LLM_EXE=llamafile.exe
set GPUARG=
if /i "%GPU%"=="disable" (
  set "GPUARG=--gpu disable"
  if defined USER_FA set "GPUARG=--gpu disable -fa %USER_FA%"
) else (
  set "GPUARG=-fa %FA_GPU%"
  if defined GPU set "GPUARG=--gpu %GPU% -fa %FA_GPU%"
  if defined NGL set "GPUARG=!GPUARG! -ngl %NGL%"
)
rem Vulkan coopmat2: your setting if you made one, else off on a Turing GPU (GPU_CC 7.5). ggml only checks
rem that the variable exists, so "0" clears it rather than passing it on.
set "GGML_VK_DISABLE_COOPMAT2=%USER_COOPMAT2%"
if not defined USER_COOPMAT2 (echo  %GPU_CC% | findstr /c:" 7.5" >nul && set GGML_VK_DISABLE_COOPMAT2=1)
if "%GGML_VK_DISABLE_COOPMAT2%"=="0" set GGML_VK_DISABLE_COOPMAT2=
set VKNOTE=
if /i not "%GPU%"=="disable" if defined GGML_VK_DISABLE_COOPMAT2 set "VKNOTE=, Vulkan coopmat2 off"
echo Starting AI model (%MODEL%, context %CTX%, %GPUARG%%VKNOTE%) - %REASON% ...
rem cmd /c keeps llamafile's output in %AILOG% (cmd strips the outer quotes of /c "...").
start "Offline Kit - AI" /min cmd /c ""tools\run-ai.bat" "bin\llamafile.exe" --server -m "%MODEL%" %MMARG% --host %BIND% --port 8080 -c %CTX% --reasoning %REASONING% %GPUARG% >"%AILOG%" 2>&1"
goto started_llm

rem llama.cpp's own server (Vulkan build). Flash attention: FA if you set it, else auto (it turns itself
rem off where the GPU lacks support). coopmat2: only your own GGML_VK_DISABLE_COOPMAT2 (0 = leave on).
:start_srv
set LLM_EXE=llama-server.exe
set "SRV_FA=%USER_FA%"
if not defined SRV_FA set SRV_FA=auto
set "GPUARG=-fa %SRV_FA%"
if defined NGL set "GPUARG=!GPUARG! -ngl %NGL%"
set "GGML_VK_DISABLE_COOPMAT2=%USER_COOPMAT2%"
if "%GGML_VK_DISABLE_COOPMAT2%"=="0" set GGML_VK_DISABLE_COOPMAT2=
set VKNOTE=
if defined GGML_VK_DISABLE_COOPMAT2 set "VKNOTE=, Vulkan coopmat2 off"
echo Starting AI model (%MODEL%, context %CTX%, llama-server Vulkan, %GPUARG%%VKNOTE%) - %REASON% ...
start "Offline Kit - AI" /min cmd /c ""tools\run-ai.bat" "bin\llama-vulkan\llama-server.exe" -m "%MODEL%" %MMARG% --host %BIND% --port 8080 -c %CTX% --reasoning %REASONING% %GPUARG% >"%AILOG%" 2>&1"

:started_llm
set "LAST_MODEL=%MODEL%"
set LLM_UP=1
set LLM_AGE=0
set LLM_HUNG=
set LLM_WARNED=
rem GPU memory pick-model.ps1 saw for this start; 0 = no GPU attempt (no GPU, GPU=disable, or unknown).
set "LLM_VRAM=%GPU_MB%"
if not defined LLM_VRAM set LLM_VRAM=0
set LLM_READY=
if not defined HAVE_CURL set LLM_READY=unknown
exit /b 0

:already_running
echo The Offline Kit or an AI engine (llamafile / llama-server) is already running.
echo If it is the kit, open http://localhost:8000/ - to restart, run stop.bat first, then start.bat again.
echo If it is another llamafile / llama-server, close it, or run "stop.bat all" (stops every one of them).
pause
exit /b 1
