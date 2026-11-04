param(
    [int]$Port = 8768,
    [ValidateSet('blocks', 'freeze')][string]$Project = 'blocks'
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    throw 'Run .\Setup.ps1 first.'
}
if (-not (Test-Path -LiteralPath 'frontend/dist/index.html')) {
    throw 'Frontend build is missing. Run .\Setup.ps1 first.'
}
$env:SPARK_PROJECT = $Project
Write-Host "SPARK $Project : http://127.0.0.1:$Port . Ctrl+C stops the server." -ForegroundColor Green
& ./.venv/Scripts/python.exe -m uvicorn spark_freeze.app:app --host 127.0.0.1 --port $Port
