@echo off
rem Stop the kit: the launcher window, the AI, the library and both web servers.
taskkill /f /fi "WINDOWTITLE eq Offline Kit - launcher*" >nul 2>&1
taskkill /f /im llamafile.exe >nul 2>&1
taskkill /f /im llama-server.exe >nul 2>&1
taskkill /f /im kiwix-serve.exe >nul 2>&1
taskkill /f /im miniserve.exe >nul 2>&1
echo Offline Kit stopped.
