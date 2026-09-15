param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Exercise real Win32 icon requests in a separate .NET Framework process. The
# windows stay hidden; neither application Main nor any service is started.
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runtimeRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot '.runtime'))
$testRoot = Join-Path $runtimeRoot ('icon-tests-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
}

$harness = @'
using System;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using ExperimentManagerDesktop;

static class IconTests {
    static int assertions;
    [DllImport("user32.dll")] static extern bool IsProcessDPIAware();
    [DllImport("user32.dll")] static extern uint GetDpiForSystem();
    [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr window, int message, IntPtr kind, IntPtr dpi);
    static void Assert(bool condition, string message) {
        assertions++;
        if(!condition) throw new Exception(message);
    }
    static void Compare(Icon icon, string file, int expectedSize, string label) {
        using(var bitmap=icon.ToBitmap()) using(var expected=new Bitmap(file)) {
            Assert(bitmap.Width==expectedSize&&bitmap.Height==expectedSize, label+" selected a resampled frame: "+bitmap.Size);
            Assert(expected.Size==bitmap.Size, label+" reference size differs.");
            for(int y=0;y<bitmap.Height;y++) for(int x=0;x<bitmap.Width;x++) {
                if(bitmap.GetPixel(x,y).ToArgb()!=expected.GetPixel(x,y).ToArgb())
                    throw new Exception(label+" differs from the vector-rendered frame at "+x+","+y);
            }
            assertions++;
        }
    }
    [STAThread] static int Main(string[] args) {
        try {
            if(args[0]=="legacy") {
                Assert(!IsProcessDPIAware(), "Control process unexpectedly has DPI awareness.");
                Assert(GetDpiForSystem()==96, "DPI-unaware control did not receive virtualized 96 DPI.");
                Console.WriteLine("PASS legacy control: DPI unaware, virtualized system DPI=96.");
                return 0;
            }
            Assert(IsProcessDPIAware(), "Shipped manifest failed to enable DPI awareness.");
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            string root=args[1], role=args[0];
            using(var frames=new DesktopIcons()) using(var form=new IconForm()) {
                IntPtr window=form.Handle;
                foreach(uint dpi in new uint[]{96,120,144,168,192,216,240,264,288,336,384}) {
                    foreach(bool small in new bool[]{true,false}) {
                        int size=(small?16:32)*(int)dpi/96;
                        string label=role+" "+dpi+" DPI "+(small?"small":"large");
                        string png=Path.Combine(root,"assets","generated",role+"-"+size+".png");
                        Assert(DesktopIcons.PixelSize(small,dpi)==size, label+" native metric differs.");
                        Compare(frames.Get(small,dpi),png,size,label+" direct");
                        foreach(int kind in small?new int[]{0,2}:new int[]{1}) {
                            IntPtr handle=SendMessage(window,0x007f,new IntPtr(kind),new IntPtr(dpi));
                            Assert(handle!=IntPtr.Zero,label+" WM_GETICON returned no icon.");
                            using(var icon=(Icon)Icon.FromHandle(handle).Clone())
                                Compare(icon,png,size,label+" WM_GETICON("+kind+")");
                        }
                    }
                }
                uint actual=DesktopIcons.WindowDpi(window);
                Assert(actual==GetDpiForSystem(),"Window and process system DPI disagree.");
                Console.WriteLine("PASS "+role+": "+assertions+" assertions; native small/large/SMALL2 frames match SVG PNGs from 100% to 400%, including 225%; system DPI="+actual+".");
            }
            return 0;
        } catch(Exception ex) { Console.Error.WriteLine(ex); return 1; }
    }
}
'@

try {
    $harnessFile = Join-Path $testRoot 'IconTests.cs'
    [IO.File]::WriteAllText($harnessFile, $harness, (New-Object Text.UTF8Encoding($false)))
    $references = @('System.Windows.Forms.dll', 'System.Drawing.dll', 'System.Web.Extensions.dll',
        'System.IO.Compression.dll', 'System.IO.Compression.FileSystem.dll', 'Microsoft.CSharp.dll', 'System.Management.dll')
    $sources = Get-ChildItem -LiteralPath (Join-Path $repoRoot 'deploy\desktop') -Filter '*.cs' -File |
        Sort-Object Name | ForEach-Object { $_.FullName }
    foreach ($role in @('legacy', 'center', 'worker')) {
        $executable = Join-Path $testRoot ($role + '.exe')
        $iconName = if ($role -eq 'legacy') { 'center' } else { $role }
        $compilerArgs = @('/nologo', '/target:exe', '/platform:x64', '/main:IconTests', ('/out:' + $executable),
            ('/resource:' + (Join-Path $repoRoot ('assets\' + $iconName + '.ico')) + ',AppIcon'))
        $compilerArgs += $references | ForEach-Object { '/reference:' + $_ }
        if ($role -ne 'legacy') {
            $compilerArgs += '/win32manifest:' + (Join-Path $repoRoot 'deploy\desktop\app.manifest')
        }
        $compilerArgs += $sources
        $compilerArgs += $harnessFile
        & $compiler @compilerArgs
        if ($LASTEXITCODE -ne 0) { throw "Icon test compilation failed: $role" }
        & $executable $role $repoRoot
        if ($LASTEXITCODE -ne 0) { throw "Icon test failed: $role" }
    }
}
finally {
    $resolvedTestRoot = [IO.Path]::GetFullPath($testRoot)
    if ([IO.Path]::GetDirectoryName($resolvedTestRoot) -ne $runtimeRoot -or
        -not [IO.Path]::GetFileName($resolvedTestRoot).StartsWith('icon-tests-')) {
        throw 'Refusing to clean an unexpected icon test directory.'
    }
    if (Test-Path -LiteralPath $resolvedTestRoot) {
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}
