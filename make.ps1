<#
.SYNOPSIS
    Cross-platform Windows PowerShell helper equivalent to Makefile.
.EXAMPLE
    .\make.ps1 test
    .\make.ps1 cover
    .\make.ps1 proofs
    .\make.ps1 lint
#>

param(
    [Parameter(Position=0)]
    [string]$Target = "test",
    [string]$Url = "",
    [string]$Token = "",
    [string]$Email = ""
)

$ErrorActionPreference = "Stop"

# Detect Python venv
$Python = if (Test-Path ".\.venv\Scripts\python.exe") { ".\.venv\Scripts\python.exe" } else { "python" }
$Pytest = if (Test-Path ".\.venv\Scripts\pytest.exe") { ".\.venv\Scripts\pytest.exe" } else { "pytest" }
$Ruff   = if (Test-Path ".\.venv\Scripts\ruff.exe")   { ".\.venv\Scripts\ruff.exe" }   else { "ruff" }

# Local PostgreSQL credentials
if (-not $env:TEST_DATABASE_URL) {
    $env:TEST_DATABASE_URL = "postgresql://postgres:Tenson@127.0.0.1:5432/momo_test"
}
$env:PYTHONPATH = "src"

switch ($Target.ToLower()) {
    "test" {
        Write-Host "Running unit + integration tests..." -ForegroundColor Cyan
        & $Pytest -v
    }
    "cover" {
        Write-Host "Running tests with coverage gate..." -ForegroundColor Cyan
        & $Pytest -v --cov=src/momo --cov-report=term-missing --cov-report=xml
    }
    "proofs" {
        Write-Host "Running correctness proofs locally..." -ForegroundColor Cyan
        & $Python scripts/proofs/run_proofs.py --local
    }
    "lint" {
        Write-Host "Running ruff linter..." -ForegroundColor Cyan
        & $Ruff check src tests scripts
    }
    "install" {
        Write-Host "Installing dev dependencies..." -ForegroundColor Cyan
        & $Python -m pip install -r requirements-dev.txt
    }
    "build" {
        Write-Host "Building Lambda packages via Python builder..." -ForegroundColor Cyan
        & $Python scripts/build_lambdas.py
    }
    "scan" {
        Write-Host "Running gitleaks scan..." -ForegroundColor Cyan
        gitleaks detect --config .gitleaks.toml --redact --verbose
    }
    default {
        Write-Host "Unknown target: $Target" -ForegroundColor Red
        Write-Host "Available targets: test, cover, proofs, lint, install, build, scan" -ForegroundColor Yellow
        exit 1
    }
}
