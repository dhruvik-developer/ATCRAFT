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
# The local development stack is a THIRD stack, separate from the two
# deployments, and needs its own project name: docker-compose.yml requires
# COMPOSE_PROJECT_NAME so a bare `docker compose up` cannot put two
# deployments into one project and have them silently share a database.
#
# 'atcraft-erp' is deliberately the historical name, so the volumes an
# existing checkout already has - and the demo data in them - stay put.
if (-not $env:COMPOSE_PROJECT_NAME) { $env:COMPOSE_PROJECT_NAME = 'atcraft-erp' }
$LocalProject = $env:COMPOSE_PROJECT_NAME
$DcDev   = @('compose', '-p', $LocalProject, '-f', 'docker-compose.yml', '-f', 'docker-compose.dev.yml')
# The same paths the pre-commit hook and CI lint: application code, tests,
# migrations and the repository's Python tooling.
$PyLintPaths = 'src tests migrations scripts ../../scripts'

# The two deployments are driven by scripts/deploy.sh alone, which reads
# deploy/<name>.env and passes the project name explicitly on every command.
# Duplicating that here would be a second place for the two to be confused.
# Run under Git Bash:  ./scripts/deploy.sh main up

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
        'seed-local'       = 'Migrate and populate a local database with demonstration workflows'
        'demo-data'        = 'Fill a development database with a working business'
        'db-fresh'         = 'DESTROY the dev database and rebuild it with baseline + demo data'
        'db-handover'      = 'DESTROY the dev database and rebuild it with the baseline only'
        'test'             = 'Backend unit + integration tests'
        'test-unit'        = 'Fast unit tests (no database)'
        'test-integration' = 'Integration + API tests (needs Postgres/Redis)'
        'test-cov'         = 'Backend tests with coverage'
        'test-web'         = 'Frontend unit tests'
        'build-web'        = 'Production frontend build, bundle budgets and secret scan'
        'test-e2e'         = 'Playwright end-to-end tests'
        'test-load'        = 'Locust load test'
        'lint'             = 'Lint and type-check backend + frontend'
        'format'           = 'Format supported source files'
        'typecheck'        = 'Type-check only'
        'contract'         = 'Regenerate the OpenAPI contract, the frontend types and the Postman collection'
        'contract-check'   = 'Fail if the contract, the generated types or the collection are stale'
        'postman'          = 'Regenerate the Postman collection from the committed OpenAPI contract'
        'check'            = 'contract-check + lint + test-cov + test-web + build-web'
        'hooks'            = 'Install the git pre-commit and pre-push hooks'
        'hooks-run'        = 'Run every hook against the whole tree, as a push would'
        'lock'             = 'Regenerate reviewed Python dependency locks'
        'shell-api'        = 'Shell inside the backend container'
        'shell-db'         = 'psql inside the Postgres container'
        'backup'           = 'Dump the database to .\backups'
        'deploy'           = 'Deployments are run with ./scripts/deploy.sh (see ONE-BOX-RUNBOOK.md)'
        'clean'            = 'Remove generated builds, test outputs and caches (keeps data and dependencies)'
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
        # The lock is Linux-resolved and pins uvloop, which has no Windows build;
        # the installer skips it and installs everything else hash-verified.
        Invoke-Step '.\.venv\Scripts\python.exe -m scripts.install_requirements requirements-dev.lock' $ApiDir
        Invoke-Step 'npm ci' $WebDir
        & $PSCommandPath hooks
    }

    'hooks' {
        Invoke-Step 'pre-commit install --hook-type pre-commit --hook-type pre-push'
    }
    'hooks-run' {
        Invoke-Step 'pre-commit run --all-files'
        Invoke-Step 'pre-commit run --all-files --hook-stage pre-push'
    }

    'lock' {
        $compile = '.\.venv\Scripts\pip-compile.exe --quiet --no-header --allow-unsafe --generate-hashes --strip-extras --resolver=backtracking'
        Invoke-Step "$compile --output-file=requirements.lock pyproject.toml" $ApiDir
        Invoke-Step "$compile --extra=dev --output-file=requirements-dev.lock pyproject.toml" $ApiDir
    }

    'dev' {
        Invoke-Docker ($DcDev + @('up', '-d', 'postgres', 'redis'))
        # Compose is the only thing that resolved ${POSTGRES_PORT:-5432} and
        # ${REDIS_PORT:-6379} from .env, so ask it rather than restate a number
        # the file may have overridden (the runbook recommends 5433 / 6380).
        $pg = (& docker @($DcDev + @('port', 'postgres', '5432')) | Out-String).Trim()
        $rd = (& docker @($DcDev + @('port', 'redis', '6379')) | Out-String).Trim()
        if (-not $pg) { $pg = 'see docker compose ps' }
        if (-not $rd) { $rd = 'see docker compose ps' }
        Write-Host ''
        Write-Host "Infrastructure is up (Postgres $pg, Redis $rd)." -ForegroundColor Green
        Write-Host 'Open two more terminals and run:' -ForegroundColor Yellow
        Write-Host '    .\make.ps1 dev-api'
        Write-Host '    .\make.ps1 dev-web'
        Write-Host 'Optionally a third for background jobs:' -ForegroundColor Yellow
        Write-Host '    .\make.ps1 dev-worker'
    }

    'dev-api'    { $py = Get-VenvPython; Invoke-Step "& '$py' -m uvicorn src.main:app --reload --host 0.0.0.0 --port 8000" $ApiDir }
    'dev-web'    { Invoke-Step 'npm run dev' $WebDir }
    'dev-worker' { $py = Get-VenvPython; Invoke-Step "& '$py' -m celery -A src.workers.celery_app:celery_app worker -Q default,mrp,reports --loglevel=INFO --pool=solo" $ApiDir }
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
    'seed-local' { $py = Get-VenvPython; Invoke-Step "& '$py' -m scripts.seed_local" $ApiDir }
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
        Invoke-Step "& '$py' -m ruff check $PyLintPaths" $ApiDir
        Invoke-Step "& '$py' -m ruff format --check $PyLintPaths" $ApiDir
        Invoke-Step "& '$py' -m mypy src" $ApiDir
        Invoke-Step 'npm run lint' $WebDir
        Invoke-Step 'npm run typecheck' $WebDir
        Invoke-Step 'npm run format:check' $WebDir
    }
    'format' {
        $py = Get-VenvPython
        Invoke-Step "& '$py' -m ruff format $PyLintPaths" $ApiDir
        Invoke-Step "& '$py' -m ruff check --fix $PyLintPaths" $ApiDir
        Invoke-Step 'npm run format' $WebDir
    }
    'typecheck' {
        $py = Get-VenvPython
        Invoke-Step "& '$py' -m mypy src" $ApiDir
        Invoke-Step 'npm run typecheck' $WebDir
    }
    'contract' {
        $py = Get-VenvPython
        Invoke-Step "& '$py' -m scripts.export_openapi" $ApiDir
        Invoke-Step 'npm run contract:types' $WebDir
        Invoke-Step "& '$py' -m scripts.export_postman" $ApiDir
    }
    'contract-check' {
        $py = Get-VenvPython
        Invoke-Step "& '$py' -m scripts.export_openapi --check" $ApiDir
        Invoke-Step 'npm run contract:check' $WebDir
        Invoke-Step "& '$py' -m scripts.export_postman --check" $ApiDir
    }
    'postman' { $py = Get-VenvPython; Invoke-Step "& '$py' -m scripts.export_postman" $ApiDir }
    'build-web' { Invoke-Step 'npm run build' $WebDir }
    'check' { & $PSCommandPath contract-check; & $PSCommandPath lint; & $PSCommandPath test-cov; & $PSCommandPath test-web; & $PSCommandPath build-web }

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

    # Deployments are not driven from PowerShell. scripts/deploy.sh performs
    # the cross-checks that keep the two apart - shared project name, shared
    # port, shared JWT secret, wrong branch - and reimplementing those here
    # would mean maintaining two sets of guards that must never disagree.
    { $_ -in @('prod-up', 'prod-down', 'deploy') } {
        Write-Host ''
        Write-Host 'Deployments are run through scripts/deploy.sh, under Git Bash:' -ForegroundColor Yellow
        Write-Host ''
        Write-Host '    ./scripts/deploy.sh main up        # deployment 1 (main branch)'
        Write-Host '    ./scripts/deploy.sh dev  up        # deployment 2 (dev branch)'
        Write-Host '    ./scripts/deploy.sh main migrate'
        Write-Host '    ./scripts/verify-isolation.sh      # prove they cannot see each other'
        Write-Host ''
        Write-Host 'See ONE-BOX-RUNBOOK.md for the full sequence.'
        Write-Host ''
    }

    'clean' {
        $py = Get-VenvPython
        & $py (Join-Path $Root 'scripts\clean.py') @Rest
        if ($LASTEXITCODE -ne 0) { throw "Cleanup failed (exit $LASTEXITCODE)." }
    }

    default {
        Write-Host "Unknown target '$Target'." -ForegroundColor Red
        Show-Help
        exit 1
    }
}
