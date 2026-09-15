param([Alias('Config')][string]$ExistingConfig, [switch]$List)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
Write-Host 'Experiment Manager - use new worker software with your existing configuration'
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { throw 'Install WSL2 with Ubuntu before using this worker package.' }
$inventory = & wsl.exe --list --quiet 2>$null
if ($LASTEXITCODE -ne 0) { throw 'WSL is not ready. Open the Ubuntu used by the existing worker first.' }
$distributions = @($inventory | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ -and $_ -notmatch '^docker-desktop' })
if ($distributions.Count -eq 0) { throw 'No Ubuntu/WSL distribution found. Use the same normal Linux user as the existing worker.' }
$settingsPath = Join-Path $env:LOCALAPPDATA 'ExperimentManager\worker-launcher\distribution.txt'
$distribution = if (Test-Path -LiteralPath $settingsPath) { (Get-Content -LiteralPath $settingsPath -Raw).Trim() } else { '' }
if ($distribution -notin $distributions) {
    $distribution = @($distributions | Where-Object { $_ -match '^Ubuntu' } | Select-Object -First 1)[0]
    if (-not $distribution) { $distribution = $distributions[0] }
    if ($distributions.Count -gt 1) {
        for ($index=0; $index -lt $distributions.Count; $index++) { Write-Host "[$index] $($distributions[$index])" }
        $choice = Read-Host "Choose the original worker distribution (Enter uses $distribution)"
        if ($choice) {
            $number = 0
            if (-not [int]::TryParse($choice,[ref]$number) -or $number -lt 0 -or $number -ge $distributions.Count) { throw 'Invalid distribution number.' }
            $distribution = $distributions[$number]
        }
    }
}
$packagePath = & wsl.exe -d $distribution --exec wslpath -a $PSScriptRoot
if ($LASTEXITCODE -ne 0) { throw 'Extract the complete worker ZIP to a local disk, then retry.' }
$workerArguments = @()
if ($ExistingConfig) {
    if ($ExistingConfig -match '^[A-Za-z]:[\\/]') {
        $ExistingConfig = & wsl.exe -d $distribution --exec wslpath -a $ExistingConfig
        if ($LASTEXITCODE -ne 0) { throw 'Could not translate the existing configuration path.' }
        $ExistingConfig = $ExistingConfig.Trim()
    }
    $workerArguments += @('--config', $ExistingConfig)
}
if ($List) { $workerArguments += '--list' }
Write-Host "Using $distribution. Start Docker Desktop before switching the worker."
& wsl.exe -d $distribution --exec bash ($packagePath.Trim() + '/Update-Worker.sh') @workerArguments
exit $LASTEXITCODE
