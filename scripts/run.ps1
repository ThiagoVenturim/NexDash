<#
.SYNOPSIS
    Cross-platform execution script for NexDash on Windows (PowerShell).

.DESCRIPTION
    Runs NexDash locally, in Docker Compose, or executes test suites.

.PARAMETER Mode
    Execution mode: 'local' (default), 'docker', or 'test'.

.PARAMETER Port
    Port number to listen on (default: 8000).

.PARAMETER DbUrl
    Optional database connection URL (e.g. postgresql+psycopg://... or sqlite:///...).

.EXAMPLE
    .\scripts\run.ps1
    .\scripts\run.ps1 -Mode docker
    .\scripts\run.ps1 -Mode test
#>

[CmdletBinding()]
param (
    [ValidateSet("local", "docker", "test")]
    [string]$Mode = "local",

    [int]$Port = 8000,

    [string]$DbUrl = "",

    [switch]$Docker
)

$ErrorActionPreference = "Stop"

if ($Docker) {
    $Mode = "docker"
}

if ($DbUrl -ne "") {
    $env:DB_URL = $DbUrl
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

# Detect python executable
$pythonExe = "python"
if (Test-Path "$repoRoot\.venv\Scripts\python.exe") {
    $pythonExe = "$repoRoot\.venv\Scripts\python.exe"
}

switch ($Mode) {
    "docker" {
        Write-Host "[INFO] Starting NexDash with Docker Compose..."
        docker compose up --build
    }
    "test" {
        Write-Host "[INFO] Running test suite..."
        & $pythonExe -m pytest
    }
    "local" {
        Write-Host "[INFO] Starting NexDash locally on port $Port..."
        & $pythonExe -m nexdash --port $Port
    }
}
