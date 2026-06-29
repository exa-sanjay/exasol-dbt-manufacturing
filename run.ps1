<#
.SYNOPSIS
    Manufacturing OEE Demo — run pipeline steps on Windows.

.EXAMPLE
    .\run.ps1 demo
    .\run.ps1 up
    .\run.ps1 seed
    .\run.ps1 dbt-run
    .\run.ps1 dbt-test
    .\run.ps1 docs
    .\run.ps1 ai-setup
    .\run.ps1 ai-agent
    .\run.ps1 seed-uci
    .\run.ps1 seed-uci-live
    .\run.ps1 orchestrate
    .\run.ps1 clean
    .\run.ps1 help
#>

param([string]$Command = "help")

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ROOT = $PSScriptRoot

# ── resolve dbt ──────────────────────────────────────────────────────────────
# Set DBT_CMD env var to override (e.g. if dbt-fusion is on PATH and conflicts):
#   $env:DBT_CMD = "path\to\venv\Scripts\dbt.exe"
if ($env:DBT_CMD) {
    $DBT = $env:DBT_CMD
} elseif (Get-Command dbt -ErrorAction SilentlyContinue) {
    $DBT = (Get-Command dbt).Source
} else {
    Write-Error "dbt not found. Run: pip install dbt-core dbt-exasol"
    exit 1
}

$PYTHON = (Get-Command python -ErrorAction SilentlyContinue)?.Source ?? "python"

function Wait-Postgres {
    Write-Host "==> Waiting for PostgreSQL..."
    $max = 60; $i = 0
    while ($i -lt $max) {
        $result = docker compose exec -T postgres pg_isready -U erp_user -d manufacturing_erp 2>&1
        if ($LASTEXITCODE -eq 0) { Write-Host "    PostgreSQL ready."; return }
        Start-Sleep 2; $i++
    }
    throw "PostgreSQL did not become ready in time."
}

