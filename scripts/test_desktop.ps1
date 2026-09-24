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

Add-Type -AssemblyName System.Drawing, System.Windows.Forms
Add-Type -ReferencedAssemblies System.Drawing, System.Windows.Forms -TypeDefinition @'
using System;
using System.Drawing;
using System.Runtime.InteropServices;
public static class DesktopIconTest {
    [DllImport("shell32.dll", CharSet=CharSet.Unicode)]
    static extern uint ExtractIconEx(string path, int index, IntPtr[] large, IntPtr[] small, uint count);
    [DllImport("user32.dll")]
    static extern bool DestroyIcon(IntPtr icon);
    public static Icon Extract(string path, bool useSmall) {
        var large=new IntPtr[1]; var small=new IntPtr[1];
        try {
            uint count=ExtractIconEx(path,0,large,small,1);
            if(count==0||count==UInt32.MaxValue) throw new Exception("Executable has no native icon: "+path);
            IntPtr selected=useSmall?small[0]:large[0];
            if(selected==IntPtr.Zero) throw new Exception("Executable icon could not be extracted: "+path);
            using(var icon=Icon.FromHandle(selected)) return (Icon)icon.Clone();
        } finally {
            if(large[0]!=IntPtr.Zero) DestroyIcon(large[0]);
            if(small[0]!=IntPtr.Zero) DestroyIcon(small[0]);
        }
    }
}
public static class DesktopWorkerInstallTest {
    public static void VerifyUnpaired(System.Reflection.MethodInfo method) {
        var stopped=new System.Collections.Generic.Dictionary<string,object>{{"running",false},{"ready_for_install",true}};
        Func<string[],System.Collections.Generic.Dictionary<string,object>> unexpected=arguments=> {
            throw new Exception("Fresh unpaired desktop installation unexpectedly invoked its WSL installer.");
        };
        method.Invoke(null,new object[]{"/tmp/unpaired desktop package","0.4.0",stopped,unexpected});
    }
}
public static class DesktopGpuSettingsTest {
    public static void Verify(System.Type formType, string screenshot) {
        var flags=System.Reflection.BindingFlags.Instance|System.Reflection.BindingFlags.NonPublic;
        const string target="GPU-738c5e76-1914-9b77-77df-7a57a48b996c";
        bool enabled=false;int mutations=0;
        Func<string,string[],System.Threading.Tasks.Task<System.Collections.Generic.Dictionary<string,object>>> command=(action,options)=> {
            if(action=="gpu-enable") {
                if(options.Length!=2||options[0]!="--gpu"||options[1]!=target)throw new Exception("GPU settings selected the wrong UUID");
                mutations++;
                if(mutations==1)return System.Threading.Tasks.Task.FromResult(new System.Collections.Generic.Dictionary<string,object>{{"status","failed"},{"detail","CUDA check failed; original configuration preserved."}});
                enabled=true;
                return System.Threading.Tasks.Task.FromResult(new System.Collections.Generic.Dictionary<string,object>{{"status","succeeded"},{"detail","GPU verified and enabled."}});
            }
            if(action!="gpu-status")throw new Exception("Unexpected GPU settings command: "+action);
            object[] cards={
                new System.Collections.Generic.Dictionary<string,object>{{"uuid","GPU-5647b081-f9d7-1019-0a1d-a026a2a4da30"},{"name","NVIDIA GeForce RTX 2080 Ti"},{"reason","\u5df2\u542f\u7528"},{"local_enabled",true},{"can_enable",true}},
                new System.Collections.Generic.Dictionary<string,object>{{"uuid",target},{"name","NVIDIA GeForce RTX 2080 Ti"},{"reason",enabled?"\u5df2\u542f\u7528":"\u672c\u5730\u672a\u542f\u7528"},{"local_enabled",enabled},{"can_enable",true}}
            };
            return System.Threading.Tasks.Task.FromResult(new System.Collections.Generic.Dictionary<string,object>{{"gpus",cards}});
        };
        using(var form=(System.Windows.Forms.Form)System.Activator.CreateInstance(formType,flags,null,new object[]{command},null)) {
            form.ShowInTaskbar=false;form.StartPosition=System.Windows.Forms.FormStartPosition.Manual;
            form.Location=new System.Drawing.Point(-20000,-20000);form.Show();
            System.Windows.Forms.Application.DoEvents();
            var handle=form.Handle;
            var cards=(System.Windows.Forms.ListView)formType.GetField("cards",flags).GetValue(form);
            var cardHandle=cards.Handle;
            ((System.Threading.Tasks.Task)formType.GetMethod("LoadCards",flags).Invoke(form,null)).GetAwaiter().GetResult();
            if(cards.Items.Count!=2)throw new Exception("GPU settings inventory did not render both cards");
            cards.Items[0].Selected=false;cards.Items[1].Selected=true;
            formType.GetMethod("UpdateSelection",flags).Invoke(form,null);
            var button=(System.Windows.Forms.Button)formType.GetField("enable",flags).GetValue(form);
            if(!button.Enabled)throw new Exception("Disabled GPU cannot be enabled from settings");
            form.PerformLayout();
            if(cards.Width<form.ClientSize.Width*0.8||cards.Height<100)throw new Exception("GPU settings layout is collapsed");
            using(var bitmap=new System.Drawing.Bitmap(form.Width,form.Height)) {
                form.DrawToBitmap(bitmap,new System.Drawing.Rectangle(0,0,form.Width,form.Height));
                bitmap.Save(screenshot,System.Drawing.Imaging.ImageFormat.Png);
            }
            ((System.Threading.Tasks.Task)formType.GetMethod("EnableSelected",flags).Invoke(form,null)).GetAwaiter().GetResult();
            if(mutations!=1||!button.Enabled)throw new Exception("GPU settings cannot retry a failed check");
            ((System.Threading.Tasks.Task)formType.GetMethod("EnableSelected",flags).Invoke(form,null)).GetAwaiter().GetResult();
            if(mutations!=2||button.Enabled||cards.Items[1].SubItems[2].Text!="\u5df2\u542f\u7528")throw new Exception("GPU settings did not refresh after enabling");
            ((System.Threading.Tasks.Task)formType.GetMethod("EnableSelected",flags).Invoke(form,null)).GetAwaiter().GetResult();
            if(mutations!=2)throw new Exception("GPU settings re-enabled an already enabled card");
        }
    }
}
'@

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
        # by reflection; command transport and icon loading are exercised directly.
        $assembly = [Reflection.Assembly]::Load([IO.File]::ReadAllBytes($executable))
        $app = $assembly.GetType('ExperimentManagerDesktop.App', $true)
        if ($role -eq 'worker') {
            [DesktopGpuSettingsTest]::Verify($assembly.GetType('ExperimentManagerDesktop.WorkerGpuForm', $true),
                (Join-Path $runtimeRoot 'gpu-settings-preview.png'))
            Write-Output 'PASS worker GPU settings: correct UUID action, refreshed local state, duplicate-action prevention, rendered window.'
        }
        $flags = [Reflection.BindingFlags]'NonPublic,Static'
        $installer = $assembly.GetType('ExperimentManagerDesktop.InstallerForm', $true)
        $completeWorker = $installer.GetMethod('CompleteWorkerInstallation', $flags)
        $verifyStopped = $installer.GetMethod('VerifyBackendStopped', $flags)
        if (-not $completeWorker -or -not $verifyStopped -or
            $verifyStopped.ReturnType -ne [Collections.Generic.Dictionary[string,object]] -or
            $completeWorker.GetParameters().Count -ne 4) {
            throw "Worker installation completion/readiness contract is missing from the compiled $role client."
        }
        [DesktopWorkerInstallTest]::VerifyUnpaired($completeWorker)
        $app.GetField('Package', $flags).SetValue($null, $testRoot)
        $quote = $app.GetMethod('Quote', $flags)
        $run = $app.GetMethod('Run', $flags)
        if (-not $quote -or -not $run) { throw 'Desktop command transport methods are missing.' }
        $waitPrevious = $app.GetMethod('WaitForPreviousClient', $flags)
        if (-not $waitPrevious) { throw 'Update handoff method is missing.' }
        $waitInfo = New-Object Diagnostics.ProcessStartInfo
        $waitInfo.FileName = $Python
        $waitInfo.Arguments = '-c "import time; time.sleep(0.8)"'
        $waitInfo.UseShellExecute = $false
        $waitInfo.CreateNoWindow = $true
        $previous = [Diagnostics.Process]::Start($waitInfo)
        try {
            $waitArgs = [string[]]@('--wait-pid', [string]$previous.Id, '--wait-start', [string]$previous.StartTime.ToUniversalTime().Ticks)
            $null = Invoke-AppMethod $waitPrevious ([object[]]@(,$waitArgs))
            if (-not $previous.HasExited) { throw 'Update installer did not wait for the previous client.' }
        }
        finally { $previous.Dispose() }

        $loadIcon = $app.GetMethod('LoadIcon', $flags)
        if (-not $loadIcon) { throw 'Desktop icon loader is missing.' }
        foreach ($small in @($false, $true)) {
            $size = if ($small) { [Windows.Forms.SystemInformation]::SmallIconSize } else { [Windows.Forms.SystemInformation]::IconSize }
            $managedIcon = Invoke-AppMethod $loadIcon ([object[]]@($size))
            $nativeIcon = $null
            $managedBitmap = $null
            $nativeBitmap = $null
            try {
                $nativeIcon = [DesktopIconTest]::Extract($executable, $small)
                $managedBitmap = $managedIcon.ToBitmap()
                $nativeBitmap = $nativeIcon.ToBitmap()
                if ($managedBitmap.Size -ne $nativeBitmap.Size) { throw "$role native/managed icon sizes differ." }
                for ($y = 0; $y -lt $managedBitmap.Height; $y++) {
                    for ($x = 0; $x -lt $managedBitmap.Width; $x++) {
                        if ($managedBitmap.GetPixel($x, $y).ToArgb() -ne $nativeBitmap.GetPixel($x, $y).ToArgb()) {
                            throw "$role executable icon does not match its window/tray icon."
                        }
                    }
                }
            }
            finally {
                if ($nativeBitmap) { $nativeBitmap.Dispose() }
                if ($managedBitmap) { $managedBitmap.Dispose() }
                if ($nativeIcon) { $nativeIcon.Dispose() }
                $managedIcon.Dispose()
            }
        }

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
        if ($report.version -cne (Get-Content -LiteralPath (Join-Path $repoRoot 'VERSION') -Raw).Trim()) {
            throw "Compiled application version does not match VERSION: $role"
        }
        $iconName = if ($role -eq 'controller') { 'center.ico' } else { 'worker.ico' }
        $expectedIconHash = (Get-FileHash -LiteralPath (Join-Path $repoRoot ('assets/' + $iconName)) -Algorithm SHA256).Hash
        if ($report.icon_sha256 -ine $expectedIconHash -or ($report.runtime_icon_sizes -join ',') -ne '16,20,24,28,32,36,40,44,48,56,64,72,80,88,96,112,128') {
            throw "Embedded icon resource or its frames do not match the expected role: $role"
        }
        Write-Output "PASS $role : native compilation, role-specific EXE/window/tray icons, WinForms ICO frames, embedded role, bare options, exact Python argv roundtrip, unpaired worker installation remains inactive."
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
