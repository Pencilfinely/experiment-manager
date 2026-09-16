using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Management;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;

namespace ExperimentManagerDesktop {
    // Edge --app is a separate process: setting the launcher's Form.Icon does
    // not set its native window icons. Only touch our dedicated profile's
    // browser process, even when Edge transfers a launch to an existing PID.
    sealed class BrowserAppWindow : IDisposable {
        readonly string executable, profile;
        readonly object gate=new object();
        readonly Dictionary<int,Icon> icons=new Dictionary<int,Icon>();
        readonly Dictionary<IntPtr,WindowIdentity> windows=new Dictionary<IntPtr,WindowIdentity>();
        readonly Dictionary<int,long> knownProcesses=new Dictionary<int,long>();
        readonly System.Threading.Timer timer;
        DateTime nextDiscovery=DateTime.MinValue;
        bool disposed;
        sealed class WindowIdentity { internal int Pid; internal long Started; }

        internal BrowserAppWindow(string executablePath,string profilePath) {
            executable=CanonicalPath(executablePath); profile=CanonicalPath(profilePath);
            timer=new System.Threading.Timer(Tick,null,Timeout.Infinite,Timeout.Infinite);
        }

        internal void Start() { lock(gate) { if(!disposed){nextDiscovery=DateTime.MinValue;timer.Change(250,2000);} } }

        void Tick(object unused) {
            // Avoid overlapping WMI requests when a machine is busy.
            if(!Monitor.TryEnter(gate))return;
            try { if(!disposed)Refresh(); }
            catch(Exception ex) { Debug.WriteLine("Browser icon refresh: "+ex.Message); }
            finally { Monitor.Exit(gate); }
        }

        internal static string CanonicalPath(string path) {
            if(string.IsNullOrWhiteSpace(path))return "";
            try { return Path.IsPathRooted(path)?Path.GetFullPath(path).TrimEnd(Path.DirectorySeparatorChar,Path.AltDirectorySeparatorChar):""; }
            catch(ArgumentException) { return ""; }
            catch(NotSupportedException) { return ""; }
            catch(PathTooLongException) { return ""; }
        }

        internal static bool MatchesProfile(string commandLine,string expectedProfile) {
            if(string.IsNullOrWhiteSpace(commandLine)||string.IsNullOrEmpty(expectedProfile))return false;
            int count; IntPtr argv=CommandLineToArgvW(commandLine,out count);
            if(argv==IntPtr.Zero)return false;
            try {
                string found=null;
                for(int i=1;i<count;i++) {
                    string arg=Marshal.PtrToStringUni(Marshal.ReadIntPtr(argv,i*IntPtr.Size));
                    // Renderer/GPU/helper processes must never be selected.
                    if(arg=="--type"||arg.StartsWith("--type=",StringComparison.Ordinal))return false;
                    string value=null;
                    if(arg.StartsWith("--user-data-dir=",StringComparison.Ordinal))value=arg.Substring(16);
                    else if(arg=="--user-data-dir"&&i+1<count)value=Marshal.PtrToStringUni(Marshal.ReadIntPtr(argv,++i*IntPtr.Size));
                    if(value!=null) { if(found!=null)return false; found=CanonicalPath(value); }
                }
                return !string.IsNullOrEmpty(found)&&string.Equals(found,CanonicalPath(expectedProfile),StringComparison.OrdinalIgnoreCase);
            } finally { LocalFree(argv); }
        }

