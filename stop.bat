@echo off
rem Stop THIS kit: its launcher window, and the AI, library and web servers started from this kit folder.
rem Only programs whose .exe lies inside this folder are stopped (found with PowerShell by their path), so a
rem llamafile / llama-server / kiwix-serve / miniserve of your own, or another kit copy, keeps running.
rem   stop.bat all   also stops EVERY llamafile.exe, llama-server.exe, kiwix-serve.exe and miniserve.exe
rem                  (the old behaviour), e.g. a stray AI engine that keeps start.bat from starting.
setlocal
set "KIT_DIR=%~dp0"
taskkill /f /fi "WINDOWTITLE eq Offline Kit - launcher*" >nul 2>&1
if /i "%~1"=="all" goto all
powershell -NoProfile -ExecutionPolicy Bypass -Command "$k = [IO.Path]::GetFullPath($env:KIT_DIR).TrimEnd('\') + '\'; $n = 'llamafile.exe','llama-server.exe','kiwix-serve.exe','miniserve.exe'; Get-CimInstance Win32_Process | Where-Object { $n -contains $_.Name -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith($k, [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
if errorlevel 1 (
  rem No PowerShell: fall back to the kit's own window titles (start.bat names every window it opens).
  taskkill /f /t /fi "WINDOWTITLE eq Offline Kit - *" >nul 2>&1
)
echo Offline Kit stopped.
exit /b 0

:all
taskkill /f /im llamafile.exe >nul 2>&1
taskkill /f /im llama-server.exe >nul 2>&1
taskkill /f /im kiwix-serve.exe >nul 2>&1
taskkill /f /im miniserve.exe >nul 2>&1
echo Offline Kit stopped (and every llamafile, llama-server, kiwix-serve and miniserve on this PC).
exit /b 0
