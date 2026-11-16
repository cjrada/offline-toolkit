@echo off
taskkill /f /im llamafile.exe >nul 2>&1
taskkill /f /im kiwix-serve.exe >nul 2>&1
taskkill /f /im miniserve.exe >nul 2>&1
echo Offline Kit stopped.