        // Kept synchronous for an isolated native smoke test; normal calls run
        // on the timer thread, never the WinForms message thread.
        internal int Refresh() {
            if(disposed||string.IsNullOrEmpty(executable)||string.IsNullOrEmpty(profile))return 0;
            var candidates=new Dictionary<int,Process>();
            try {
                foreach(var known in knownProcesses) {
                    Process process=null;
                    try {
                        process=Process.GetProcessById(known.Key);
                        if(process.HasExited||process.StartTime.ToUniversalTime().Ticks!=known.Value)continue;
                        IntPtr held=process.Handle;candidates.Add(known.Key,process);process=null;
                    } catch(ArgumentException) { }
                    catch(InvalidOperationException) { }
                    catch(System.ComponentModel.Win32Exception) { }
                    finally { if(process!=null)process.Dispose(); }
                }
                // Window/DPI checks stay frequent, but process command lines
                // only need rediscovery on launch or every 10 s.
                if(DateTime.UtcNow>=nextDiscovery) {
                nextDiscovery=DateTime.UtcNow.AddSeconds(10);
                var options=new EnumerationOptions { ReturnImmediately=true, Timeout=TimeSpan.FromSeconds(3) };
                using(var search=new ManagementObjectSearcher("root\\CIMV2",
                    "SELECT ProcessId,ExecutablePath,CommandLine,CreationDate FROM Win32_Process WHERE Name='msedge.exe'",options))
                using(var results=search.Get()) foreach(ManagementObject row in results) using(row) {
                    if(!string.Equals(CanonicalPath(Convert.ToString(row["ExecutablePath"])),executable,StringComparison.OrdinalIgnoreCase)||
                        !MatchesProfile(Convert.ToString(row["CommandLine"]),profile))continue;
                    Process process=null;
                    try {
                        int pid=Convert.ToInt32(row["ProcessId"]);if(candidates.ContainsKey(pid))continue;
                        process=Process.GetProcessById(pid);
                        long observed=ManagementDateTimeConverter.ToDateTime(Convert.ToString(row["CreationDate"])).ToUniversalTime().Ticks;
                        if(process.HasExited||Math.Abs(process.StartTime.ToUniversalTime().Ticks-observed)>=TimeSpan.TicksPerMillisecond)continue;
                        // Hold the process handle while enumerating its windows.
                        IntPtr held=process.Handle;candidates.Add(pid,process);process=null;
                    } catch(ArgumentException) { }
                    catch(InvalidOperationException) { }
                    catch(System.ComponentModel.Win32Exception) { }
                    finally { if(process!=null)process.Dispose(); }
                }
                }
                knownProcesses.Clear();
                foreach(var item in candidates)knownProcesses[item.Key]=item.Value.StartTime.ToUniversalTime().Ticks;
                int matched=0;
                EnumWindows((window,unused)=> {
                    uint pid;GetWindowThreadProcessId(window,out pid);
                    Process owner;
                    if(!candidates.TryGetValue((int)pid,out owner)||owner.HasExited||GetWindow(window,4)!=IntPtr.Zero)return true;
                    var className=new StringBuilder(128);GetClassNameW(window,className,className.Capacity);
                    if(className.ToString()!="Chrome_WidgetWin_1")return true;
                    ApplyIcons(window);
                    windows[window]=new WindowIdentity { Pid=(int)pid,Started=owner.StartTime.ToUniversalTime().Ticks };
                    matched++;return true;
                },IntPtr.Zero);
                var closed=new List<IntPtr>();
                foreach(var item in windows)if(!StillOwnsWindow(item.Key,item.Value))closed.Add(item.Key);
                foreach(var window in closed)windows.Remove(window);
                return matched;
            } finally { foreach(var process in candidates.Values)process.Dispose(); }
        }

        Icon IconAt(int size) { Icon icon;if(!icons.TryGetValue(size,out icon)){icon=App.LoadIcon(new Size(size,size));icons.Add(size,icon);}return icon; }

        void ApplyIcons(IntPtr window) {
            uint dpi=96;
            try { uint value=GetDpiForWindow(window);if(value>0)dpi=value; } catch(EntryPointNotFoundException) { }
            int small=Math.Max(16,(int)Math.Round(16*dpi/96.0));
            int large=Math.Max(32,(int)Math.Round(32*dpi/96.0));
            SetIcon(window,0,IconAt(small).Handle);SetIcon(window,1,IconAt(large).Handle);
        }

