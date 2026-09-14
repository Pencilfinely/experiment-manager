param([switch]$ConfigureProject)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
Write-Host 'Experiment Manager - Windows worker (WSL2)'
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { throw 'Install WSL2 with Ubuntu first: https://learn.microsoft.com/windows/wsl/install' }
$inventory = & wsl.exe --list --quiet 2>$null
if ($LASTEXITCODE -ne 0) { throw 'WSL is not ready. Complete the WSL2/Ubuntu installation first.' }
$distributions = @($inventory | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ -and $_ -notmatch '^docker-desktop' })
if ($distributions.Count -eq 0) { throw 'Install Ubuntu in WSL2, open it once and create a normal Linux user.' }
$settingsDirectory = Join-Path $env:LOCALAPPDATA 'ExperimentManager\worker-launcher'
$settingsPath = Join-Path $settingsDirectory 'distribution.txt'
$distribution = if (Test-Path -LiteralPath $settingsPath) { (Get-Content -LiteralPath $settingsPath -Raw).Trim() } else { '' }
if ($distribution -notin $distributions) {
    $distribution = @($distributions | Where-Object { $_ -match '^Ubuntu' } | Select-Object -First 1)[0]
    if (-not $distribution) { $distribution = $distributions[0] }
    if ($distributions.Count -gt 1) {
        for ($index=0; $index -lt $distributions.Count; $index++) { Write-Host "[$index] $($distributions[$index])" }
        $choice = Read-Host "Choose distribution number (Enter uses $distribution)"
        if ($choice) {
            $number = 0
            if (-not [int]::TryParse($choice,[ref]$number) -or $number -lt 0 -or $number -ge $distributions.Count) { throw 'Invalid distribution number.' }
            $distribution = $distributions[$number]
        }
    }
    New-Item -ItemType Directory -Path $settingsDirectory -Force | Out-Null
    Set-Content -LiteralPath $settingsPath -Value $distribution -Encoding UTF8
}
Write-Host "Using $distribution. Docker Desktop must be running with WSL integration enabled."
$packagePath = & wsl.exe -d $distribution --exec wslpath -a $PSScriptRoot
if ($LASTEXITCODE -ne 0) { throw 'Could not translate this folder to a WSL path. Extract the complete ZIP to a local disk.' }
$entry = if ($ConfigureProject) { '/Configure-Project.sh' } else { '/Start-Worker.sh' }
& wsl.exe -d $distribution --exec bash ($packagePath.Trim() + $entry)
exit $LASTEXITCODE
