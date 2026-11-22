#!/usr/bin/env python3
"""Static checks for start.bat (it cannot run on the Linux CI runner). Usage: python3 tests/test_start_bat.py

The behaviour itself is tested in tests/test_cpu_fallback.sh against start.sh; this keeps start.bat in
step with it:

* CRLF line endings (cmd.exe misparses labels/blocks in LF batch files).
* Every `goto X` / `call :X` has a matching `:X` label.
* llamafile gets the OBS Vulkan-hook workaround (DISABLE_VULKAN_OBS_CAPTURE=1 and
  VK_LOADER_LAYERS_DISABLE=~implicit~, keeping values the user set) before the first llamafile start.
* GPU starts pass `-fa %FA_GPU%` with FA defaulting to off; CPU starts pass `--gpu disable`.
  The superseded `-fit` workaround is gone.
* CPU fallback only after a real GPU attempt: GPU_MB (GPU memory pick-model.ps1 saw) > 0 and GPU not
  set by hand. Otherwise an exit is reported and a slow load is reported once and left running.
* The watch loop notices a dead llamafile with tasklist (start "..." hides the exit code) and polls
  /health with curl.exe; the CPU fallback prints one "fell back to the CPU" line and is skipped
  when GPU was set by hand.
* Switch requests (model + optional cpu=on|off) are validated by pick-model.ps1 -CheckOnly; the
  toggle restarts with GPU=disable or the user's own GPU value.
* GPU engine: llama.cpp's Vulkan llama-server (bin\\llama-vulkan) when present and GPU is unset or
  vulkan; if it exits or hangs, llamafile on the GPU, then the CPU. Both run through tools\\run-ai.bat,
  which logs the exit code. A second start is refused. Vulkan coopmat2 is turned off on Turing
  (GPU_CC 7.5) for llamafile only, unless the user set GGML_VK_DISABLE_COOPMAT2.
"""
import re
import sys
from pathlib import Path

BAT = Path(__file__).resolve().parent.parent / "start.bat"
raw = BAT.read_bytes()
text = raw.decode("ascii")
lines = text.split("\r\n")
fails = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def first(pattern):
    """Index of the first line matching pattern (case-insensitive), or -1."""
    rx = re.compile(pattern, re.I)
    return next((i for i, l in enumerate(lines) if rx.search(l)), -1)


code = [l for l in lines if not re.match(r"\s*rem\b", l, re.I)]
body = "\r\n".join(code)

check("CRLF line endings only", b"\n" not in raw.replace(b"\r\n", b""))

labels = {m.group(1).lower() for l in lines for m in [re.match(r"^:(\w+)", l)] if m}
targets = {t.lower() for t in re.findall(r"\bgoto\s+:?(\w+)", body, re.I) + re.findall(r"\bcall\s+:(\w+)", body, re.I)}
check("every goto/call target has a label (missing: %s)" % sorted(targets - labels), targets <= labels)

first_start = first(r'start "Offline Kit - AI"')
check("llamafile is started", first_start > 0)
for var, val in (("DISABLE_VULKAN_OBS_CAPTURE", "1"), ("VK_LOADER_LAYERS_DISABLE", "~implicit~")):
    i = first(r'^if not defined %s set "?%s=%s"?$' % (var, var, re.escape(val)))
    check("%s=%s set (unless the user set it) before llamafile starts" % (var, val), 0 <= i < first_start)
    call_start = first(r"^call :start_llm")
    check("...and before the first call :start_llm", 0 <= i < call_start)

check("FA defaults to off for GPU runs", first(r"^if not defined FA_GPU set FA_GPU=off$") >= 0)
check("FA validated (on/off/auto)", first(r'FA must be on, off or auto') >= 0)
check("GPU starts pass -fa %FA_GPU%", re.search(r'set "GPUARG=-fa %FA_GPU%"', body) is not None)
check("CPU starts pass --gpu disable", re.search(r'set "GPUARG=--gpu disable"', body) is not None)
check("llamafile command line carries %GPUARG%", re.search(r'start "Offline Kit - AI" .*bin\\llamafile\.exe"? .*%GPUARG%', body) is not None)
check("no -fit workaround left", "-fit" not in body.lower())

check("exit watch uses tasklist for the running engine (%LLM_EXE%)",
      re.search(r'tasklist /fi "imagename eq %LLM_EXE%".*goto llm_exited', body, re.I) is not None)
check("LLM_EXE names llamafile.exe or llama-server.exe per start",
      body.count("set LLM_EXE=llamafile.exe") >= 2 and "set LLM_EXE=llama-server.exe" in body)
