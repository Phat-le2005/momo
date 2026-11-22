<#
.SYNOPSIS
  FinResolve AI dev tasks for Windows PowerShell (no make required).
.EXAMPLE
  .\scripts\dev.ps1 setup     # create venv, install backend + frontend deps
  .\scripts\dev.ps1 api       # run FastAPI on :8000 (reload)
  .\scripts\dev.ps1 web       # run Next.js on :3000
  .\scripts\dev.ps1 check     # ruff + mypy + pytest + eslint + tsc + next build
  .\scripts\dev.ps1 up        # docker compose up --build
#>
param(
  [Parameter(Position = 0)]
  [ValidateSet("setup", "api", "web", "migrate", "test", "lint", "typecheck", "check", "openapi", "up", "down", "compose-config")]
  [string]$Task = "check"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Api = Join-Path $Root "services/api"
$Web = Join-Path $Root "apps/web"
$Py = Join-Path $Api ".venv/Scripts/python.exe"
$Compose = @("compose", "-f", (Join-Path $Root "infra/docker-compose.yml"))

function Invoke-Step([string]$Dir, [scriptblock]$Block) {
  Push-Location $Dir
  try {
    & $Block
    if ($LASTEXITCODE -ne 0) { throw "Step failed with exit code $LASTEXITCODE" }
  } finally { Pop-Location }
}

switch ($Task) {
  "setup" {
    Invoke-Step $Api { python -m venv .venv }
    Invoke-Step $Api { & $Py -m pip install --upgrade pip }
    Invoke-Step $Api { & $Py -m pip install -e ".[dev]" }
    Invoke-Step $Web { npm ci }
  }
  "api" { Invoke-Step $Api { & $Py -m uvicorn app.main:app --reload --port 8000 } }
  "web" { Invoke-Step $Web { npm run dev } }
  "migrate" { Invoke-Step $Api { & $Py -m alembic upgrade head } }
  "test" { Invoke-Step $Api { & $Py -m pytest } }
  "lint" {
    Invoke-Step $Api { & $Py -m ruff check . }
    Invoke-Step $Api { & $Py -m ruff format --check . }
    Invoke-Step $Web { npm run lint }
  }
  "typecheck" {
    Invoke-Step $Api { & $Py -m mypy }
    Invoke-Step $Web { npm run typecheck }
  }
  "check" {
    & $PSCommandPath lint
    & $PSCommandPath typecheck
    & $PSCommandPath test
    Invoke-Step $Web { npm run build }
  }
  "openapi" { Invoke-Step $Api { & $Py -m scripts.export_openapi } }
  "up" { & docker @Compose up --build }
  "down" { & docker @Compose down }
  "compose-config" { & docker @Compose config }
}
