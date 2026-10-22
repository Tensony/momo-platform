@echo off
setlocal

set "PYTHON=.venv\Scripts\python.exe"
if not exist "%PYTHON%" set "PYTHON=python"

set "PYTEST=.venv\Scripts\pytest.exe"
if not exist "%PYTEST%" set "PYTEST=pytest"

set "RUFF=.venv\Scripts\ruff.exe"
if not exist "%RUFF%" set "RUFF=ruff"

if "%TEST_DATABASE_URL%"=="" set "TEST_DATABASE_URL=postgresql://postgres:Tenson@127.0.0.1:5432/momo_test"
set "PYTHONPATH=src"

set "TARGET=%~1"
if "%TARGET%"=="" set "TARGET=test"

if /I "%TARGET%"=="test" (
    echo Running unit + integration tests...
    "%PYTEST%" -v
    goto :eof
)

if /I "%TARGET%"=="cover" (
    echo Running tests with coverage...
    "%PYTEST%" -v --cov=src/momo --cov-report=term-missing --cov-report=xml
    goto :eof
)

if /I "%TARGET%"=="proofs" (
    echo Running correctness proofs...
    "%PYTHON%" scripts\proofs\run_proofs.py --local
    goto :eof
)

if /I "%TARGET%"=="lint" (
    echo Running linter...
    "%RUFF%" check src tests scripts
    goto :eof
)

if /I "%TARGET%"=="install" (
    echo Installing dev dependencies...
    "%PYTHON%" -m pip install -r requirements-dev.txt
    goto :eof
)

if /I "%TARGET%"=="build" (
    echo Building Lambda artifacts...
    "%PYTHON%" scripts\build_lambdas.py
    goto :eof
)

echo Unknown target: %TARGET%
echo Available targets: test, cover, proofs, lint, install, build
exit /b 1
