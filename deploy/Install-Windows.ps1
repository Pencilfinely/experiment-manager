$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
Write-Host 'Experiment Manager node setup'
Write-Host 'Checking WSL distributions...'
$distributionOutput = & wsl.exe --list --quiet 2>$null
$wslExit = $LASTEXITCODE
$distributions = @($distributionOutput | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ -and $_ -notmatch '^docker-desktop' })
if ($wslExit -ne 0 -or $distributions.Count -eq 0) {
    Write-Host 'No Linux distribution is ready. Open an administrator PowerShell and run:'
    Write-Host '  wsl --install -d Ubuntu'
    Write-Host 'Finish the Ubuntu user setup, restart Windows if requested, then run this installer again.'
    Start-Process 'https://learn.microsoft.com/windows/wsl/install'
    exit 2
}
if ($distributions.Count -eq 1) {
    $selectedDistribution = $distributions[0]
} else {
    for ($index = 0; $index -lt $distributions.Count; $index++) { Write-Host "[$index] $($distributions[$index])" }
    $choiceText = Read-Host 'Choose the Linux distribution number'
    $choiceNumber = 0
    if (-not [int]::TryParse($choiceText, [ref]$choiceNumber) -or $choiceNumber -lt 0 -or $choiceNumber -ge $distributions.Count) { throw 'Invalid distribution number.' }
    $selectedDistribution = $distributions[$choiceNumber]
}
Write-Host "Using: $selectedDistribution"
& wsl.exe --distribution $selectedDistribution --exec docker info --format '{{.ServerVersion}}'
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Docker is not ready in this distribution.'
    Write-Host 'Install/start Docker Desktop, enable Settings > Resources > WSL Integration for this distribution, then run this installer again.'
    Start-Process 'https://docs.docker.com/desktop/setup/install/windows-install/'
    exit 2
}
$packagePath = Join-Path $PSScriptRoot 'ExperimentNode.pyz'
if (-not (Test-Path -LiteralPath $packagePath -PathType Leaf)) { throw 'Extract the complete ZIP first; ExperimentNode.pyz is missing.' }
$linuxPackage = & wsl.exe --distribution $selectedDistribution --exec wslpath -a $packagePath
if ($LASTEXITCODE -ne 0) { throw 'Could not translate the installer path to WSL.' }
& wsl.exe --distribution $selectedDistribution --exec python3 $linuxPackage.Trim()
exit $LASTEXITCODE
