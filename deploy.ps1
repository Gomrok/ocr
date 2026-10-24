param(
    [string]$DeploymentRoot = 'C:\Websites\ocr.gomrok.org',
    [string]$Python = 'C:\Program Files\Python311\python.exe',
    [string]$Tesseract = 'C:\Tools\GomrokOCR\Tesseract\tesseract.exe',
    [string]$AccuracyReport,
    [string]$PaddlePython,
    [string]$PaddleModels,
    [switch]$ProvisionDomain
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$sourceRoot = $PSScriptRoot
Set-Location -LiteralPath $sourceRoot
$revision = (& git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'The source must be a Git checkout.' }
if (& git status --porcelain --untracked-files=normal) { throw 'Commit and push all changes before deployment.' }
$remoteHead = (& git ls-remote origin refs/heads/main)
if ($LASTEXITCODE -ne 0 -or -not $remoteHead -or $remoteHead.Split()[0] -ne $revision) {
    throw 'origin/main does not match HEAD. Push this revision before deployment.'
}
if (-not (Test-Path -LiteralPath $Tesseract -PathType Leaf)) { throw 'Install native Tesseract before deployment.' }
$root = [IO.Path]::GetFullPath($DeploymentRoot).TrimEnd('\')
if ($root -ne 'C:\Websites\ocr.gomrok.org') { throw 'Deployment target is outside the approved OCR application directory.' }
if (-not $AccuracyReport -or -not $PaddlePython -or -not $PaddleModels) {
    throw 'Provide passing automatic accuracy evidence and pre-provisioned private Paddle runtime/model paths.'
}
$privateOcrRoot = Join-Path $root 'local-ocr'
foreach ($artifact in @($PaddlePython, $PaddleModels)) {
    $resolved = (Resolve-Path -LiteralPath $artifact).Path
    if (-not $resolved.StartsWith($privateOcrRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Paddle artifacts must be immutable versioned paths under the private local-ocr directory.'
    }
}
$preflightPython = Join-Path $root 'runtime\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $preflightPython)) { throw 'Provision the main runtime before this accuracy upgrade.' }
& $preflightPython (Join-Path $sourceRoot 'tools\release_gate.py') --accuracy-report $AccuracyReport --python $PaddlePython --models $PaddleModels --storage $root
if ($LASTEXITCODE -ne 0) { throw 'Release quality, offline model or disk-space gate failed. Production has not been changed.' }
$drainPath = Join-Path $root 'shared\data\deployment-drain'
$drainOwned = $false
try {
$previousTask = Get-ScheduledTask -TaskName 'GomrokOCR' -ErrorAction SilentlyContinue
if ($previousTask) {
    try { $oldHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:15101/healthz' -TimeoutSec 5 }
    catch { throw 'Could not establish whether production has active jobs; deployment is blocked.' }
    if (-not $oldHealth.PSObject.Properties['drain_supported'] -or -not $oldHealth.drain_supported) {
        throw 'The running release cannot confirm closed upload admissions. Automatic deployment is blocked; a controlled maintenance handoff is required.'
    }
    # CreateNew must not replace an operator's existing maintenance marker.
    $drainFile = [IO.File]::Open($drainPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    $drainOwned = $true
    $drainFile.Dispose()
    $oldHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:15101/healthz' -TimeoutSec 5
    if (-not $oldHealth.draining) { throw 'Upload admissions were not closed; deployment is blocked.' }
    if ($oldHealth.active_jobs -gt 0) { throw 'Active OCR jobs must complete before deployment.' }
}
$release = Join-Path $root "releases\$revision"
$shared = Join-Path $root 'shared'
$proxyRoot = Join-Path $root 'iis'
$runtime = Join-Path $root 'runtime'
$settingsPath = Join-Path $shared 'settings.json'
$taskName = 'GomrokOCR'
foreach ($directory in @($release, $shared, $proxyRoot, $runtime, (Join-Path $shared 'data'), (Join-Path $shared 'logs'))) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}

# The service and IIS can read code but cannot create or replace application files.
$rootAcl = [Security.AccessControl.DirectorySecurity]::new()
$rootAcl.SetAccessRuleProtection($true, $false)
foreach ($rule in @(@('S-1-5-32-544', 'FullControl'), @('S-1-5-18', 'FullControl'),
                    @('S-1-5-19', 'ReadAndExecute'), @('S-1-5-17', 'ReadAndExecute'),
                    @('S-1-5-32-568', 'ReadAndExecute'))) {
    $rootAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        [Security.Principal.SecurityIdentifier]::new($rule[0]), $rule[1],
        'ContainerInherit,ObjectInherit', 'None', 'Allow'))
}
Set-Acl -LiteralPath $root -AclObject $rootAcl

# Private configuration is outside both the checkout and the IIS document root.
$acl = [Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in @(@('S-1-5-32-544', 'FullControl'), @('S-1-5-18', 'FullControl'), @('S-1-5-19', 'ReadAndExecute'))) {
    $identity = [Security.Principal.SecurityIdentifier]::new($rule[0])
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $identity, $rule[1], 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
}
Set-Acl -LiteralPath $shared -AclObject $acl
foreach ($directory in @((Join-Path $shared 'data'), (Join-Path $shared 'logs'))) {
    $dataAcl = Get-Acl -LiteralPath $directory
    $dataAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        [Security.Principal.SecurityIdentifier]::new('S-1-5-19'), 'Modify',
        'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    Set-Acl -LiteralPath $directory -AclObject $dataAcl
}

if (-not (Test-Path -LiteralPath (Join-Path $release 'serve.py'))) {
    $archive = Join-Path $runtime "$revision.zip"
    & git archive --format=zip "--output=$archive" $revision
    if ($LASTEXITCODE -ne 0) { throw 'Could not archive the pushed revision.' }
    Expand-Archive -LiteralPath $archive -DestinationPath $release
}
$runtimePython = Join-Path $runtime '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtimePython)) {
    & $Python -m venv (Join-Path $runtime '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the production environment.' }
}
& $runtimePython -m pip install --no-cache-dir -r (Join-Path $release 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
Push-Location -LiteralPath $release
try {
    & $runtimePython -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Release tests failed.' }
} finally { Pop-Location }

$settings = @{}
$previousSettingsBytes = $null
if (Test-Path -LiteralPath $settingsPath) {
    $previousSettingsBytes = [IO.File]::ReadAllBytes($settingsPath)
    $previous = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
    foreach ($property in $previous.PSObject.Properties) { $settings[$property.Name] = $property.Value }
}
if (-not $settings.ContainsKey('OCR_SECRET_KEY')) {
    $bytes = New-Object byte[] 32
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    $random.GetBytes($bytes)
    $random.Dispose()
    $settings.OCR_SECRET_KEY = [BitConverter]::ToString($bytes).Replace('-', '')
}
$settings.OCR_SECURE_COOKIES = '1'
$settings.OCR_ALLOWED_HOSTS = 'ocr.gomrok.org,127.0.0.1,localhost'
$settings.OCR_DATA_DIR = Join-Path $shared 'data'
$settings.OCR_TESSDATA_DIR = Join-Path $release 'tessdata'
$settings.OCR_TESSERACT_ONLY = '1'
$settings.OCR_LOCAL_ONLY = '1'
$settings.OCR_ACCURACY_MODE = 'maximum_local'
$settings.OCR_PADDLE_PYTHON = $PaddlePython
$settings.OCR_PADDLE_MODELS = $PaddleModels
$settings.TESSERACT_CMD = $Tesseract
$settings.OMP_THREAD_LIMIT = '2'
if ($previousTask) {
    try { $oldHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:15101/healthz' -TimeoutSec 5 }
    catch { throw 'Could not safely establish idle state; deployment is blocked.' }
    if (-not $oldHealth.draining) { throw 'Upload admissions reopened; deployment is blocked.' }
    if ($oldHealth.active_jobs -gt 0) { throw 'Active OCR jobs must complete before deployment.' }
    Stop-ScheduledTask -TaskName $taskName
}
$settings | ConvertTo-Json | Set-Content -LiteralPath $settingsPath -Encoding UTF8
$arguments = "-X utf8 -u `"$(Join-Path $release 'serve.py')`" --settings `"$settingsPath`" --release $revision"
$action = New-ScheduledTaskAction -Execute $runtimePython -Argument $arguments -WorkingDirectory $release
$principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\LOCAL SERVICE' -LogonType ServiceAccount
$taskSettings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $taskSettings -Trigger (New-ScheduledTaskTrigger -AtStartup) -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
$healthy = $false
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:15101/healthz' -TimeoutSec 3
        if ($health.status -eq 'ok' -and $health.release -eq $revision) { $healthy = $true; break }
    } catch { Start-Sleep -Seconds 1 }
}
if (-not $healthy) {
    Stop-ScheduledTask -TaskName $taskName
    if ($null -ne $previousSettingsBytes) { [IO.File]::WriteAllBytes($settingsPath, $previousSettingsBytes) }
    if ($previousTask) { Register-ScheduledTask -TaskName $taskName -Action $previousTask.Actions -Principal $previousTask.Principal -Settings $previousTask.Settings -Trigger $previousTask.Triggers -Force | Out-Null; Start-ScheduledTask -TaskName $taskName }
    throw 'Release health check failed; the previous task was restored when available.'
}

Copy-Item -LiteralPath (Join-Path $release 'iis\web.config') -Destination (Join-Path $proxyRoot 'web.config') -Force
New-Item -ItemType Directory -Path (Join-Path $proxyRoot '.well-known\acme-challenge') -Force | Out-Null
Add-Type -Path "$env:windir\System32\inetsrv\Microsoft.Web.Administration.dll"
$manager = [Microsoft.Web.Administration.ServerManager]::new()
try {
    if (-not $manager.ApplicationPools['GomrokOCR']) { $manager.ApplicationPools.Add('GomrokOCR') | Out-Null }
    $manager.ApplicationPools['GomrokOCR'].ManagedRuntimeVersion = ''
    $site = $manager.Sites['ocr.gomrok.org']
    if (-not $site) { $site = $manager.Sites.Add('ocr.gomrok.org', 'http', '*:80:ocr.gomrok.org', $proxyRoot) }
    $site.Applications['/'].VirtualDirectories['/'].PhysicalPath = $proxyRoot
    $site.Applications['/'].ApplicationPoolName = 'GomrokOCR'
    $config = $manager.GetApplicationHostConfiguration()
    if (-not $config.GetSection('system.webServer/proxy').GetAttributeValue('enabled')) { throw 'IIS reverse proxy support must be enabled.' }
    $variables = $config.GetSection('system.webServer/rewrite/allowedServerVariables', 'ocr.gomrok.org').GetCollection()
    foreach ($name in @('HTTP_X_FORWARDED_PROTO', 'HTTP_X_FORWARDED_HOST')) {
        if (-not @($variables | Where-Object { $_.GetAttributeValue('name') -eq $name }).Count) {
            $element = $variables.CreateElement('add'); $element.SetAttributeValue('name', $name); $variables.Add($element)
        }
    }
    $siteId = $site.Id
    $manager.CommitChanges()
} finally { $manager.Dispose() }

if ($ProvisionDomain) {
    Import-Module DnsServer
    $records = @(Get-DnsServerResourceRecord -ZoneName 'gomrok.org' -Name 'ocr' -RRType A -ErrorAction SilentlyContinue)
    if ($records.Count -eq 0) {
        Add-DnsServerResourceRecordA -ZoneName 'gomrok.org' -Name 'ocr' -IPv4Address '185.252.86.16' -TimeToLive (New-TimeSpan -Minutes 5) | Out-Null
    } elseif (@($records | Where-Object { $_.RecordData.IPv4Address.IPAddressToString -ne '185.252.86.16' }).Count) { throw 'The subdomain already points to another server.' }
    $certificate = Get-ChildItem 'Cert:\LocalMachine\WebHosting' | Where-Object { $_.DnsNameList.Unicode -contains 'ocr.gomrok.org' -and $_.NotAfter -gt (Get-Date).AddDays(7) } | Select-Object -First 1
    if (-not $certificate) {
        & 'C:\Program Files\win-acme\wacs.exe' --source manual --host ocr.gomrok.org --validation filesystem --webroot $proxyRoot --store certificatestore --certificatestore WebHosting --installation iis --installationsiteid $siteId --friendlyname ocr.gomrok.org --accepttos --notaskscheduler
        if ($LASTEXITCODE -ne 0) { throw 'TLS certificate provisioning failed.' }
    }
}
$certificate = Get-ChildItem 'Cert:\LocalMachine\WebHosting' | Where-Object { $_.DnsNameList.Unicode -contains 'ocr.gomrok.org' -and $_.NotAfter -gt (Get-Date) } | Sort-Object NotAfter -Descending | Select-Object -First 1
if ($certificate) {
    $manager = [Microsoft.Web.Administration.ServerManager]::new()
    try {
        $site = $manager.Sites['ocr.gomrok.org']
        $binding = @($site.Bindings | Where-Object { $_.Protocol -eq 'https' -and $_.Host -eq 'ocr.gomrok.org' }) | Select-Object -First 1
        if (-not $binding) {
            $binding = $site.Bindings.Add('*:443:ocr.gomrok.org', $certificate.GetCertHash(), 'WebHosting')
            $binding.SslFlags = 1
        }
        $manager.CommitChanges()
    } finally { $manager.Dispose() }
}
[pscustomobject]@{Release=$revision;PublicUrl='https://ocr.gomrok.org';Health=$healthy;ReleasePath=$release}
} finally {
    if ($drainOwned) { Remove-Item -LiteralPath $drainPath -ErrorAction Stop }
}
