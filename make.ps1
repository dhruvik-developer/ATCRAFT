<#
.SYNOPSIS
    Atcraft Manufacturing ERP - Windows equivalent of the Makefile targets.

.DESCRIPTION
    GNU make is not normally present on Windows. This wrapper exposes the same
    command vocabulary required by master rules section 49A:

        .\make.ps1 dev            .\make.ps1 migrate      .\make.ps1 lint
        .\make.ps1 dev-docker     .\make.ps1 test         .\make.ps1 format
        .\make.ps1 up / down / logs

.EXAMPLE
    .\make.ps1 dev
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Target = 'help',

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = 'Stop'
$Root    = $PSScriptRoot
$ApiDir  = Join-Path $Root 'apps\api'
$WebDir  = Join-Path $Root 'apps\web'
$DcDev   = @('compose', '-f', 'docker-compose.yml', '-f', 'docker-compose.dev.yml')
$DcProd  = @('compose', '-f', 'docker-compose.yml', '-f', 'docker-compose.prod.yml')

function Invoke-Step {
    param([string]$Command, [string]$WorkDir = $Root)
    Write-Host "==> $Command" -ForegroundColor Cyan
    Push-Location $WorkDir
    try {
        & powershell -NoProfile -Command $Command
        if ($LASTEXITCODE -ne 0) { throw "Step failed (exit $LASTEXITCODE): $Command" }
    } finally { Pop-Location }
}

function Invoke-Docker { param([string[]]$Args) Invoke-Step "docker $($Args -join ' ')" }

function Get-VenvPython {
    $candidate = Join-Path $ApiDir '.venv\Scripts\python.exe'
    if (Test-Path $candidate) { return $candidate }
    return 'python'
}

function Show-Help {
    Write-Host ''
    Write-Host 'Atcraft Manufacturing ERP - available targets' -ForegroundColor Green
    Write-Host ''
    $targets = [ordered]@{
        'install'          = 'Install backend (.venv) and frontend dependencies'
        'dev'              = 'Hybrid: Postgres+Redis in Docker, API and web local with hot reload'
        'dev-api'          = 'Run FastAPI locally (expects infra up)'
        'dev-web'          = 'Run the Vite dev server locally'
        'dev-worker'       = 'Run a Celery worker locally'
        'dev-docker'       = 'Full Docker development environment'
        'up'               = 'Start the Docker Compose stack detached'
        'down'             = 'Stop the stack (volumes preserved)'
        'restart'          = 'Restart the stack'
        'logs'             = 'Tail service logs'
        'ps'               = 'Show service status'
        'migrate'          = 'Apply Alembic migrations to head'
        'migrate-down'     = 'Roll back one Alembic revision'
        'migration'        = 'Autogenerate a migration:  .\make.ps1 migration "add xyz"'
        'seed'             = 'Load baseline reference data'
        'demo-data'        = 'Fill a development database with a working business'
        'db-fresh'         = 'DESTROY the dev database and rebuild it with baseline + demo data'
        'db-handover'      = 'DESTROY the dev database and rebuild it with the baseline only'
        'test'             = 'Backend unit + integration tests'
        'test-unit'        = 'Fast unit tests (no database)'
        'test-integration' = 'Integration + API tests (needs Postgres/Redis)'
        'test-cov'         = 'Backend tests with coverage'
        'test-web'         = 'Frontend unit tests'
        'test-e2e'         = 'Playwright end-to-end tests'
        'test-load'        = 'Locust load test'
        'lint'             = 'Lint and type-check backend + frontend'
        'format'           = 'Format supported source files'
        'typecheck'        = 'Type-check only'
        'check'            = 'lint + test (what CI runs)'
        'shell-api'        = 'Shell inside the backend container'
        'shell-db'         = 'psql inside the Postgres container'
        'backup'           = 'Dump the database to .\backups'
        'prod-up'          = 'Start the production stack'
        'clean'            = 'Remove build artefacts and caches'
    }
    foreach ($k in $targets.Keys) {
        Write-Host ('  {0,-18} {1}' -f $k, $targets[$k])
    }
    Write-Host ''
}

