param([string]$WorkerIP, [int]$Port = 0, [switch]$Elevated)
$ErrorActionPreference = 'Stop'
if (-not $WorkerIP) { $WorkerIP = Read-Host 'Worker IPv4 address (LAN or private VPN)' }
$address = $null
if (-not [System.Net.IPAddress]::TryParse($WorkerIP,[ref]$address) -or $address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) { throw 'Enter one valid worker IPv4 address.' }
if (-not $Port) {
    $settings = Join-Path $env:LOCALAPPDATA 'ExperimentManager\controller\launcher.json'
    if (-not (Test-Path -LiteralPath $settings)) { throw 'Start the controller first.' }
    $Port = (Get-Content -LiteralPath $settings -Raw | ConvertFrom-Json).port
}
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Invalid controller port.' }
if (-not $Elevated) {
    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -WorkerIP {1} -Port {2} -Elevated' -f $PSCommandPath,$address.ToString(),$Port
    $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw 'Windows did not enable the firewall rule.' }
    Write-Host 'Done. This worker can connect to the controller port.'
    exit 0
}
$ruleName = 'ExperimentManager-' + $Port + '-' + $address.ToString()
if (-not (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name $ruleName -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -RemoteAddress $address.ToString() -Profile Any | Out-Null
}
