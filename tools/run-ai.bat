@echo off
rem Runs the AI engine for start.bat (which sends this script's output to the AI log) and records the
rem engine's exit code there, so a crash or silent exit leaves a reason behind.
%*
echo [launcher] AI process exit code %errorlevel%