switch ($Target) {
    'help' { Show-Help }

    'install' {
        Invoke-Step 'python -m venv .venv' $ApiDir
        Invoke-Step '.\.venv\Scripts\python.exe -m pip install --upgrade pip' $ApiDir
        Invoke-Step '.\.venv\Scripts\python.exe -m pip install -e ".[dev]"' $ApiDir
        Invoke-Step 'npm ci' $WebDir
    }

    'dev' {
        Invoke-Docker ($DcDev + @('up', '-d', 'postgres', 'redis'))
        Write-Host ''
        Write-Host 'Infrastructure is up (Postgres :5432, Redis :6379).' -ForegroundColor Green
        Write-Host 'Open two more terminals and run:' -ForegroundColor Yellow
        Write-Host '    .\make.ps1 dev-api'
        Write-Host '    .\make.ps1 dev-web'
        Write-Host 'Optionally a third for background jobs:' -ForegroundColor Yellow
        Write-Host '    .\make.ps1 dev-worker'
    }

    'dev-api'    { $py = Get-VenvPython; Invoke-Step "& '$py' -m uvicorn src.main:app --reload --host 0.0.0.0 --port 8000" $ApiDir }
    'dev-web'    { Invoke-Step 'npm run dev' $WebDir }
    'dev-worker' { $py = Get-VenvPython; Invoke-Step "& '$py' -m celery -A src.workers.celery_app:celery_app worker --loglevel=INFO --pool=solo" $ApiDir }
    'dev-docker' { Invoke-Docker ($DcDev + @('up', '--build')) }

    'up'      { Invoke-Docker ($DcDev + @('up', '-d')) }
    'down'    { Invoke-Docker ($DcDev + @('down')) }
    'restart' { Invoke-Docker ($DcDev + @('down')); Invoke-Docker ($DcDev + @('up', '-d')) }
    'logs'    { Invoke-Docker ($DcDev + @('logs', '-f', '--tail=100', 'backend', 'celery-worker', 'frontend', 'nginx')) }
    'ps'      { Invoke-Docker ($DcDev + @('ps')) }

    'migrate'      { $py = Get-VenvPython; Invoke-Step "& '$py' -m alembic upgrade head" $ApiDir }
    'migrate-down' { $py = Get-VenvPython; Invoke-Step "& '$py' -m alembic downgrade -1" $ApiDir }
    'migration' {
        if (-not $Rest) { throw 'Usage: .\make.ps1 migration "message"' }
        $msg = $Rest -join ' '
        $py = Get-VenvPython
        Invoke-Step "& '$py' -m alembic revision --autogenerate -m `"$msg`"" $ApiDir
    }
    'seed' { $py = Get-VenvPython; Invoke-Step "& '$py' -m src.core.seed" $ApiDir }
    'demo-data' { $py = Get-VenvPython; Invoke-Step "& '$py' -m src.core.demo_data" $ApiDir }
    'db-fresh' { $py = Get-VenvPython; Invoke-Step "& '$py' -m scripts.reset_database --yes --demo" $ApiDir }
    'db-handover' { $py = Get-VenvPython; Invoke-Step "& '$py' -m scripts.reset_database --yes --seed" $ApiDir }

    'test'             { $py = Get-VenvPython; Invoke-Step "& '$py' -m pytest tests/unit -q" $ApiDir; Invoke-Step "& '$py' -m pytest tests/integration tests/api -q" $ApiDir }
    'test-unit'        { $py = Get-VenvPython; Invoke-Step "& '$py' -m pytest tests/unit -q" $ApiDir }
    'test-integration' { $py = Get-VenvPython; Invoke-Step "& '$py' -m pytest tests/integration tests/api -q" $ApiDir }
    'test-cov'         { $py = Get-VenvPython; Invoke-Step "& '$py' -m pytest --cov=src --cov-report=term-missing --cov-report=html" $ApiDir }
    'test-web'         { Invoke-Step 'npm run test' $WebDir }
    'test-e2e'         { Invoke-Step 'npx playwright test' (Join-Path $Root 'tests\e2e') }
    'test-load'        { Invoke-Step 'locust -f locustfile.py --headless -u 50 -r 5 -t 2m --host http://localhost' (Join-Path $Root 'tests\load') }

    'lint' {
        $py = Get-VenvPython
        Invoke-Step "& '$py' -m ruff check src tests" $ApiDir
        Invoke-Step "& '$py' -m mypy src" $ApiDir
        Invoke-Step 'npm run lint' $WebDir
        Invoke-Step 'npm run typecheck' $WebDir
    }
    'format' {
        $py = Get-VenvPython
        Invoke-Step "& '$py' -m ruff format src tests" $ApiDir
        Invoke-Step "& '$py' -m ruff check --fix src tests" $ApiDir
        Invoke-Step 'npm run format' $WebDir
    }
    'typecheck' {
        $py = Get-VenvPython
        Invoke-Step "& '$py' -m mypy src" $ApiDir
        Invoke-Step 'npm run typecheck' $WebDir
    }
    'check' { & $PSCommandPath lint; & $PSCommandPath test }

    'shell-api' { Invoke-Docker ($DcDev + @('exec', 'backend', 'bash')) }
    'shell-db'  { Invoke-Docker ($DcDev + @('exec', 'postgres', 'psql', '-U', 'atcraft', '-d', 'atcraft_erp')) }

    'backup' {
        $dir = Join-Path $Root 'backups'
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory $dir | Out-Null }
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $out = Join-Path $dir "atcraft-$stamp.dump"
        Invoke-Step "docker $($DcDev -join ' ') exec -T postgres pg_dump -U atcraft -Fc atcraft_erp > `"$out`""
        Write-Host "Reminder: copy this dump to EXTERNAL storage. A backup is not reliable until a restore is tested (section 46)." -ForegroundColor Yellow
    }

    'prod-up'   { Invoke-Docker ($DcProd + @('up', '-d', '--build')) }
    'prod-down' { Invoke-Docker ($DcProd + @('down')) }

    'clean' {
        Get-ChildItem -Path $Root -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        foreach ($p in '.pytest_cache', '.ruff_cache', '.mypy_cache', 'htmlcov') {
            $full = Join-Path $ApiDir $p
            if (Test-Path $full) { Remove-Item $full -Recurse -Force }
        }
        foreach ($p in 'dist', '.vite') {
            $full = Join-Path $WebDir $p
            if (Test-Path $full) { Remove-Item $full -Recurse -Force }
        }
        Write-Host 'Cleaned.' -ForegroundColor Green
    }

    default {
        Write-Host "Unknown target '$Target'." -ForegroundColor Red
        Show-Help
        exit 1
    }
}
