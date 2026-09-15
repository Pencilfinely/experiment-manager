param([string]$Python = '')

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $Python) {
    # Ignore Windows Store execution aliases; invoking one can open an installer
    # instead of running tests. setup-python's real executable takes precedence.
    $pythonCommand = Get-Command python -CommandType Application -All -ErrorAction Stop |
        Where-Object { $_.Source -notlike '*\Microsoft\WindowsApps\*' } |
        Select-Object -First 1
    if (-not $pythonCommand) { throw 'No installed Python was found. Supply -Python with its python.exe path.' }
    $Python = $pythonCommand.Source
}
$Python = (Resolve-Path -LiteralPath $Python).Path

# The shipped application targets .NET Framework. Run its reflection checks in
# that same runtime even when GitHub Actions initially invokes this with pwsh.
if ($PSVersionTable.PSEdition -ne 'Desktop') {
    $frameworkShell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    & $frameworkShell -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -Python $Python
    if ($LASTEXITCODE -ne 0) { throw "Native desktop tests failed ($LASTEXITCODE)." }
    exit 0
}

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runtimeRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot '.runtime'))
$testRoot = Join-Path $runtimeRoot ('desktop-tests-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null

function Invoke-AppMethod($Method, [object[]]$Values) {
    try { return $Method.Invoke($null, $Values) }
    catch {
        if ($_.Exception.InnerException) { throw $_.Exception.InnerException }
        throw
    }
}

try {
    foreach ($role in @('controller', 'worker')) {
        $executableName = if ($role -eq 'controller') { 'ExperimentCenter.exe' } else { 'ExperimentWorker.exe' }
        $executable = Join-Path $testRoot $executableName
        & $Python (Join-Path $PSScriptRoot 'build_desktop.py') --role $role --output $executable
        if ($LASTEXITCODE -ne 0) { throw "Failed to compile desktop role: $role" }

        # Load bytes rather than locking the compiled executable on disk. No
        # Form, NotifyIcon, installer, WSL session, or application Main is invoked
        # by reflection; only the real command transport methods are exercised.
        $assembly = [Reflection.Assembly]::Load([IO.File]::ReadAllBytes($executable))
        $app = $assembly.GetType('ExperimentManagerDesktop.App', $true)
        $flags = [Reflection.BindingFlags]'NonPublic,Static'
        $app.GetField('Package', $flags).SetValue($null, $testRoot)
        $quote = $app.GetMethod('Quote', $flags)
        $run = $app.GetMethod('Run', $flags)
        if (-not $quote -or -not $run) { throw 'Desktop command transport methods are missing.' }

        foreach ($option in @('--list', '-d', '--exec', '--')) {
            $quoted = Invoke-AppMethod $quote ([object[]]@($option))
            if ($quoted -cne $option) {
                throw "Simple option was quoted; WSL's option parser rejects this: $option => $quoted"
            }
        }

        $unicode = -join @([char]0x7B97, [char]0x529B, [char]0x8282, [char]0x70B9, [char]0x00E9)
        [string[]]$arguments = @(
            'plain', '--list', '-d', '-c', 'two words', 'quote"inside',
            'C:\directory with spaces\', 'C:\plain\',
            'backslashes\\"before-quote', $unicode, "tab`tvalue", "line`nvalue", ''
        )
        $pythonCode = 'import json, sys; print(json.dumps(sys.argv[1:], ensure_ascii=True))'
        [object[]]$invoke = @($Python, [string[]](@('-c', $pythonCode, '--') + $arguments))
        $output = Invoke-AppMethod $run $invoke
        [object[]]$actual = ConvertFrom-Json -InputObject ([string]$output)
        $expected = @('--') + $arguments
        if ($actual.Count -ne $expected.Count) {
            throw "$role argv count changed: expected $($expected.Count), received $($actual.Count)."
        }
        for ($index = 0; $index -lt $expected.Count; $index++) {
            if ($actual[$index] -cne $expected[$index]) {
                throw "$role argv roundtrip changed argument $index. Expected $($expected[$index] | ConvertTo-Json -Compress); received $($actual[$index] | ConvertTo-Json -Compress)."
            }
        }

        # --self-test exits before any application or installer window is shown.
        # The actual executable reads its embedded Role resource and writes a
        # report. This also checks the flag/space transport used to start it.
        $reportPath = Join-Path $testRoot ($role + ' self-test.json')
        [object[]]$selfTestArgs = @([string]$executable, [string[]]@('--self-test', '--report', $reportPath))
        $null = Invoke-AppMethod $run $selfTestArgs
        if (-not (Test-Path -LiteralPath $reportPath -PathType Leaf)) {
            throw "Desktop self-test produced no report: $role"
        }
        $report = Get-Content -LiteralPath $reportPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $expectedRole = if ($role -eq 'controller') { 'Controller' } else { 'Worker' }
        if ($report.status -ne 'passed' -or $report.role -cne $expectedRole -or
            $report.executable -cne $executableName -or $report.payload_files -ne 0) {
            throw "Invalid portable desktop self-test result for $role."
        }
        Write-Output "PASS $role : native compilation, embedded role, bare options, exact Python argv roundtrip."
    }
}
finally {
    # Only this invocation's verified workspace directory is removed. Never pass
    # computed cleanup paths to a second shell or delete a parent runtime folder.
    $resolved = [IO.Path]::GetFullPath($testRoot)
    $parent = [IO.Path]::GetDirectoryName($resolved)
    if ($parent -ne $runtimeRoot -or [IO.Path]::GetFileName($resolved) -notmatch '^desktop-tests-[0-9a-f]{32}$') {
        throw 'Unexpected desktop test cleanup directory.'
    }
    if (Test-Path -LiteralPath $resolved) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
