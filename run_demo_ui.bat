@echo off
setlocal
set "PYTHON=.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

echo Starting MoMo Platform Interactive Demo Interface...
"%PYTHON%" scripts\demo_server.py

