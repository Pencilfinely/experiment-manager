param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# The installer subprocess checks exercise .NET Framework, as shipped. Match
# the desktop test runner when CI invokes this script from PowerShell 7.
if ($PSVersionTable.PSEdition -ne 'Desktop') {
    $frameworkShell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'
    & $frameworkShell -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath
    if ($LASTEXITCODE -ne 0) { throw "Native update tests failed ($LASTEXITCODE)." }
    exit 0
}

# Compile against the same .NET Framework/C# version as the shipped desktop app.
# The harness exercises deterministic streams; it never contacts GitHub, creates
# UI, starts a worker, or executes an installer.
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runtimeRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot '.runtime'))
$testRoot = Join-Path $runtimeRoot ('update-tests-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}
$harness = @'
using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using ExperimentManagerDesktop;

static class UpdateTests {
    static int assertions;
    static void Assert(bool condition, string message) {
        assertions++;
        if(!condition) throw new Exception(message);
    }
    static void Fails<T>(Action action, string message) where T : Exception {
        assertions++;
        try { action(); } catch(T) { return; }
        throw new Exception(message);
    }
    static string Hash(byte[] bytes) {
        using(var sha = SHA256.Create()) return BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant();
    }
    static Dictionary<string,object> Asset(string tag, string name, long size) {
        return new Dictionary<string,object> { { "name", name }, { "size", size }, { "state", "uploaded" },
            { "browser_download_url", UpdateService.Repository + "/releases/download/" + tag + "/" + name } };
    }
    static Dictionary<string,object> Release(string version, bool draft, bool preview, string role = "controller") {
        string tag = "v" + version;
        string name = "ExperimentManager-" + version + "-windows-" + role + "-x64-Setup.exe";
        return new Dictionary<string,object> { { "tag_name", tag }, { "draft", draft }, { "prerelease", preview },
            { "body", "Release notes" }, { "assets", new object[] { Asset(tag, name, 1024), Asset(tag, "SHA256SUMS.txt", 100) } } };
    }
    static string Json(params object[] releases) { return new JavaScriptSerializer().Serialize(releases); }
    static UpdateRelease FileRelease(byte[] bytes) {
        string version = "0.3.0-rc.2", tag = "v" + version;
        string name = "ExperimentManager-" + version + "-windows-controller-x64-Setup.exe";
        string download = UpdateService.Repository + "/releases/download/" + tag + "/";
        return new UpdateRelease { Version = version, Tag = tag, AssetName = name,
            AssetUrl = download + name, ChecksumUrl = download + "SHA256SUMS.txt", Size = bytes.Length, Sha256 = Hash(bytes) };
    }
    static void NoPartials(string root) { Assert(Directory.GetFiles(root, "*.part").Length == 0, "A partial download was left behind."); }
    static int Main(string[] args) {
        try { Run(args[0]); Console.WriteLine("PASS update service: " + assertions + " assertions; semantic versions, channels, exact role assets, HTTPS origins, checksum, truncation, cache finalization and cancellation."); return 0; }
        catch(Exception ex) { Console.Error.WriteLine(ex); return 1; }
    }
    static void Run(string root) {
        Assert(DesktopLifecycle.CanExitUnconfiguredWorker("",new string[0]),"Fresh worker without WSL cannot exit.");
        Assert(DesktopLifecycle.CanExitUnconfiguredWorker("",new[]{"docker-desktop","docker-desktop-data"}),"Docker Desktop alone was mistaken for a configured worker.");
        Assert(!DesktopLifecycle.CanExitUnconfiguredWorker("Ubuntu",new string[0]),"Configured worker skipped backend shutdown.");
        Assert(!DesktopLifecycle.CanExitUnconfiguredWorker("",new[]{"Ubuntu"}),"Existing WSL environment bypassed shutdown checks.");
        Assert(!DesktopLifecycle.CanExitUnconfiguredWorker("",null),"Unknown WSL inventory permitted unchecked exit.");
        Assert(!DesktopLifecycle.CanExitUnconfiguredWorker("",new[]{""}),"Malformed WSL registration permitted unchecked exit.");
        Assert(UpdateService.BackendMatchesRelease("0.3.0rc4","0.3.0-rc.4"),"Python RC version never confirmed new backend startup.");
        Assert(UpdateService.BackendMatchesRelease("0.3.0-rc.4","v0.3.0-rc.4"),"SemVer backend spelling was rejected.");
        Assert(!UpdateService.BackendMatchesRelease("0.3.0rc1","0.3.0-rc.4"),"Old backend was reported as upgraded.");
        Assert(!UpdateService.BackendMatchesRelease("unknown","0.3.0-rc.4"),"Unknown backend was reported as upgraded.");
        Assert(!UpdateService.BackendMatchesRelease(null,"0.3.0-rc.4"),"Missing backend version was reported as upgraded.");
        DesktopLifecycle.RequireInstallReady(new Dictionary<string,object>{{"running",false},{"ready_for_install",true}},false);
        assertions++;
        Fails<InvalidOperationException>(() => DesktopLifecycle.RequireInstallReady(
            new Dictionary<string,object>{{"running",true},{"ready_for_install",true}},false),
            "Manual installer accepted an idle but still running legacy backend.");
        Fails<InvalidOperationException>(() => DesktopLifecycle.RequireInstallReady(
            new Dictionary<string,object>{{"running",false},{"ready_for_install",false}},true),
            "Worker installation ignored an unsafe local execution state.");
        Fails<InvalidOperationException>(() => DesktopLifecycle.RequireInstallReady(
            new Dictionary<string,object>{{"ready_for_install",true}},false),
            "Manual installer inferred that an unknown backend was stopped.");
        var statuses = new Queue<Dictionary<string,object>>(new[] {
            new Dictionary<string,object>{{"running",true},{"status","unresponsive"}},
            new Dictionary<string,object>{{"running",false},{"status","shutting_down"}},
            new Dictionary<string,object>{{"running",false},{"status","stopped"}}
        });
        int observed = 0, delays = 0;
        DesktopLifecycle.WaitForStopped(() => Task.FromResult(statuses.Dequeue()), state => observed++, null,
            () => { delays++; return Task.FromResult(0); }).GetAwaiter().GetResult();
        Assert(observed == 3 && delays == 2, "Exit completed while service was still running or shutting down.");
        Fails<InvalidDataException>(() => DesktopLifecycle.WaitForStopped(
            () => Task.FromResult(new Dictionary<string,object>{{"status","stopped"}}), null, null).GetAwaiter().GetResult(),
            "Missing running flag was treated as a stopped backend.");
        Fails<IOException>(() => DesktopLifecycle.WaitForStopped(
            () => Task.FromResult(new Dictionary<string,object>{{"running",true},{"status","exit_failed"},{"detail","Checkpoint failed"}}),
            null, null).GetAwaiter().GetResult(), "Failed checkpoint exit was ignored.");
        Fails<IOException>(() => DesktopLifecycle.WaitForStopped(
            () => Task.FromResult(new Dictionary<string,object>{{"running",false},{"status","unknown"}}),
            null, TimeSpan.Zero).GetAwaiter().GetResult(), "Unknown status permitted update handoff.");
        Fails<IOException>(() => DesktopLifecycle.WaitForStopped(
            () => Task.FromResult(new Dictionary<string,object>{{"running",true},{"status","stopping"}}),
            null, TimeSpan.Zero).GetAwaiter().GetResult(), "Stop timeout was silently accepted.");
        Fails<IOException>(() => DesktopLifecycle.WaitForStopped(
            () => Task.FromResult(new Dictionary<string,object>{{"running",false},{"status","failed"}}),
            null, null, finalStatus:"stopped").GetAwaiter().GetResult(),
            "Worker crash was treated as completed experiment shutdown.");
        Assert(UpdateService.CompareVersions("0.3.0-rc.10", "0.3.0-rc.2") > 0, "RC versions must compare numerically.");
        Assert(UpdateService.CompareVersions("v0.3.0", "0.3.0-rc.99") > 0, "Stable must follow prerelease.");
        Assert(UpdateService.CompareVersions("1.0.0+build.2", "1.0.0+build.1") == 0, "Build metadata must not change precedence.");
        Assert(UpdateService.CompareVersions("0.10.0", "0.9.99") > 0, "Version components must compare numerically.");
        Assert(UpdateService.CompareVersions("1.0.0-alpha.1", "1.0.0-alpha.beta") < 0, "Numeric prerelease identifiers must come first.");
        Assert(UpdateService.CompareVersions("1.0.0-alpha", "1.0.0-alpha.1") < 0, "Shorter matching prerelease must come first.");
        Assert(UpdateService.CompareVersions("99999999999999999999999999.0.0", "9999999999999999999999999.0.0") > 0, "Large numeric components must not overflow.");
        Fails<FormatException>(() => UpdateService.CompareVersions("1.0.0-rc.01", "1.0.0"), "Numeric leading zero accepted.");
        Fails<FormatException>(() => UpdateService.CompareVersions("01.0.0", "1.0.0"), "Core leading zero accepted.");
        Fails<FormatException>(() => UpdateService.CompareVersions("../1.0.0", "1.0.0"), "Unsafe version accepted.");

        var rc2 = Release("0.3.0-rc.2", false, true);
        var rc10 = Release("0.3.0-rc.10", false, true);
        var stable = Release("0.3.0", false, false);
        var future = Release("2.0.0-rc.1", false, true);
        var draft = Release("9.0.0", true, false);
        var selected = UpdateService.SelectRelease(Json(rc10, draft, rc2), "0.3.0-rc.1", false);
        Assert(selected.Version == "0.3.0-rc.10", "API order or draft affected maximum version.");
        Assert(UpdateService.SelectRelease(Json(rc2, stable), "0.3.0-rc.1", false).Version == "0.3.0", "Preview cannot advance to stable.");
        Assert(UpdateService.SelectRelease(Json(future, stable, draft), "0.2.0", false).Version == "0.3.0", "Stable selected a preview or draft.");
        Assert(UpdateService.SelectRelease(Json(Release("3.0.0", false, true), stable), "0.2.0", false).Version == "0.3.0", "GitHub prerelease flag was ignored.");
        Assert(UpdateService.SelectRelease(Json(Release("3.0.0-rc.1", false, false), stable), "0.2.0", false).Version == "0.3.0", "SemVer prerelease flag was ignored.");
        Assert(UpdateService.SelectRelease(Json(stable, rc10), "0.3.0", false) == null, "Current version offered downgrade/reinstall.");
        Assert(UpdateService.SelectRelease(Json(rc2), "0.3.0-rc.1", true) == null, "Worker selected Controller installer.");
        Assert(UpdateService.SelectRelease(Json(Release("0.3.0-rc.2", false, true, "worker")), "0.3.0-rc.1", true).AssetName.EndsWith("worker-x64-Setup.exe"), "Worker asset missing.");
        var missingChecksum = Release("0.4.0", false, false);
        missingChecksum["assets"] = new object[] { ((object[])missingChecksum["assets"])[0] };
        Assert(UpdateService.SelectRelease(Json(missingChecksum), "0.3.0", false) == null, "Unchecked release offered.");
        Fails<InvalidDataException>(() => UpdateService.SelectRelease("{}", "0.3.0", false), "Invalid release list accepted.");
        var wrongRepo = Release("0.4.0", false, false);
        ((Dictionary<string,object>)((object[])wrongRepo["assets"])[0])["browser_download_url"] = "https://github.com/other/repository/releases/download/v0.4.0/test.exe";
        Fails<InvalidDataException>(() => UpdateService.SelectRelease(Json(wrongRepo), "0.3.0", false), "Other repository accepted.");

        string tag = "v0.3.0-rc.2", name = "ExperimentManager-0.3.0-rc.2-windows-controller-x64-Setup.exe";
        string url = UpdateService.Repository + "/releases/download/" + tag + "/" + name;
        UpdateService.ValidateAssetUri(url, tag, name);
        Fails<InvalidDataException>(() => UpdateService.ValidateAssetUri(url.Replace("https:", "http:"), tag, name), "HTTP accepted.");
        Fails<InvalidDataException>(() => UpdateService.ValidateAssetUri(url + "?redirect=1", tag, name), "Query accepted in initial download.");
        Fails<InvalidDataException>(() => UpdateService.ValidateAssetUri(url.Replace("github.com", "github.com.evil.invalid"), tag, name), "Lookalike domain accepted.");
        Fails<InvalidDataException>(() => UpdateService.ValidateAssetUri(url.Replace(tag, "v0.3.0-rc.1"), tag, name), "Different release accepted.");
        Assert(UpdateService.AllowedRequestUri(new Uri("https://release-assets.githubusercontent.com/asset?sig=1"), false, false), "GitHub asset redirect rejected.");
        Assert(!UpdateService.AllowedRequestUri(new Uri("https://release-assets.githubusercontent.com/asset"), false, true), "Direct CDN origin accepted.");
        Assert(!UpdateService.AllowedRequestUri(new Uri("https://evil.githubusercontent.com/asset"), false, false), "Arbitrary GitHub subdomain accepted.");
        Assert(!UpdateService.AllowedRequestUri(new Uri("https://github.com/other/repo/releases/download/v1/a.exe"), false, false), "Cross-repository redirect accepted.");
        Assert(!UpdateService.AllowedRequestUri(new Uri("http://release-assets.githubusercontent.com/asset"), false, false), "HTTP redirect accepted.");
        Assert(!UpdateService.AllowedRequestUri(new Uri("https://user@release-assets.githubusercontent.com/asset"), false, false), "Userinfo accepted.");
        Assert(!UpdateService.AllowedRequestUri(new Uri("https://release-assets.githubusercontent.com:444/asset"), false, false), "Unexpected port accepted.");
        Assert(UpdateService.AllowedRequestUri(new Uri(UpdateService.ReleasesApi), true, true), "Public API rejected.");
        Assert(!UpdateService.AllowedRequestUri(new Uri("https://api.github.com/repos/other/repo/releases?per_page=100"), true, false), "Other API repository accepted.");

        byte[] bytes = new byte[300000]; bytes[0] = (byte)'M'; bytes[1] = (byte)'Z';
        for(int i = 2; i < bytes.Length; i++) bytes[i] = (byte)(i % 251);
        string hash = Hash(bytes);
        Assert(UpdateService.ParseChecksum(hash.ToUpperInvariant() + "  " + name + "\n", name) == hash, "Text checksum not accepted.");
        Assert(UpdateService.ParseChecksum("\uFEFF" + hash + " *" + name + "\r\n", name) == hash, "Binary/BOM checksum not accepted.");
        Fails<InvalidDataException>(() => UpdateService.ParseChecksum(hash + "  other.exe", name), "Different file checksum accepted.");
        Fails<InvalidDataException>(() => UpdateService.ParseChecksum(hash + "  " + name + "\n" + hash + "  " + name, name), "Duplicate checksum accepted.");
        Fails<InvalidDataException>(() => UpdateService.ParseChecksum("garbage  " + name, name), "Invalid checksum accepted.");

        string target = Path.Combine(root, "installer.exe");
        var file = FileRelease(bytes);
        long received = 0, announced = 0;
        using(var input = new MemoryStream(bytes)) {
            Assert(UpdateService.SaveDownload(input, file, target, (done, total) => { received = done; announced = total; }, CancellationToken.None) == target, "Wrong final path.");
        }
        Assert(received == bytes.Length && announced == bytes.Length, "Progress is incorrect.");
        UpdateService.ValidateFile(file, target, CancellationToken.None);
        Assert(Hash(File.ReadAllBytes(target)) == hash, "Final download differs from source.");
        NoPartials(root);
        string cached = Path.Combine(root, file.AssetName);
        File.WriteAllBytes(cached, bytes);
        Assert(UpdateService.Download(file, root, null, CancellationToken.None) == cached, "Valid cache was not reused without network.");
        UpdateService.ValidateDownloaded(file, cached);
        var altered = (byte[])bytes.Clone(); altered[20] ^= 1;
        File.WriteAllBytes(cached, altered);
        Fails<InvalidDataException>(() => UpdateService.ValidateDownloaded(file, cached), "Installer tampering after download was ignored.");
        File.Delete(cached);
        File.WriteAllText(target, "old cache");
        using(var input = new MemoryStream(bytes)) UpdateService.SaveDownload(input, file, target, null, CancellationToken.None);
        UpdateService.ValidateFile(file, target, CancellationToken.None);
        NoPartials(root);

        var badHash = FileRelease(bytes); badHash.Sha256 = new string('0', 64);
        Fails<InvalidDataException>(() => UpdateService.ValidateFile(badHash, target, CancellationToken.None), "Bad hash accepted.");
        using(var input = new MemoryStream(bytes)) Fails<InvalidDataException>(() => UpdateService.SaveDownload(input, badHash, target, null, CancellationToken.None), "Bad hash finalized.");
        Assert(Hash(File.ReadAllBytes(target)) == hash, "Bad download replaced good cache.");
        NoPartials(root);
        using(var input = new MemoryStream(new byte[] { (byte)'M', (byte)'Z' }))
            Fails<InvalidDataException>(() => UpdateService.SaveDownload(input, file, target, null, CancellationToken.None), "Truncated installer accepted.");
        NoPartials(root);
        var undersized = FileRelease(bytes); undersized.Size = bytes.Length - 1;
        using(var input = new MemoryStream(bytes)) Fails<InvalidDataException>(() => UpdateService.SaveDownload(input, undersized, target, null, CancellationToken.None), "Oversized installer accepted.");
        NoPartials(root);
        byte[] invalidExe = Encoding.UTF8.GetBytes("This is an HTML error.");
        using(var input = new MemoryStream(invalidExe)) Fails<InvalidDataException>(() => UpdateService.SaveDownload(input, FileRelease(invalidExe), target, null, CancellationToken.None), "Non-EXE finalized.");
        NoPartials(root);

        using(var cancel = new CancellationTokenSource()) {
            cancel.Cancel();
            Fails<OperationCanceledException>(() => UpdateService.Download(file, root, null, cancel.Token), "Download contacted network after cancellation.");
            using(var input = new MemoryStream(bytes)) Fails<OperationCanceledException>(() => UpdateService.SaveDownload(input, file, target, null, cancel.Token), "Initial cancellation ignored.");
        }
        NoPartials(root);
        using(var cancel = new CancellationTokenSource()) {
            using(var input = new MemoryStream(bytes)) Fails<OperationCanceledException>(() => UpdateService.SaveDownload(input, file, target, (done, total) => cancel.Cancel(), cancel.Token), "Midstream cancellation ignored.");
        }
        NoPartials(root);
        Assert(Hash(File.ReadAllBytes(target)) == hash, "Canceled download replaced good cache.");
        using(var input = new MemoryStream(bytes)) Fails<IOException>(() => UpdateService.SaveDownload(input, file, target, (done, total) => { throw new IOException("Simulated disconnect"); }, CancellationToken.None), "Disconnect unexpectedly succeeded.");
        NoPartials(root);
        Assert(Hash(File.ReadAllBytes(target)) == hash, "Failed download replaced good cache.");
    }
}
'@

try {
    $testSource = Join-Path $testRoot 'UpdateTests.cs'
    $testExe = Join-Path $testRoot 'UpdateTests.exe'
    [IO.File]::WriteAllText($testSource, $harness, (New-Object Text.UTF8Encoding $false))
    & $compiler /nologo /target:exe /optimize+ /langversion:5 /r:System.Web.Extensions.dll ("/out:" + $testExe) (Join-Path $repoRoot 'deploy/desktop/DesktopUpdates.cs') $testSource
    if ($LASTEXITCODE -ne 0) { throw 'Update service compilation failed.' }
    & $testExe $testRoot
    if ($LASTEXITCODE -ne 0) { throw 'Update service tests failed.' }

    # Exercise the installer's real subprocess boundary with an isolated status
    # executable. No application settings, service, browser or installer runs.
    $probeRuntime = Join-Path $testRoot 'runtime'
    New-Item -ItemType Directory -Path $probeRuntime | Out-Null
    $fakePythonSource = Join-Path $testRoot 'StatusFixture.cs'
    @'
using System;
using System.IO;
using System.Threading;
static class StatusFixture {
    static int Main(string[] args) {
        if(args.Length == 2 && args[0] == "--hold") {
            var limit=DateTime.UtcNow.AddSeconds(10);
            while(!File.Exists(args[1])&&DateTime.UtcNow<limit)Thread.Sleep(20);
            return File.Exists(args[1])?0:3;
        }
        if(args.Length != 5 || args[0] != "-m" || args[1] != "expman.desktop" ||
            args[2] != "controller-install-status" || args[3] != "--root")return 2;
        Console.Write(File.ReadAllText(Path.Combine(args[4],"status.json")));
        return 0;
    }
}
'@ | Set-Content -LiteralPath $fakePythonSource -Encoding UTF8
    & $compiler /nologo /target:exe ("/out:" + (Join-Path $probeRuntime 'python.exe')) $fakePythonSource
    if ($LASTEXITCODE -ne 0) { throw 'Isolated status fixture compilation failed.' }
    $probeSource = Join-Path $testRoot 'InstallProbeTests.cs'
    @'
using System;
using System.IO;
using System.Collections.Generic;
using System.Diagnostics;
using System.Threading;
using System.Threading.Tasks;
using ExperimentManagerDesktop;
static class InstallProbeTests {
    static int Main(string[] args) {
        try {
            App.Worker=false;
            string data=Path.Combine(args[0],"legacy data");Directory.CreateDirectory(data);
            string status=Path.Combine(data,"status.json");
            var settings=new Dictionary<string,object>();
            File.WriteAllText(status,"{\"running\":false,\"status\":\"stopped\",\"ready_for_install\":true,\"unverified_nodes\":1}");
            InstallerForm.VerifyBackendStopped(args[0],data,settings);
            foreach(string invalid in new[] {
                "{\"running\":true,\"status\":\"running\",\"version\":\"0.3.0rc1\",\"ready_for_install\":false}",
                "{\"running\":true,\"status\":\"unresponsive\",\"ready_for_install\":false}",
                "{\"running\":false,\"status\":\"stopped\",\"ready_for_update\":true}"
            }) {
                File.WriteAllText(status,invalid);bool refused=false;
                try {InstallerForm.VerifyBackendStopped(args[0],data,settings);}
                catch(InvalidOperationException){refused=true;}
                if(!refused)throw new Exception("Installer accepted an unverified or old running backend.");
            }
            string helper=Path.Combine(args[0],"runtime","python.exe");
            string release=Path.Combine(args[0],"release-client");
            using(var child=Process.Start(new ProcessStartInfo(helper,App.Arguments("--hold",release)){UseShellExecute=false,CreateNoWindow=true})) {
                var finish=Task.Run(()=>{Thread.Sleep(100);File.WriteAllText(release,"exit");});
                App.WaitForPreviousClient(new[]{"--wait-pid",child.Id.ToString(),"--wait-start",child.StartTime.ToUniversalTime().Ticks.ToString()});
                if(!child.HasExited)throw new Exception("Installer continued before previous client exited.");
                finish.GetAwaiter().GetResult();
            }
            File.Delete(release);
            using(var child=Process.Start(new ProcessStartInfo(helper,App.Arguments("--hold",release)){UseShellExecute=false,CreateNoWindow=true})) {
                App.WaitForPreviousClient(new[]{"--wait-pid",child.Id.ToString(),"--wait-start",(child.StartTime.ToUniversalTime().Ticks+1).ToString()});
                if(child.HasExited)throw new Exception("Installer waited on a reused process identity.");
                File.WriteAllText(release,"exit");
                child.WaitForExit(5000);
            }
            Console.WriteLine("PASS installer subprocess boundary: local stopped state, quoted data root, old backend, unresponsive backend, separate install protocol and previous-client handoff identity.");
            return 0;
        } catch(Exception ex){Console.Error.WriteLine(ex);return 1;}
    }
}
'@ | Set-Content -LiteralPath $probeSource -Encoding UTF8
    $probeExe = Join-Path $testRoot 'InstallProbeTests.exe'
    $references = @('System.Windows.Forms.dll','System.Drawing.dll','System.Web.Extensions.dll','System.IO.Compression.dll','System.IO.Compression.FileSystem.dll','Microsoft.CSharp.dll','System.Management.dll')
    $compileArgs = @('/nologo','/target:exe','/langversion:5','/main:InstallProbeTests',('/out:' + $probeExe))
    $compileArgs += $references | ForEach-Object { '/r:' + $_ }
    $compileArgs += @('ExperimentApp.cs','DesktopUpdates.cs','DesktopUpdateForm.cs','DesktopIcons.cs','BrowserAppWindow.cs') | ForEach-Object { Join-Path $repoRoot ('deploy/desktop/' + $_) }
    $compileArgs += $probeSource
    & $compiler @compileArgs
    if ($LASTEXITCODE -ne 0) { throw 'Installer probe tests compilation failed.' }
    & $probeExe $testRoot
    if ($LASTEXITCODE -ne 0) { throw 'Installer subprocess checks failed.' }
}
finally {
    # Only remove this test's freshly created directory beneath the workspace.
    $resolvedTestRoot = [IO.Path]::GetFullPath($testRoot)
    if (-not $resolvedTestRoot.StartsWith($runtimeRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Test cleanup target is outside the workspace runtime directory.'
    }
    if (Test-Path -LiteralPath $resolvedTestRoot) { Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force }
}