check("health poll with curl.exe on /health", re.search(r'curl\.exe -sf -m 2 -o nul "%HEALTH_URL%"', body) is not None)
check("health URL defaults to 127.0.0.1:8080/health",
      first(r"^if not defined HEALTH_URL set HEALTH_URL=http://127\.0\.0\.1:8080/health$") >= 0)
check("timeout leads to llm_hung (once)", "if not defined LLM_WARNED if !LLM_AGE! GEQ %LOAD_TIMEOUT% set LLM_HUNG=1" in body
      and "if defined LLM_HUNG goto llm_hung" in body)
check("one clear fallback line", body.count("fell back to the CPU") == 1)
seg = text[text.index(":llm_exited"):text.index(":llm_hung")]
check("no automatic fallback when GPU was set by hand (exit)", "if defined GPU goto llm_dead" in seg)
check("no fallback after an exit with no GPU in use", '\r\nif "%LLM_VRAM%"=="0" goto llm_dead\r\n' in seg)
check("exit fallback only after both checks", seg.index('"%LLM_VRAM%"=="0"') < seg.index("goto cpu_fallback"))
seg = text[text.index(":llm_hung"):text.index(":cpu_fallback")]
check("no automatic fallback when GPU was set by hand (hang)", "\r\nif defined GPU goto llm_slow\r\n" in seg)
check("slow load with no GPU in use is not restarted", '\r\nif "%LLM_VRAM%"=="0" goto llm_slow\r\n' in seg
      and seg.index("goto llm_slow") < seg.index("taskkill"))
seg = text[text.index(":llm_slow"):text.index(":llm_dead")]
check("llm_slow reports once and keeps llamafile running",
      "set LLM_WARNED=1" in seg and "goto watch" in seg and "taskkill" not in seg and "start_llm" not in seg)
seg = text[text.index(":start_llm"):]
check("each start records the GPU memory seen (default 0)",
      'set "LLM_VRAM=%GPU_MB%"' in seg and "if not defined LLM_VRAM set LLM_VRAM=0" in seg and "set LLM_WARNED=" in seg)
seg = text[text.index(":cpu_fallback"):text.index(":llm_dead")]
check("fallback sets GPU=disable and GPU_FALLBACK=1, re-picks", all(s in seg for s in ("set GPU=disable", "set GPU_FALLBACK=1", "%PICK%", "call :start_llm")))

check("switch requests validated with pick-model.ps1 -CheckOnly", re.search(r"-RequestFile \"%CTL%\\request\.txt\" -CheckOnly", body) is not None)
check("invalid request ignored", "if not defined REQ_MODEL (echo Ignored an invalid model-switch request." in body)
check("cpu=on -> GPU=disable", re.search(r'if /i "%REQ_CPU%"=="on" \(\r\n(?:.*\r\n)*?\s+set GPU=disable\r\n', body) is not None)
check("cpu=off -> the user's GPU value (or auto)", re.search(r'if /i "%REQ_CPU%"=="off" \(\r\n(?:.*\r\n)*?\s+set "GPU=%USER_GPU%"', body) is not None)
check("control server still 127.0.0.1 only", re.search(r'miniserve\.exe "%CTL%" -i 127\.0\.0\.1 -p 8079', body) is not None)

def L(name):
    """Index of the label line :name (not a goto/call to it)."""
    return text.index("\r\n:" + name + "\r\n")


# --- GPU engine: llama.cpp's Vulkan llama-server, llamafile as GPU fallback, then the CPU ---
seg = text[L("start_llm"):L("started_llm")]
check("llama-server only with ENGINE=auto, no engine fallback yet, and the build present",
      'if /i "%ENGINE%"=="auto" if not defined ENGINE_FALLBACK if exist "bin\\llama-vulkan\\llama-server.exe" (' in seg)
check("llama-server only with GPU unset or vulkan (llamafile-only --gpu values keep llamafile)",
      "if not defined GPU set USE_SRV=1" in seg and 'if /i "%GPU%"=="vulkan" set USE_SRV=1' in seg)
check("ENGINE validated (auto/llamafile)", first(r"ENGINE must be auto or llamafile") >= 0)
check("llama-server flash attention: FA if set, else auto",
      'set "SRV_FA=%USER_FA%"' in seg and "if not defined SRV_FA set SRV_FA=auto" in seg and 'set "GPUARG=-fa %SRV_FA%"' in seg)
check("llama-server command line carries model, host, port, context, reasoning, %GPUARG%",
      re.search(r'bin\\llama-vulkan\\llama-server\.exe" -m "%MODEL%" %MMARG% --host %BIND% --port 8080 -c %CTX% '
                r'--reasoning %REASONING% %GPUARG%', seg) is not None)