function Wait-Exasol {
    Write-Host "==> Waiting for Exasol (may take ~90s on first start)..."
    $max = 60; $i = 0
    while ($i -lt $max) {
        $result = docker compose exec -T exasol python3 -c `
            "import pyexasol; c=pyexasol.connect(dsn='localhost:8563',user='sys',password='exasol'); print('ready')" 2>&1
        if ($LASTEXITCODE -eq 0) { Write-Host "    Exasol ready."; return }
        Start-Sleep 5; $i++
    }
    throw "Exasol did not become ready in time."
}

function Update-ExasolConnection {
    # Exasol JVM can't use Docker DNS — refresh the hardcoded IP after every container start
    Write-Host "==> Refreshing Exasol -> PostgreSQL connection IP..."
    $pgIp = docker inspect mfg_postgres --format "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}" 2>&1
    if (-not $pgIp) { Write-Host "    Skipped (no existing connection to update)."; return }
    & $PYTHON -c @"
import pyexasol, sys
con = pyexasol.connect(dsn='localhost:8563', user='sys', password='exasol', websocket_sslopt={'cert_reqs': 0})
rows = con.execute('SELECT COUNT(*) FROM SYS.EXA_DBA_CONNECTIONS WHERE CONNECTION_NAME = ' + chr(39) + 'POSTGRES_CONN' + chr(39)).fetchval()
if int(rows) == 0:
    print('    No POSTGRES_CONN yet -- will be created on first seed.')
    sys.exit(0)
url = 'jdbc:postgresql://$($pgIp):5432/manufacturing_erp'
sql = ('ALTER CONNECTION POSTGRES_CONN TO ' + chr(39) + url + chr(39)
       + ' USER ' + chr(39) + 'erp_user' + chr(39)
       + ' IDENTIFIED BY ' + chr(39) + 'erp_password' + chr(39))
con.execute(sql)
print('    POSTGRES_CONN updated to', url)
con.close()
"@
}

switch ($Command) {

    "help" {
        Write-Host ""
        Write-Host "Exasol + dbt Manufacturing OEE Demo"
        Write-Host ""
        Write-Host "  Core pipeline:"
        Write-Host "  .\run.ps1 demo          Full end-to-end run (up + seed + dbt-run + dbt-test + docs)"
        Write-Host "  .\run.ps1 up            Start Exasol, PostgreSQL, and Ollama containers"
        Write-Host "  .\run.ps1 seed          Seed data into both databases"
        Write-Host "  .\run.ps1 dbt-run       Run all dbt models"
        Write-Host "  .\run.ps1 dbt-test      Run all dbt tests"
        Write-Host "  .\run.ps1 docs          Generate and serve dbt docs (http://localhost:8080)"
        Write-Host "  .\run.ps1 clean         Stop containers and remove volumes"
        Write-Host ""
        Write-Host "  Real sensor data (UCI AI4I 2020 Predictive Maintenance dataset):"
        Write-Host "  .\run.ps1 seed-uci      Download UCI dataset + replace synthetic IoT data"
        Write-Host "  .\run.ps1 seed-uci-live Stream one reading per machine every 5 min"
        Write-Host ""
        Write-Host "  AI layer:"
        Write-Host "  .\run.ps1 ai-setup      Create AI tables + seed failure vectors + pull Ollama model"
        Write-Host "  .\run.ps1 ai-agent      Run the Factory AI Agent"
        Write-Host ""
        Write-Host "  Orchestration:"
        Write-Host "  .\run.ps1 orchestrate   Start Dagster UI at http://localhost:3000"
        Write-Host "                          Then enable sensor 'new_sensor_data_sensor' in the UI"
        Write-Host ""
    }

    "up" {
        Write-Host "==> Starting containers..."
        docker compose up -d
        Wait-Postgres
        Wait-Exasol
        Update-ExasolConnection
        Write-Host "==> Both databases ready."
    }

    "seed" {
        Write-Host "==> Seeding Exasol schemas, Virtual Schema, and IoT data..."
        & $PYTHON "$ROOT\scripts\setup_exasol.py"
        Write-Host "==> Seed complete."
    }

    "dbt-run" {
        Write-Host "==> Running dbt models..."
        Push-Location "$ROOT\dbt_project"
        & $DBT deps
        & $DBT run
        Pop-Location
    }

    "dbt-test" {
        Write-Host "==> Running dbt tests..."
        Push-Location "$ROOT\dbt_project"
        & $DBT test
        Pop-Location
    }

    "docs" {
        Write-Host "==> Generating dbt docs..."
        Push-Location "$ROOT\dbt_project"
        & $DBT docs generate
        & $DBT docs serve --port 8081
        Pop-Location
    }

    "seed-uci" {
        Write-Host "==> Seeding IoT data from UCI AI4I 2020 dataset..."
        & $PYTHON "$ROOT\scripts\seed_iot_from_uci.py"
        Write-Host "==> Rebuilding dbt models..."
        Push-Location "$ROOT\dbt_project"
        & $DBT run
        Pop-Location
    }

    "seed-uci-live" {
        Write-Host "==> Live streaming UCI data (Ctrl+C to stop)..."
        & $PYTHON "$ROOT\scripts\seed_iot_from_uci.py" --live
    }

    "ai-setup" {
        Write-Host "==> Creating AI tables and seeding failure pattern vectors..."
        & $PYTHON "$ROOT\scripts\setup_ai_tables.py"
        Write-Host "==> Pulling Ollama model (first run only, ~4 GB)..."
        & $PYTHON "$ROOT\scripts\pull_ollama_model.py"
    }

    "ai-agent" {
        Write-Host "==> Running Self-Healing Factory AI Agent..."
        & $PYTHON "$ROOT\scripts\factory_ai_agent.py"
    }

    "orchestrate" {
        Write-Host "==> Starting Dagster UI at http://localhost:3000 ..."
        Write-Host "==> Enable sensor 'new_sensor_data_sensor' in the UI to start auto-triggering."
        Set-Location $ROOT
        dagster dev
    }

    "demo" {
        & "$PSCommandPath" up
        & "$PSCommandPath" seed
        & "$PSCommandPath" dbt-run
        & "$PSCommandPath" dbt-test
        & "$PSCommandPath" docs
    }

    "clean" {
        docker compose down -v
        Write-Host "==> Containers and volumes removed."
    }

    default {
        Write-Error "Unknown command '$Command'. Run '.\run.ps1 help' for usage."
        exit 1
    }
}