        static void SetIcon(IntPtr window,int kind,IntPtr icon) {
            IntPtr previous;
            if(SendMessageTimeoutW(window,0x7F,new IntPtr(kind),IntPtr.Zero,2,200,out previous)!=IntPtr.Zero&&previous==icon)return;
            SendMessageTimeoutW(window,0x80,new IntPtr(kind),icon,2,200,out previous);
        }

        static bool StillOwnsWindow(IntPtr window,WindowIdentity identity) {
            uint pid;GetWindowThreadProcessId(window,out pid);if(pid!=(uint)identity.Pid)return false;
            try { using(var process=Process.GetProcessById(identity.Pid))return !process.HasExited&&process.StartTime.ToUniversalTime().Ticks==identity.Started; }
            catch(ArgumentException) { return false; }
            catch(InvalidOperationException) { return false; }
            catch(System.ComponentModel.Win32Exception) { return false; }
        }

        internal void CloseOwnedWindows() {
            lock(gate) {
                if(disposed)return;
                nextDiscovery=DateTime.MinValue;Refresh();
                RequestCloseTrackedWindows();
            }
            var deadline=DateTime.UtcNow.AddSeconds(10);
            while(true) {
                bool remaining=false;
                lock(gate)foreach(var window in windows)if(StillOwnsWindow(window.Key,window.Value)){remaining=true;break;}
                if(!remaining)return;
                if(DateTime.UtcNow>=deadline)throw new InvalidOperationException("实验台窗口仍在关闭，请确认窗口中的提示后重试退出。");
                Thread.Sleep(100);
            }
        }

        void RequestCloseTrackedWindows() {
            // Close only windows discovered through the exact executable,
            // dedicated profile and process-start identity checks above.
            // Never terminate Edge or affect the user's ordinary profile.
            foreach(var window in windows)if(StillOwnsWindow(window.Key,window.Value))
                if(!PostMessageW(window.Key,0x10,IntPtr.Zero,IntPtr.Zero))
                    throw new InvalidOperationException("无法关闭实验台窗口，请关闭该窗口后重试退出。");
        }

        public void Dispose() {
            lock(gate) {
                if(disposed)return;disposed=true;timer.Dispose();
                // Do not leave windows holding icon handles owned by a client
                // that has exited. Never destroy icons belonging to Edge.
                foreach(var window in windows)if(StillOwnsWindow(window.Key,window.Value))for(int kind=0;kind<=1;kind++) {
                    IntPtr current;
                    if(SendMessageTimeoutW(window.Key,0x7F,new IntPtr(kind),IntPtr.Zero,2,200,out current)==IntPtr.Zero)continue;
                    foreach(var icon in icons.Values)if(current==icon.Handle){SetIcon(window.Key,kind,IntPtr.Zero);break;}
                }
                windows.Clear();knownProcesses.Clear();foreach(var icon in icons.Values)icon.Dispose();icons.Clear();
            }
        }

        delegate bool EnumWindowsProc(IntPtr window,IntPtr parameter);
        [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindowsProc callback,IntPtr parameter);
        [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window,out uint processId);
        [DllImport("user32.dll")] static extern IntPtr GetWindow(IntPtr window,uint command);
        [DllImport("user32.dll",CharSet=CharSet.Unicode)] static extern int GetClassNameW(IntPtr window,StringBuilder name,int maximum);
        [DllImport("user32.dll")] static extern uint GetDpiForWindow(IntPtr window);
        [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool PostMessageW(IntPtr window,uint message,IntPtr wParam,IntPtr lParam);
        [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr SendMessageTimeoutW(IntPtr window,uint message,IntPtr wParam,IntPtr lParam,uint flags,uint timeout,out IntPtr result);
        [DllImport("shell32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern IntPtr CommandLineToArgvW(string commandLine,out int argumentCount);
        [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr memory);
    }
}
