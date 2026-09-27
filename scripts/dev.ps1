$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$overlay = Join-Path $repoRoot '.harness'
$toolDirectory = Join-Path $overlay 'tools'
$uv = Join-Path $toolDirectory 'uv.exe'

$env:HARNESS_DEV_ROOT = $repoRoot
$env:XDG_STATE_HOME = Join-Path $overlay 'state'
$env:XDG_RUNTIME_DIR = Join-Path $overlay 'runtime'
$env:HARNESS_SKILL_REGISTRY = Join-Path $overlay 'skills'
$env:HARNESS_DEV_SKILL_PROFILES = 'codex,cursor'
$env:UV_CACHE_DIR = Join-Path $overlay 'uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $overlay 'python'
$env:TEMP = Join-Path $overlay 'tmp'
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null

function Show-Help {
    @'
Run this Harness checkout with isolated Windows runtime paths.

Usage:
  scripts\dev.cmd help
  scripts\dev.cmd env
  scripts\dev.cmd sync
  scripts\dev.cmd stop
  scripts\dev.cmd connect-codex [Git workspace root]
  scripts\dev.cmd quality
  scripts\dev.cmd <command> [args...]

The wrapper uses uv 0.12.5 and Python 3.13 from this checkout. It keeps
the database, IPC endpoint, skills, and uv cache under .harness. It does not
install or uninstall Harness from the user's global host configuration.
'@ | Write-Output
}

function Ensure-Uv {
    if (Test-Path -LiteralPath $uv -PathType Leaf) {
        $version = & $uv --version
        if ($LASTEXITCODE -eq 0 -and $version -match '^uv 0\.12\.5(?:\s|$)') {
            return
        }
    }
    New-Item -ItemType Directory -Force -Path $toolDirectory | Out-Null
    $installer = Join-Path $toolDirectory 'uv-install.ps1'
    Invoke-WebRequest -Uri 'https://astral.sh/uv/0.12.5/install.ps1' -OutFile $installer
    $env:UV_INSTALL_DIR = $toolDirectory
    $env:UV_NO_MODIFY_PATH = '1'
    & $installer
    if ($LASTEXITCODE -ne 0) { throw "uv installer exited with $LASTEXITCODE" }
    $version = & $uv --version
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^uv 0\.12\.5(?:\s|$)') {
        throw 'uv 0.12.5 was not installed successfully'
    }
}

function Sync-Project {
    & $uv python install --no-bin --no-registry 3.13
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $uv sync --locked --all-groups
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

function Secure-Overlay {
    $python = Join-Path $repoRoot '.venv\Scripts\python.exe'
    $code = @'
from pathlib import Path
import sys
from harness.windows_fs import ensure_private_windows_directory, secure_owned_windows_path

overlay = Path(sys.argv[1])
secure_owned_windows_path(overlay, directory=True)
skills = overlay / 'skills'
if skills.exists():
    secure_owned_windows_path(skills, directory=True)
else:
    ensure_private_windows_directory(skills)
'@
    & $python -c $code $overlay
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

function Stop-IsolatedDaemon {
    $python = Join-Path $repoRoot '.venv\Scripts\python.exe'
    $code = @'
from harness.ipc import IpcTransportError, request_shutdown
from harness.runtime_paths import default_runtime_paths

try:
    request_shutdown(default_runtime_paths().socket)
except IpcTransportError:
    print('No isolated Harness daemon is reachable.')
else:
    print('Stopped isolated Harness daemon.')
'@
    & $python -c $code
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

if ($args.Count -eq 0) {
    Show-Help
    exit 1
}

switch ($args[0]) {
    { $_ -in @('help', '-h', '--help') } { Show-Help; exit 0 }
    'env' {
        @(
            "HARNESS_DEV_ROOT=$env:HARNESS_DEV_ROOT"
            "XDG_STATE_HOME=$env:XDG_STATE_HOME"
            "XDG_RUNTIME_DIR=$env:XDG_RUNTIME_DIR"
            "HARNESS_SKILL_REGISTRY=$env:HARNESS_SKILL_REGISTRY"
            "UV_CACHE_DIR=$env:UV_CACHE_DIR"
            "UV_PYTHON_INSTALL_DIR=$env:UV_PYTHON_INSTALL_DIR"
        ) | Write-Output
        exit 0
    }
}

Ensure-Uv
Set-Location -LiteralPath $repoRoot
if ($args[0] -eq 'sync') {
    Sync-Project
    Secure-Overlay
    exit 0
}
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot '.venv\Scripts\python.exe'))) {
    Sync-Project
}
Secure-Overlay
if ($args[0] -eq 'stop') {
    Stop-IsolatedDaemon
    exit 0
}
if ($args[0] -eq 'connect-codex') {
    $workspace = if ($args.Count -gt 1) { $args[1] } else { $repoRoot }
    & $uv run --frozen python scripts/connect_codex_windows.py $workspace
    exit $LASTEXITCODE
}
if ($args[0] -eq 'quality') {
    & $uv run --frozen python scripts/quality.py
} else {
    & $uv run --frozen @args
}
exit $LASTEXITCODE
