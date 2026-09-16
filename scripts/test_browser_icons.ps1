param()
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Test the real Windows command-line parser and exact-profile guard without
# opening a browser, using WMI, or connecting to a running Center.
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$testRoot = Join-Path $repoRoot ('.runtime\browser-icon-tests-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
$source = Join-Path $testRoot 'ProfileTests.cs'
@'
using System;
using System.Drawing;
using System.Collections;
using System.Diagnostics;
using System.Reflection;
using System.Windows.Forms;
using ExperimentManagerDesktop;
namespace ExperimentManagerDesktop {
    static class App {
        internal static Icon LoadIcon(Size size) { throw new NotSupportedException("Profile tests never load icons."); }
    }
}
static class ProfileTests {
    static int count;
    static void Check(bool result,string label) { if(!result)throw new Exception(label);count++; }
    [STAThread] static int Main() {
        string profile=@"C:\Users\Test User\ExperimentManager\desktop\Controller\web-profile";
        string command="\"C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe\" --app=http://127.0.0.1:8765/ --user-data-dir=\""+profile+"\"";
        Check(BrowserAppWindow.MatchesProfile(command,profile),"quoted exact profile");
        Check(BrowserAppWindow.MatchesProfile(command,profile.ToUpperInvariant()),"case-insensitive Windows path");
        Check(BrowserAppWindow.MatchesProfile("msedge.exe --user-data-dir \""+profile+"\"",profile),"separate switch value");
        Check(BrowserAppWindow.MatchesProfile("msedge.exe --user-data-dir=\""+profile+"\\\\\"",profile),"escaped trailing slash");
        Check(!BrowserAppWindow.MatchesProfile(command,profile+"-other"),"profile prefix cannot select another profile");
        Check(!BrowserAppWindow.MatchesProfile(command+" --type=renderer",profile),"renderer process excluded");
        Check(!BrowserAppWindow.MatchesProfile(command+" --type gpu-process",profile),"GPU process excluded");
        Check(!BrowserAppWindow.MatchesProfile(command+" --user-data-dir=\""+profile+"\"",profile),"ambiguous duplicate profile excluded");
        Check(!BrowserAppWindow.MatchesProfile("msedge.exe --app=\"https://example.invalid/?profile=--user-data-dir="+profile+"\"",profile),"URL substring cannot select a profile");
        Check(!BrowserAppWindow.MatchesProfile("msedge.exe --some-user-data-dir=\""+profile+"\"",profile),"similar option cannot select a profile");
        Check(!BrowserAppWindow.MatchesProfile("msedge.exe --user-data-dir",profile),"missing switch value excluded");
        Check(!BrowserAppWindow.MatchesProfile("msedge.exe --user-data-dir=relative-path",profile),"relative profile excluded");
        Check(!BrowserAppWindow.MatchesProfile(command,""),"empty expected profile excluded");
        Check(!BrowserAppWindow.MatchesProfile(null,profile),"missing command line excluded");
        Check(BrowserAppWindow.CanonicalPath("C:\\invalid\0path")=="","invalid path fails closed");
        // Use hidden test-owned native windows to verify the actual WM_CLOSE
        // path. No browser or WMI discovery is involved in these fixtures.
        using(var tracked=new BrowserAppWindow(@"C:\test-edge.exe",profile))
        using(var owned=new Form())using(var unrelated=new Form())using(var stale=new Form())
        using(var process=Process.GetCurrentProcess()) {
            var ownedHandle=owned.Handle;var unrelatedHandle=unrelated.Handle;var staleHandle=stale.Handle;
            var identities=(IDictionary)typeof(BrowserAppWindow).GetField("windows",BindingFlags.Instance|BindingFlags.NonPublic).GetValue(tracked);
            var identityType=typeof(BrowserAppWindow).GetNestedType("WindowIdentity",BindingFlags.NonPublic);
            foreach(var window in new[]{ownedHandle,staleHandle}) {
                object identity=Activator.CreateInstance(identityType,true);
                identityType.GetField("Pid",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(identity,process.Id);
                identityType.GetField("Started",BindingFlags.Instance|BindingFlags.NonPublic).SetValue(identity,
                    process.StartTime.ToUniversalTime().Ticks+(window==staleHandle?1:0));
                identities.Add(window,identity);
            }
            typeof(BrowserAppWindow).GetMethod("RequestCloseTrackedWindows",BindingFlags.Instance|BindingFlags.NonPublic).Invoke(tracked,null);
            Application.DoEvents();
            Check(owned.IsDisposed,"verified native window closed");
            Check(!unrelated.IsDisposed,"untracked native window preserved");
            Check(!stale.IsDisposed,"reused process identity preserved");
        }
        Console.WriteLine("Browser profile guard: "+count+" assertions passed.");
        return 0;
    }
}
'@ | Set-Content -LiteralPath $source -Encoding UTF8

$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler)) { $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe' }
$output = Join-Path $testRoot 'ProfileTests.exe'
& $compiler /nologo /target:exe /r:System.Drawing.dll /r:System.Windows.Forms.dll /r:System.Management.dll "/out:$output" $source (Join-Path $repoRoot 'deploy\desktop\BrowserAppWindow.cs')
if ($LASTEXITCODE -ne 0) { throw 'Browser profile tests did not compile.' }
& $output
if ($LASTEXITCODE -ne 0) { throw 'Browser profile guard failed.' }
