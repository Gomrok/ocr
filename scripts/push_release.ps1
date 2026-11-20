param(
    [Parameter(Mandatory)][string]$GateBundle,
    [Parameter(Mandatory)][string]$Settings,
    [Parameter(Mandatory)][string]$Python,
    [Parameter(Mandatory)][string]$Tesseract
)

$ErrorActionPreference = 'Stop'
$source = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $source
try {
    $result = & $Python -B (Join-Path $PSScriptRoot 'release_gate.py') preflight --bundle $GateBundle --settings $Settings --tesseract $Tesseract
    if ($LASTEXITCODE -ne 0) { throw 'Release gate rejected the push.' }
    $gate = $result | ConvertFrom-Json
    $revision = (& git rev-parse HEAD).Trim()
    if ($gate.status -ne 'READY_TO_RELEASE' -or $gate.commit -ne $revision -or (& git status --porcelain)) {
        throw 'The approved tree must still be checked out and clean.'
    }
    & git push origin "${revision}:refs/heads/main"
    if ($LASTEXITCODE -ne 0) { throw 'The push did not complete.' }
    $remote = & git ls-remote origin refs/heads/main
    if ($LASTEXITCODE -ne 0 -or -not $remote -or $remote.Split()[0] -ne $revision) {
        throw 'The destination does not match the approved release.'
    }
    Write-Output $revision
} finally { Pop-Location }
