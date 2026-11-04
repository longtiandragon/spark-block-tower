param([switch]$DemoOnly, [switch]$LegacyFreeze)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Assert-Exit([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed (exit $LASTEXITCODE)." }
}

if (Get-Command uv -ErrorAction SilentlyContinue) {
    if ($LegacyFreeze -and -not $DemoOnly) { & uv sync --extra vision } else { & uv sync }
    Assert-Exit 'Python dependencies'
} else {
    if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
        & python -m venv .venv
        Assert-Exit 'Create virtual environment'
    }
    $Package = if ($LegacyFreeze -and -not $DemoOnly) { '.[vision]' } else { '.' }
    & ./.venv/Scripts/python.exe -m pip install -e $Package
    Assert-Exit 'Python dependencies'
}

if ($LegacyFreeze -and -not $DemoOnly) {
    & ./.venv/Scripts/python.exe scripts/prepare_model.py
    Assert-Exit 'Download pose model'
}

Push-Location frontend
try {
    & npm.cmd ci
    Assert-Exit 'Frontend dependencies'
    & npm.cmd run build
    Assert-Exit 'Frontend build'
} finally { Pop-Location }

Write-Host 'Ready. Run .\Start.ps1 and open http://127.0.0.1:8768' -ForegroundColor Green
