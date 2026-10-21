param(
    [string]$DeploymentRoot = 'C:\Websites\ocr.gomrok.org',
    [string]$Python = 'C:\Program Files\Python311\python.exe',
    [string]$Tesseract = 'C:\Tools\GomrokOCR\Tesseract\tesseract.exe',
    [string]$StorageRoot,
    [switch]$ReuseRuntime,
    [switch]$ProvisionDomain
)

function Copy-OcrDataVerified([string]$Source, [string]$Destination) {
    $sourcePath = (Resolve-Path -LiteralPath $Source).Path.TrimEnd('\')
    $destinationPath = [IO.Path]::GetFullPath($Destination).TrimEnd('\')
    if ($sourcePath -eq $destinationPath -or $destinationPath.StartsWith($sourcePath + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Migration requires a separate destination.' }
    $entries = @((Get-Item -LiteralPath $sourcePath -Force)) + @(Get-ChildItem -LiteralPath $sourcePath -Recurse -Force)
    if (@($entries | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count) { throw 'Existing data contains a reparse point; no migration performed.' }
    for ($ancestor = $destinationPath; $ancestor; $ancestor = [IO.Path]::GetDirectoryName($ancestor)) {
        if ((Test-Path -LiteralPath $ancestor) -and ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Migration destination contains a reparse point.' }
    }
    if ((Test-Path -LiteralPath $destinationPath) -and @(Get-ChildItem -LiteralPath $destinationPath -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count) { throw 'Migration destination contains a reparse point.' }
    $manifest = @()
    $files = @($entries | Where-Object { -not $_.PSIsContainer })
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($sourcePath.Length + 1)
        $target = Join-Path $destinationPath $relative
        $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        if (-not (Test-Path -LiteralPath $target)) { Copy-Item -LiteralPath $file.FullName -Destination $target }
        if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $hash -or (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -ne $hash) { throw 'Document copy failed hash verification; original files are preserved.' }
        $manifest += @{Path=$relative;SHA256=$hash;Bytes=$file.Length}
    }
    if (@(Get-ChildItem -LiteralPath $sourcePath -File -Recurse -Force).Count -ne $files.Count) { throw 'Source documents changed during migration.' }
    return $manifest
}

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
$shared = Join-Path $root 'shared'
$proxyRoot = Join-Path $root 'iis'
$runtime = Join-Path $root 'runtime'
$settingsPath = Join-Path $shared 'settings.json'
$taskName = 'GomrokOCR'
$previousTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
$previousSettingsBytes = $null
$settings = @{}
if (Test-Path -LiteralPath $settingsPath) {
    $previousSettingsBytes = [IO.File]::ReadAllBytes($settingsPath)
    $previous = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
    foreach ($property in $previous.PSObject.Properties) { $settings[$property.Name] = $property.Value }
}
$previousData = if ($settings.ContainsKey('OCR_DATA_DIR')) { $settings.OCR_DATA_DIR } else { Join-Path $shared 'data' }
if (-not $StorageRoot) {
    $StorageRoot = if ($settings.ContainsKey('OCR_STORAGE_ROOT')) { $settings.OCR_STORAGE_ROOT } else { $shared }
}
if ($StorageRoot -notmatch '^[A-Za-z]:[\\/]' -or $StorageRoot.StartsWith('\\')) {
    throw 'StorageRoot must be an explicit local absolute private directory.'
}
$storage = [IO.Path]::GetFullPath($StorageRoot).TrimEnd('\')
foreach ($forbidden in @($sourceRoot, $proxyRoot)) {
    $forbidden = [IO.Path]::GetFullPath($forbidden).TrimEnd('\')
    if ($storage -eq $forbidden -or $storage.StartsWith($forbidden + '\', [StringComparison]::OrdinalIgnoreCase) -or $forbidden.StartsWith($storage + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Storage must be outside the checkout and IIS document root.'
    }
}
if ($storage.TrimEnd('\') -eq [IO.Path]::GetPathRoot($storage).TrimEnd('\')) { throw 'A drive root cannot be private storage.' }
for ($ancestor = $storage; $ancestor; $ancestor = [IO.Path]::GetDirectoryName($ancestor)) {
    if ((Test-Path -LiteralPath $ancestor) -and ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Storage ancestors must not be reparse points.' }
}
$dataPath = Join-Path $storage 'data'
$tempPath = Join-Path $storage 'temp'
$logPath = Join-Path $storage 'logs'
$release = Join-Path $storage "releases\$revision"
if ((Test-Path -LiteralPath $storage) -and @(Get-ChildItem -LiteralPath $storage -Recurse -Force | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count) { throw 'Private storage must not contain reparse points.' }
$oldFiles = if (Test-Path -LiteralPath $previousData) { @(Get-ChildItem -LiteralPath $previousData -File -Recurse -Force) } else { @() }
$oldBytes = if ($oldFiles) { ($oldFiles | Measure-Object Length -Sum).Sum } else { 0L }
$spaceNeeded = 1536MB + 512MB + [long]$oldBytes
if ([IO.DriveInfo]::new([IO.Path]::GetPathRoot($storage)).AvailableFreeSpace -lt $spaceNeeded) { throw 'Private storage cannot preserve the safety reserve and existing documents.' }
if ($ReuseRuntime) {
    if (-not $previousTask -or -not (Test-Path -LiteralPath (Join-Path $runtime '.venv\Scripts\python.exe'))) { throw 'No existing runtime is available to reuse.' }
    $previousRequirements = Join-Path $previousTask.Actions[0].WorkingDirectory 'requirements.txt'
    if ((Get-FileHash -LiteralPath $previousRequirements).Hash -ne (Get-FileHash -LiteralPath (Join-Path $sourceRoot 'requirements.txt')).Hash) { throw 'Dependencies changed; reusing the runtime is not allowed.' }
}
foreach ($directory in @($storage, $shared, $proxyRoot, $runtime)) {
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
# The second volume is private too; it is not an IIS virtual directory.
Set-Acl -LiteralPath $storage -AclObject $acl
if ($null -ne $previousSettingsBytes) {
    $rollbackSettings = Join-Path $storage "settings-before-$revision.json"
    if (-not (Test-Path -LiteralPath $rollbackSettings)) { [IO.File]::WriteAllBytes($rollbackSettings, $previousSettingsBytes) }
}
foreach ($directory in @($release, $dataPath, $tempPath, $logPath)) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
foreach ($directory in @($dataPath, $tempPath, $logPath)) {
    $dataAcl = Get-Acl -LiteralPath $directory
    $dataAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        [Security.Principal.SecurityIdentifier]::new('S-1-5-19'), 'Modify',
        'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    Set-Acl -LiteralPath $directory -AclObject $dataAcl
}

if (-not (Test-Path -LiteralPath (Join-Path $release 'serve.py'))) {
    $archive = Join-Path $tempPath "$revision.zip"
    & git archive --format=zip "--output=$archive" $revision
    if ($LASTEXITCODE -ne 0) { throw 'Could not archive the pushed revision.' }
    Expand-Archive -LiteralPath $archive -DestinationPath $release
}
$runtimePython = Join-Path $runtime '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $runtimePython)) {
    & $Python -m venv (Join-Path $runtime '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the production environment.' }
}
$env:TEMP = $tempPath
$env:TMP = $tempPath
$env:TMPDIR = $tempPath
$env:PYTHONDONTWRITEBYTECODE = '1'
if (-not $ReuseRuntime) {
    & $runtimePython -B -m pip install --no-cache-dir -r (Join-Path $release 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
}
$validationRoot = Join-Path (Split-Path -Parent $storage) "ocr-release-tests\$revision"
Push-Location -LiteralPath $release
try {
    # The launcher sets storage before import and guards Python spawn children.
    # Test writes must not use the service's data/temp directories.
    & $runtimePython -B (Join-Path $release 'scripts\run_isolated_tests.py') --root $validationRoot
    if ($LASTEXITCODE -ne 0) { throw 'Release tests failed.' }
} finally { Pop-Location }

if (-not $settings.ContainsKey('OCR_SECRET_KEY')) {
    $bytes = New-Object byte[] 32
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    $random.GetBytes($bytes)
    $random.Dispose()
    $settings.OCR_SECRET_KEY = [BitConverter]::ToString($bytes).Replace('-', '')
}
$settings.OCR_SECURE_COOKIES = '1'
$settings.OCR_ALLOWED_HOSTS = 'ocr.gomrok.org,127.0.0.1,localhost'
$settings.OCR_STORAGE_ROOT = $storage
$settings.OCR_DATA_DIR = $dataPath
$settings.OCR_TEMP_DIR = $tempPath
$settings.OCR_LOG_DIR = $logPath
$settings.OCR_TESSDATA_DIR = Join-Path $release 'tessdata'
$settings.OCR_TESSERACT_ONLY = '1'
$settings.TESSERACT_CMD = $Tesseract
$settings.OMP_THREAD_LIMIT = '2'
$localKey = Join-Path $sourceRoot 'api_key.txt'
if (Test-Path -LiteralPath $localKey) { $settings.GOOGLE_API_KEY = (Get-Content -LiteralPath $localKey -Raw).Trim() }
$proxyConfig = Join-Path $proxyRoot 'web.config'
$proxyBefore = if (Test-Path -LiteralPath $proxyConfig) { [IO.File]::ReadAllBytes($proxyConfig) } else { $null }
$pauseOwned = $false
$taskChanged = $false
$committed = $false
try {
if ($previousTask) {
    # Close public upload admissions before checking for idle workers. The old
    # release predates the application-level drain protocol.
    if ($null -eq $proxyBefore) { throw 'Cannot safely close public upload admissions.' }
    [xml]$pausedConfig = [Text.Encoding]::UTF8.GetString($proxyBefore).TrimStart([char]0xFEFF)
    $rules = $pausedConfig.configuration.'system.webServer'.rewrite.rules
    if ($rules.SelectSingleNode("rule[@name='OCR storage maintenance']")) { throw 'Another maintenance operation owns upload admissions.' }
    $pause = $pausedConfig.CreateElement('rule')
    $pause.SetAttribute('name', 'OCR storage maintenance')
    $pause.SetAttribute('stopProcessing', 'true')
    $match = $pausedConfig.CreateElement('match')
    $match.SetAttribute('url', '^upload/?$')
    [void]$pause.AppendChild($match)
    $action = $pausedConfig.CreateElement('action')
    $action.SetAttribute('type', 'CustomResponse')
    $action.SetAttribute('statusCode', '503')
    $action.SetAttribute('statusReason', 'Service Unavailable')
    $action.SetAttribute('statusDescription', 'OCR storage maintenance')
    [void]$pause.AppendChild($action)
    [void]$rules.PrependChild($pause)
    $webServer = $pausedConfig.configuration.'system.webServer'
    $protocol = $webServer.SelectSingleNode('httpProtocol')
    if ($null -eq $protocol) { $protocol = $pausedConfig.CreateElement('httpProtocol'); [void]$webServer.AppendChild($protocol) }
    $headers = $protocol.SelectSingleNode('customHeaders')
    if ($null -eq $headers) { $headers = $pausedConfig.CreateElement('customHeaders'); [void]$protocol.AppendChild($headers) }
    if ($headers.SelectSingleNode("add[@name='X-OCR-Admission-Pause']")) { throw 'Another operation owns the admission header.' }
    $pauseToken = [guid]::NewGuid().ToString('N')
    $header = $pausedConfig.CreateElement('add')
    $header.SetAttribute('name', 'X-OCR-Admission-Pause')
    $header.SetAttribute('value', $pauseToken)
    [void]$headers.AppendChild($header)
    $pauseOwned = $true
    $pausedConfig.Save($proxyConfig)
    Add-Type -AssemblyName System.Net.Http
    $pauseClient = [Net.Http.HttpClient]::new()
    $pauseClient.Timeout = [TimeSpan]::FromSeconds(10)
    try {
        $pauseConfirmed = $false
        for ($attempt = 0; $attempt -lt 10; $attempt++) {
            $pauseResponse = $pauseClient.PostAsync('https://ocr.gomrok.org/upload', [Net.Http.StringContent]::new('')).GetAwaiter().GetResult()
            $pauseStatus = [int]$pauseResponse.StatusCode
            $pauseConfirmed = $pauseStatus -eq 503 -and $pauseResponse.Headers.Contains('X-OCR-Admission-Pause') -and ($pauseResponse.Headers.GetValues('X-OCR-Admission-Pause') -contains $pauseToken)
            $pauseResponse.Dispose()
            if ($pauseConfirmed) { break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $pauseConfirmed) { throw "Could not verify closed upload admissions (HTTP $pauseStatus)." }
    } finally { $pauseClient.Dispose() }
    $oldHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:15101/healthz' -TimeoutSec 5
    if (-not $oldHealth.PSObject.Properties['active_jobs'] -or $oldHealth.active_jobs -ne 0) { throw 'Active OCR jobs must complete before deployment.' }
}
if ((Test-Path -LiteralPath $previousData) -and [IO.Path]::GetFullPath($previousData).TrimEnd('\') -ne $dataPath) {
    $manifest = @(Copy-OcrDataVerified -Source $previousData -Destination $dataPath)
    $manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $storage "migration-$revision.json") -Encoding UTF8
}
if ($previousTask) {
    $oldHealth = Invoke-RestMethod -Uri 'http://127.0.0.1:15101/healthz' -TimeoutSec 5
    if (-not $oldHealth.PSObject.Properties['active_jobs'] -or $oldHealth.active_jobs -ne 0) { throw 'A worker is still active; no service restart performed.' }
    Stop-ScheduledTask -TaskName $taskName
}
$taskChanged = $true
$settings | ConvertTo-Json | Set-Content -LiteralPath $settingsPath -Encoding UTF8
$arguments = "-B -X utf8 -u `"$(Join-Path $release 'serve.py')`" --settings `"$settingsPath`" --release $revision"
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
    throw 'Release health check failed.'
}

if (-not $pauseOwned) { Copy-Item -LiteralPath (Join-Path $release 'iis\web.config') -Destination (Join-Path $proxyRoot 'web.config') -Force }
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
$committed = $true
} finally {
    if (-not $committed -and $taskChanged) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($null -ne $previousSettingsBytes) { [IO.File]::WriteAllBytes($settingsPath, $previousSettingsBytes) }
        if ($previousTask) { Register-ScheduledTask -TaskName $taskName -Action $previousTask.Actions -Principal $previousTask.Principal -Settings $previousTask.Settings -Trigger $previousTask.Triggers -Force | Out-Null; Start-ScheduledTask -TaskName $taskName }
    }
    if ($pauseOwned -and $null -ne $proxyBefore) {
        if ($committed) { Copy-Item -LiteralPath (Join-Path $release 'iis\web.config') -Destination $proxyConfig -Force }
        else { [IO.File]::WriteAllBytes($proxyConfig, $proxyBefore) }
    }
}
