using System;
using System.Collections.Generic;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace ExperimentManagerDesktop {
    // Keep native-size frames alive for as long as Windows may use their handles.
    sealed class DesktopIcons : IDisposable {
        readonly Dictionary<int,Icon> frames = new Dictionary<int,Icon>();
        [DllImport("user32.dll")] static extern uint GetDpiForWindow(IntPtr window);
        [DllImport("user32.dll")] static extern int GetSystemMetricsForDpi(int metric, uint dpi);
        [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern IntPtr FindWindow(string name, string title);

        internal static uint WindowDpi(IntPtr window) {
            try { uint dpi=GetDpiForWindow(window); if(dpi>0)return dpi; }
            catch(EntryPointNotFoundException) { }
            using(var screen=Graphics.FromHwnd(IntPtr.Zero))return (uint)Math.Round(screen.DpiX);
        }
        internal static int PixelSize(bool small,uint dpi) {
            try { int size=GetSystemMetricsForDpi(small?49:11,dpi); if(size>0)return size; }
            catch(EntryPointNotFoundException) { }
            return Math.Max(1,(int)Math.Round((small?16:32)*dpi/96.0));
        }
        internal Icon Get(bool small,uint dpi) {
            int size=PixelSize(small,dpi);
            Icon icon;
            if(!frames.TryGetValue(size,out icon)) {
                icon=App.LoadIcon(new Size(size,size)); frames.Add(size,icon);
            }
            return icon;
        }
        internal Icon TrayIcon { get { return Get(true,WindowDpi(FindWindow("Shell_TrayWnd",null))); } }
        public void Dispose() { foreach(var icon in frames.Values)icon.Dispose(); frames.Clear(); }
    }

    class IconForm : Form {
        readonly DesktopIcons windowIcons=new DesktopIcons();
        internal IconForm() {
            SuspendLayout();
            AutoScaleMode=AutoScaleMode.Dpi;
            AutoScaleDimensions=new SizeF(96,96);
            Icon=windowIcons.Get(false,DesktopIcons.WindowDpi(IntPtr.Zero));
        }
        protected override void WndProc(ref Message message) {
            // Honor the DPI explicitly requested by the shell for the caption,
            // taskbar and Alt+Tab instead of always using the process DPI.
            if(message.Msg==0x007f) {
                long kind=message.WParam.ToInt64();
                if(kind==0||kind==1||kind==2) {
                    long requested=message.LParam.ToInt64();
                    uint dpi=requested>=48&&requested<=768?(uint)requested:DesktopIcons.WindowDpi(Handle);
                    message.Result=windowIcons.Get(kind!=1,dpi).Handle; return;
                }
            }
            base.WndProc(ref message);
        }
        protected override void Dispose(bool disposing) {
            base.Dispose(disposing);
            if(disposing)windowIcons.Dispose();
        }
    }
}
