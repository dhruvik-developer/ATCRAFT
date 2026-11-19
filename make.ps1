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
# The local development stack is separate from the production deployment
# and needs its own project name: docker-compose.yml requires
# COMPOSE_PROJECT_NAME so a bare `docker compose up` cannot put two stacks
# into one project and have them silently share a database.
#
# 'atcraft-erp' is deliberately the historical name, so the volumes an
# existing checkout already has - and the demo data in them - stay put.
if (-not $env:COMPOSE_PROJECT_NAME) { $env:COMPOSE_PROJECT_NAME = 'atcraft-erp' }
$LocalProject = $env:COMPOSE_PROJECT_NAME
$DcDev   = @('compose', '-p', $LocalProject, '-f', 'docker-compose.yml', '-f', 'docker-compose.dev.yml')
# The same paths the pre-commit hook and CI lint: application code, tests,
# migrations, the repository's Python tooling and the load harness.
$PyLintPaths = 'src tests migrations scripts ../../scripts ../../tests/load'

# The hosted deployment (production, from main) is driven by scripts/deploy.sh
# alone, which reads deploy/main.env and passes the project name explicitly on
# every command. Duplicating that here would be a second set of guards.
# Run under Git Bash:  ./scripts/deploy.sh main up

function Invoke-Step {
    param([string]$Command, [string]$WorkDir = $Root)
    Write-Host "==> $Command" -ForegroundColor Cyan
    Push-Location $WorkDir
    try {
        # npm writes ordinary progress to stderr. PowerShell 5.1 promotes that to
        # NativeCommandError and can abort even when the process exited 0.
        # Start-Process keeps the real exit code without poisoning the session.
        $executable = 'powershell.exe'
        $argumentList = @('-NoProfile', '-Command', $Command)
        if ($Command -match '^\s*npm\b') {
            $executable = 'cmd.exe'
            $argumentList = @('/c', $Command)
        }
        $proc = Start-Process -FilePath $executable -ArgumentList $argumentList `
            -WorkingDirectory (Get-Location).Path -Wait -PassThru -NoNewWindow
        if ($proc.ExitCode -ne 0) {
            throw "Step failed (exit $($proc.ExitCode)): $Command"
        }
    } finally { Pop-Location }
}

# Not `$Args`: a parameter by that name is shadowed by PowerShell's automatic
# $args (the unbound arguments, empty here), so every target ran a bare
# `docker`, which prints its usage and exits 0 - `dev`, `up` and `down` all
# "succeeded" without touching a container.
function Invoke-Docker { param([string[]]$DockerArgs) Invoke-Step "docker $($DockerArgs -join ' ')" }

function Get-VenvPython {
    $candidate = Join-Path $ApiDir '.venv\Scripts\python.exe'
    if (Test-Path $candidate) { return $candidate }
    return 'python'
}

# The database's owner and name, read from the running postgres container rather
# than restated here: Compose built that container from .env, so a checkout that
# renamed either still dumps and opens the database it actually has.
function Get-LocalDatabase {
    $values = foreach ($name in @('POSTGRES_USER', 'POSTGRES_DB')) {
        $value = (& docker @($DcDev + @('exec', '-T', 'postgres', 'printenv', $name)) | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $value) {
            throw "Could not read $name from the postgres container. Is the local stack up (.\make.ps1 dev)?"
        }
        $value
    }
    return $values
}

# A custom-format archive starts with the five bytes 'PGDMP'. A dump that went
# through a Windows PowerShell 5.1 redirect starts FF FE 50 00 instead (UTF-16),
# and pg_restore cannot read it - so never trust a backup file unchecked.
function Assert-PgDumpArchive {
    param([string]$Path)
    $head = New-Object byte[] 5
    $stream = [System.IO.File]::OpenRead($Path)
    try { $read = $stream.Read($head, 0, $head.Length) } finally { $stream.Dispose() }
    if ($read -ne $head.Length -or [System.Text.Encoding]::ASCII.GetString($head) -ne 'PGDMP') {
        throw "$Path is not a pg_dump custom-format archive (it does not start with PGDMP); pg_restore cannot read it."
    }
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
        'test-unit'        = 'Fast unit tests (no database), including the load harness''s own'
        'test-integration' = 'Integration + API tests (needs Postgres/Redis)'
        'test-cov'         = 'Backend tests with coverage'
        'test-web'         = 'Frontend unit tests'
        'build-web'        = 'Production frontend build, bundle budgets and secret scan'
        'test-e2e'         = 'Playwright end-to-end tests'
        'test-load'        = 'Locust smoke profile against LOAD_BASE_URL with LOAD_ACCOUNTS_FILE'
        'lint'             = 'Lint and type-check backend + frontend'
        'format'           = 'Format supported source files'
        'typecheck'        = 'Type-check only'
        'contract'         = 'Regenerate the OpenAPI contract, the frontend types and the Postman collection'
        'contract-check'   = 'Fail if the contract, the generated types or the collection are stale'
        'postman'          = 'Regenerate the Postman collection from the committed OpenAPI contract'
        'check'            = 'contract-check + lint + test-cov + test-web + build-web'
        'hooks'            = 'Install the git pre-commit and pre-push hooks'
        'hooks-run'        = 'Run every hook against the whole tree, as a push would'
        'lock'             = 'Regenerate the Linux-resolved Python dependency locks (in Docker)'
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
        # The locks are Linux-resolved. pip-compile evaluates environment markers
        # for the platform it runs on, so on Windows it drops uvloop, and CI -
        # which re-resolves both locks on Linux and diffs them - rejects the
        # result. Resolve inside the backend Dockerfile's dependency stage
        # instead: Linux, CI's Python and pip-tools from requirements-dev.lock.
        # It gets its own tag, so the application image the stack runs is untouched.
        $image = 'atcraft/api-lock:local'
        Invoke-Step "docker build --target development-builder --tag $image ." $ApiDir
        $compile = "docker run --rm --volume ${ApiDir}:/lock --workdir /lock $image /opt/venv/bin/pip-compile --quiet --no-header --allow-unsafe --generate-hashes --strip-extras --resolver=backtracking"
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

    'test'             { & $PSCommandPath test-unit; & $PSCommandPath test-integration }
    'test-unit' {
        $py = Get-VenvPython
        Invoke-Step "& '$py' -m pytest tests/unit -q" $ApiDir
        Invoke-Step "& '$py' -m pytest ../../tests/load -q -p no:cacheprovider" $ApiDir
    }
    'test-integration' { $py = Get-VenvPython; Invoke-Step "& '$py' -m pytest tests/integration tests/api -q" $ApiDir }
    'test-cov'         { $py = Get-VenvPython; Invoke-Step "& '$py' -m pytest --cov=src --cov-report=term-missing --cov-report=html" $ApiDir }
    'test-web'         { Invoke-Step 'npm run test' $WebDir }
    'test-e2e'         { Invoke-Step 'npm test' (Join-Path $Root 'tests\e2e') }
    'test-load' {
        # A load run needs one dedicated account per virtual user and an isolated,
        # populated target - never the development stack, whose database is the
        # shared one. docs/performance.md has the setup.
        if (-not $env:LOAD_ACCOUNTS_FILE) { throw 'Set LOAD_ACCOUNTS_FILE to a JSON array of dedicated load accounts (docs/performance.md).' }
        if (-not $env:LOAD_BASE_URL) { throw 'Set LOAD_BASE_URL to an isolated load environment, never the development stack (docs/performance.md).' }
        if (-not (Get-Command locust -ErrorAction SilentlyContinue)) { throw 'locust is not on PATH; install the harness: pip install -r tests/load/requirements.txt' }
        Invoke-Step "locust -f locustfile.py --headless -u 50 -r 5 -t 2m --host $env:LOAD_BASE_URL" (Join-Path $Root 'tests\load')
    }

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
    'shell-db'  {
        $user, $db = Get-LocalDatabase
        Invoke-Docker ($DcDev + @('exec', 'postgres', 'psql', '-U', $user, '-d', $db))
    }

    'backup' {
        $dir = Join-Path $Root 'backups'
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory $dir | Out-Null }
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $name = "atcraft-$stamp.dump"
        $out = Join-Path $dir $name
        # Relative to $Root, where every docker step runs: Invoke-Docker joins its
        # arguments with spaces, so an absolute path could split on one.
        $partialName = "backups\$name.partial"
        $partial = Join-Path $Root $partialName
        $user, $db = Get-LocalDatabase
        # pg_dump writes the archive inside the container and `compose cp` copies
        # its bytes out untouched. Never redirect pg_dump's output here: the '>'
        # would run in Windows PowerShell 5.1, which decodes a native command's
        # output as text and writes it back as UTF-16, so the file cannot be
        # restored. tests/unit/test_make_wrappers.py refuses such a redirect.
        $archive = "/tmp/$name"
        try {
            Invoke-Docker ($DcDev + @('exec', '-T', 'postgres', 'pg_dump', '-U', $user, '-d', $db, '-Fc', '-f', $archive))
            # The same proof scripts/deploy.sh asks of a deployment backup.
            & docker @($DcDev + @('exec', '-T', 'postgres', 'pg_restore', '--list', $archive)) | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'pg_dump did not produce an archive pg_restore can read; no backup was written.' }
            Invoke-Docker ($DcDev + @('cp', "postgres:$archive", $partialName))
            Assert-PgDumpArchive $partial
            Move-Item -LiteralPath $partial -Destination $out
        } finally {
            Invoke-Docker ($DcDev + @('exec', '-T', 'postgres', 'rm', '-f', $archive))
            if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
        }
        Write-Host "Wrote $out" -ForegroundColor Green
        Write-Host "Reminder: copy this dump to EXTERNAL storage. A backup is not reliable until a restore is tested (section 46)." -ForegroundColor Yellow
    }

    # Deployments are not driven from PowerShell. scripts/deploy.sh performs
    # the guards that protect production - project name, branch, clean tree,
    # secrets - and reimplementing those here
    # would mean maintaining two sets of guards that must never disagree.
    { $_ -in @('prod-up', 'prod-down', 'deploy') } {
        Write-Host ''
        Write-Host 'Deployments are run through scripts/deploy.sh, under Git Bash:' -ForegroundColor Yellow
        Write-Host ''
        Write-Host '    ./scripts/deploy.sh main up        # production (main branch)'
        Write-Host '    ./scripts/deploy.sh main migrate'
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
