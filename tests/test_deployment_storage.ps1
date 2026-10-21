param([Parameter(Mandatory)][string]$TestRoot)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($TestRoot -notmatch '^[A-Za-z]:[\\/]' -or -not (Test-Path -LiteralPath $TestRoot)) { throw 'An existing absolute private test root is required.' }
$sandbox = Join-Path ([IO.Path]::GetFullPath($TestRoot)) ('migration-' + [guid]::NewGuid().ToString('N'))
$source = Join-Path $sandbox 'source'
$target = Join-Path $sandbox 'target'
New-Item -ItemType Directory -Path (Join-Path $source 'nested') -Force | Out-Null
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot '../deploy.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'Deployment script failed to parse.' }
$function = $ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Copy-OcrDataVerified'}, $true)
if (-not $function) { throw 'Verified-copy function was not found.' }
# Load only the function; deployment, settings and scheduled tasks are never run.
Invoke-Expression $function.Extent.Text
[IO.File]::WriteAllBytes((Join-Path $source 'nested/document.pdf'), [byte[]](1,4,6,0,255))
$original = (Get-FileHash -LiteralPath (Join-Path $source 'nested/document.pdf')).Hash
$first = @(Copy-OcrDataVerified -Source $source -Destination $target)
$second = @(Copy-OcrDataVerified -Source $source -Destination $target)
if ($first.Count -ne 1 -or $second.Count -ne 1 -or $first[0].SHA256 -ne $original -or $second[0].SHA256 -ne $original) { throw 'Copy or idempotence failed.' }
if ((Get-FileHash -LiteralPath (Join-Path $source 'nested/document.pdf')).Hash -ne $original) { throw 'Original changed.' }
$collision = Join-Path $target 'nested/document.pdf'
[IO.File]::WriteAllBytes($collision, [byte[]](9,9,9))
$conflictingHash = (Get-FileHash -LiteralPath $collision).Hash
$rejected = $false
try { Copy-OcrDataVerified -Source $source -Destination $target | Out-Null } catch { $rejected = $_.Exception.Message -match 'hash verification' }
if (-not $rejected -or (Get-FileHash -LiteralPath $collision).Hash -ne $conflictingHash -or (Get-FileHash -LiteralPath (Join-Path $source 'nested/document.pdf')).Hash -ne $original) { throw 'Conflicting data was not preserved.' }
$rejected = $false
try { Copy-OcrDataVerified -Source $source -Destination (Join-Path $source 'recursive') | Out-Null } catch { $rejected = $_.Exception.Message -match 'separate destination' }
if (-not $rejected) { throw 'Recursive copy was not rejected.' }
[pscustomobject]@{Status='PASS';Cases=4;OriginalPreserved=$true;ConflictPreserved=$true;Root=$sandbox} | ConvertTo-Json