check("both engines run through tools\\run-ai.bat into %AILOG%",
      len(re.findall(r'cmd /c ""tools\\run-ai\.bat" "bin\\.*>"%AILOG%" 2>&1"', body)) == 2)
seg = text[L("llm_exited"):L("exit_reason")]
check("llama-server exit -> engine_fallback before the GPU/CPU checks",
      seg.index('if /i "%LLM_EXE%"=="llama-server.exe" goto engine_fallback') < seg.index("if defined GPU goto llm_dead"))
seg = text[L("llm_hung"):L("cpu_fallback")]
check("llama-server hang -> kill and engine_fallback", seg.index('if /i "%LLM_EXE%"=="llama-server.exe" (') < seg.index("goto engine_fallback")
      < seg.index("if defined GPU goto llm_slow") and "call :kill_llm" in seg)
seg = text[L("engine_fallback"):L("kill_llm")]
check("engine_fallback sets ENGINE_FALLBACK=1 and restarts (same model, no re-pick)",
      "set ENGINE_FALLBACK=1" in seg and "call :start_llm" in seg and "%PICK%" not in seg)
check("cpu=off toggle clears ENGINE_FALLBACK (tries llama-server again)",
      re.search(r'if /i "%REQ_CPU%"=="off" \(\r\n(?:.*\r\n)*?\s+set ENGINE_FALLBACK=\r\n', body) is not None)
seg = text[L("kill_llm"):]
check("kill_llm stops both engines", "taskkill /f /im llamafile.exe" in seg and "taskkill /f /im llama-server.exe" in seg)
check("no direct llamafile-only taskkill outside kill_llm", body.count("taskkill /f /im llamafile.exe") == 1)

# --- second start refused (llama-server can share port 8080 with a running copy) ---
title = first(r"^title Offline Kit - launcher")
check("running launcher detected before this window takes the title",
      0 < first(r'tasklist /fi "WINDOWTITLE eq Offline Kit - launcher\*".*goto already_running') < title)
check("running engine detected before the title",
      0 < first(r"for %%e in \(llamafile\.exe llama-server\.exe\) do .*goto already_running") < title)
check("already_running explains and exits", ":already_running" in text and "run stop.bat first" in text)

# --- Vulkan coopmat2: off on Turing (GPU_CC 7.5) for llamafile only; the user's value wins ---
check("user's GGML_VK_DISABLE_COOPMAT2 captured before any start",
      0 < first(r'^set "USER_COOPMAT2=%GGML_VK_DISABLE_COOPMAT2%"$') < first(r"^call :start_llm"))
seg = text[L("start_llm"):L("start_srv")]
check("llamafile path: coopmat2 off on compute capability 7.5 unless the user set it",
      'if not defined USER_COOPMAT2 (echo  %GPU_CC% | findstr /c:" 7.5" >nul && set GGML_VK_DISABLE_COOPMAT2=1)' in seg)
seg2 = text[L("start_srv"):L("started_llm")]
check("llama-server path: only the user's coopmat2 value", "7.5" not in seg2 and 'set "GGML_VK_DISABLE_COOPMAT2=%USER_COOPMAT2%"' in seg2)
check("GGML_VK_DISABLE_COOPMAT2=0 clears the variable (ggml only checks it exists)",
      body.count('if "%GGML_VK_DISABLE_COOPMAT2%"=="0" set GGML_VK_DISABLE_COOPMAT2=') == 2)

# --- exit reason kept ---
seg = text[L("exit_reason"):L("llm_hung")]
check("exit reason: crash log copy, last line, exit code, SIGSEGV / out of memory",
      all(s in seg for s in ('copy /y "%AILOG%" "%TEMP%\\offline-kit-ai.crash.log"', "FAIL_LAST", "[launcher] AI process exit code",
                             "SIGSEGV", "ErrorOutOfDeviceMemory", "exit code %FAIL_CODE%")))

RUN_AI = BAT.parent / "tools" / "run-ai.bat"
ra = RUN_AI.read_bytes() if RUN_AI.exists() else b""
check("tools/run-ai.bat exists with CRLF endings", bool(ra) and b"\n" not in ra.replace(b"\r\n", b""))
check("run-ai.bat runs its arguments and logs the exit code",
      b"\r\n%*\r\n" in ra and b"echo [launcher] AI process exit code %errorlevel%" in ra)
STOP = (BAT.parent / "stop.bat").read_bytes()
check("stop.bat stops llama-server.exe too", b"taskkill /f /im llama-server.exe" in STOP)

sys.exit(1 if fails else 0)
