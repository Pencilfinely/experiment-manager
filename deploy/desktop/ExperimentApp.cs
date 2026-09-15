using System;
using System.Collections.Generic;
using System.Collections;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

namespace ExperimentManagerDesktop {
    static class App {
        internal static JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 8 * 1024 * 1024 };
        internal static string Package = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
        internal static bool Worker;
        internal static string Version {
            get { using(var stream=Assembly.GetExecutingAssembly().GetManifestResourceStream("AppVersion")) {
                if(stream==null) throw new InvalidOperationException("Application version resource is missing");
                using(var reader=new StreamReader(stream))return reader.ReadToEnd().Trim();
            } }
        }
        internal static string Role { get { return Worker ? "Worker" : "Controller"; } }
        internal static string Title { get { return Worker ? "实验算力 · Experiment Worker" : "实验台 · Experiment Center"; } }
        internal static string Executable { get { return Worker ? "ExperimentWorker.exe" : "ExperimentCenter.exe"; } }
        internal static string SettingsRoot { get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "ExperimentManager", "desktop", Role); } }
        internal static string SettingsFile { get { return Path.Combine(SettingsRoot, "settings.json"); } }
        internal static string InstanceKey {get{return "Local\\ExperimentManager-"+Role+"-"+(Environment.UserDomainName+"-"+Environment.UserName).Replace('\\','-');}}
        internal static string Arg(string[] args, string key) { for (int i=0; i<args.Length-1; i++) if(args[i]==key) return args[i+1]; return null; }
        internal static bool Has(string[] args, string key) { return Array.IndexOf(args, key)>=0; }
        internal static string Text(Dictionary<string,object> obj, string key, string fallback="") { return obj.ContainsKey(key) && obj[key]!=null ? Convert.ToString(obj[key]) : fallback; }
        internal static bool Flag(Dictionary<string,object> obj, string key) { return obj.ContainsKey(key) && obj[key] is bool && (bool)obj[key]; }
        internal static Dictionary<string,object> Read(string path) {
            if(!File.Exists(path)) return new Dictionary<string,object>();
            return Json.Deserialize<Dictionary<string,object>>(File.ReadAllText(path, Encoding.UTF8));
        }
        internal static void Write(string path, object value) {
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            string tmp=path+"."+Guid.NewGuid().ToString("N")+".tmp";
            File.WriteAllText(tmp, Json.Serialize(value), new UTF8Encoding(false));
            if(File.Exists(path)) File.Replace(tmp,path,null); else File.Move(tmp,path);
        }
        internal static string Quote(string value) {
            // WSL parses leading options itself: leave simple flags unquoted.
            if(value.Length>0&&value.IndexOfAny(new[]{' ','\t','\r','\n','"'})<0)return value;
            StringBuilder b=new StringBuilder("\""); int slashes=0;
            foreach(char c in value) { if(c=='\\') { slashes++; continue; } if(c=='"') { b.Append('\\',slashes*2+1); b.Append(c); } else { b.Append('\\',slashes); b.Append(c); } slashes=0; }
            b.Append('\\',slashes*2); return b.Append('"').ToString();
        }
        internal static string Arguments(params string[] args) { return string.Join(" ",Array.ConvertAll(args,Quote)); }
        internal static Icon LoadIcon(Size size) {
            using(var resource=Assembly.GetExecutingAssembly().GetManifestResourceStream("AppIcon")) {
                if(resource==null) throw new InvalidOperationException("Application icon resource is missing");
                // Select a frame from the multi-size ICO, then detach it from the
                // resource stream. Each caller owns and disposes its returned icon.
                using(var icon=new Icon(resource,size)) return (Icon)icon.Clone();
            }
        }
        internal static string Run(string executable, params string[] args) {
            var info=new ProcessStartInfo(executable,Arguments(args)) { WorkingDirectory=Package, UseShellExecute=false, CreateNoWindow=true,
                RedirectStandardOutput=true, RedirectStandardError=true, StandardOutputEncoding=Encoding.UTF8, StandardErrorEncoding=Encoding.UTF8 };
            if(Path.GetFileName(executable).Equals("wsl.exe",StringComparison.OrdinalIgnoreCase)&&Array.IndexOf(args,"--list")>=0) info.StandardOutputEncoding=Encoding.Unicode;
            info.EnvironmentVariables["PYTHONUTF8"]="1"; info.EnvironmentVariables["PYTHONIOENCODING"]="utf-8";
            StringBuilder output=new StringBuilder(),error=new StringBuilder();
            using(var p=new Process { StartInfo=info }) {
                p.OutputDataReceived += (s,e)=>{ if(e.Data!=null) lock(output) output.AppendLine(e.Data); };
                p.ErrorDataReceived += (s,e)=>{ if(e.Data!=null) lock(error) error.AppendLine(e.Data); };
                p.Start(); p.BeginOutputReadLine(); p.BeginErrorReadLine();
                if(!p.WaitForExit(120000)) throw new Exception("操作仍在执行，请稍后查看状态或日志。后台安装可能需要下载依赖。");
                p.WaitForExit();
                string value=output.ToString().Replace("\0", "").Trim();
                if(p.ExitCode!=0 && !value.StartsWith("{")) throw new Exception((value+"\n"+error.ToString()).Trim());
                return value;
            }
        }
        internal static Dictionary<string,object> Command(string executable, params string[] args) {
            string text=Run(executable,args);
            var value=Json.Deserialize<Dictionary<string,object>>(text);
            if(Text(value,"status")=="error") throw new Exception(Text(value,"detail",text));
            return value;
        }
        internal static void OpenFile(string path) { if(File.Exists(path)||Directory.Exists(path)) Process.Start(new ProcessStartInfo(path){UseShellExecute=true}); }
        internal static void Shortcut(string destination, string target, string arguments) {
            Directory.CreateDirectory(Path.GetDirectoryName(destination));
            Type type=Type.GetTypeFromProgID("WScript.Shell"); dynamic shell=Activator.CreateInstance(type);
            dynamic link=shell.CreateShortcut(destination); link.TargetPath=target; link.Arguments=arguments;
            link.WorkingDirectory=Path.GetDirectoryName(target); link.Description=Title; link.IconLocation=target+",0"; link.Save();
        }
        internal static void WaitForPreviousClient(string[] args) {
            string raw=Arg(args,"--wait-pid");if(raw==null)return;
            int pid;long started;
            if(!int.TryParse(raw,out pid)||pid<=0||pid==Process.GetCurrentProcess().Id||!long.TryParse(Arg(args,"--wait-start"),out started))
                throw new Exception("更新交接参数无效，请重新打开安装程序。");
            try { using(var previous=Process.GetProcessById(pid)) {
                if(previous.StartTime.ToUniversalTime().Ticks==started&&!previous.WaitForExit(60000))
                    throw new Exception("旧客户端尚未退出，请从托盘退出后重新打开安装程序。");
            } } catch(ArgumentException) { /* The old client has already exited. */ }
        }
        [STAThread] static void Main(string[] args) {
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
            try {
                using(Stream role=Assembly.GetExecutingAssembly().GetManifestResourceStream("Role")) {
                    if(role!=null) using(var reader=new StreamReader(role)) Worker=reader.ReadToEnd().Trim()=="worker";
                    else Worker=File.ReadAllText(Path.Combine(Package,"release-role.json")).Contains("worker");
                }
                if(Has(args,"--self-test")) {
                    // .NET Framework selects at most 128 px from this multi-size
                    // ICO; the 256 px frame remains available to Windows Explorer.
                    int[] runtimeIconSizes={16,20,24,28,32,36,40,44,48,56,64,72,80,88,96,112,128};
                    foreach(int size in runtimeIconSizes) using(var icon=LoadIcon(new Size(size,size))) using(var bitmap=icon.ToBitmap()) {
                        if(bitmap.Width!=size||bitmap.Height!=size) throw new Exception("Application icon frame is missing: "+size);
                    }
                    string iconHash;
                    using(var resource=Assembly.GetExecutingAssembly().GetManifestResourceStream("AppIcon")) using(var hash=SHA256.Create())
                        iconHash=BitConverter.ToString(hash.ComputeHash(resource)).Replace("-","").ToLowerInvariant();
                    int payloadFiles=0;
                    using(var payload=Assembly.GetExecutingAssembly().GetManifestResourceStream("AppPayload")) if(payload!=null) using(var zip=new ZipArchive(payload,ZipArchiveMode.Read)) {
                        if(zip.GetEntry(Executable)==null||zip.GetEntry("release-role.json")==null) throw new Exception("Incomplete application payload");
                        foreach(var item in zip.Entries) { using(var input=item.Open()) {byte[] buffer=new byte[65536];while(input.Read(buffer,0,buffer.Length)>0){} }payloadFiles++; }
                    }
                    File.WriteAllText(Arg(args,"--report")??Path.Combine(Path.GetTempPath(),"expman-desktop-test.json"), Json.Serialize(new { role=Role, version=Version, executable=Executable, status="passed",payload_files=payloadFiles,icon_sha256=iconHash,runtime_icon_sizes=runtimeIconSizes })); return;
                }
                using(var payload=Assembly.GetExecutingAssembly().GetManifestResourceStream("AppPayload")) if(payload!=null) {
                    WaitForPreviousClient(args);
                    if(Has(args,"--install")) {
                        string dataRoot=Arg(args,"--data-root")??Text(Read(SettingsFile),"data_root",Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"ExperimentManager","controller"));
                        bool startup=Has(args,"--startup");
                        using(var run=Registry.CurrentUser.OpenSubKey("Software\\Microsoft\\Windows\\CurrentVersion\\Run")) startup=startup||(run!=null&&run.GetValue("ExperimentManager-"+Role)!=null);
                        string installed=InstallerForm.Install(dataRoot,Arg(args,"--pairing"),Has(args,"--desktop"),startup);
                        Write(Arg(args,"--report")??Path.Combine(SettingsRoot,"install-report.json"),new {status="installed",role=Role,path=installed});
                    } else using(var form=new InstallerForm(args)) Application.Run(form);
                    return;
                }
                string key=InstanceKey; bool created;
                using(var mutex=new Mutex(true,key,out created)) using(var show=new EventWaitHandle(false,EventResetMode.AutoReset,key+"-Open")) {
                    if(!created) { show.Set(); return; }
                    using(var form=new ClientForm(args)) {
                        var handle=form.Handle;
                        var monitor=new Thread(()=> { while(show.WaitOne()) { if(form.IsDisposed) break; try { form.BeginInvoke((Action)(()=>form.OpenFromTray())); } catch(InvalidOperationException) { break; } } });
                        monitor.IsBackground=true; monitor.Start(); Application.Run(form); show.Set();
                    }
                }
            } catch(Exception ex) { if(Has(args,"--self-test")||Has(args,"--install")) {Write(Arg(args,"--report")??Path.Combine(Path.GetTempPath(),"expman-desktop-error.json"),new{status="failed",detail=ex.Message});Environment.ExitCode=1;}else MessageBox.Show(ex.Message,"Experiment Manager",MessageBoxButtons.OK,MessageBoxIcon.Error); }
        }
    }

    sealed class InstallerForm : IconForm {
        bool installing;
        Button install=new Button { Text="安装并启动 / Install", AutoSize=true };
        CheckBox desktop=new CheckBox { Text="创建桌面快捷方式", Checked=true, AutoSize=true };
        CheckBox startup=new CheckBox { Text="登录 Windows 后后台启动", Checked=false, AutoSize=true };
        TextBox data=new TextBox { Dock=DockStyle.Fill };
        TextBox pairing=new TextBox { Dock=DockStyle.Fill,ReadOnly=true };
        Label status=new Label { AutoSize=true,MaximumSize=new Size(530,0) };
        internal InstallerForm(string[] args) {
            Text="安装 "+App.Title; Size=new Size(610,460); MinimumSize=Size; StartPosition=FormStartPosition.CenterScreen;
            using(var run=Registry.CurrentUser.OpenSubKey("Software\\Microsoft\\Windows\\CurrentVersion\\Run")) startup.Checked=run!=null&&run.GetValue("ExperimentManager-"+App.Role)!=null;
            var saved=App.Read(App.SettingsFile);
            if(App.Has(args,"--apply-update"))desktop.Checked=saved.ContainsKey("desktop_shortcut")?App.Flag(saved,"desktop_shortcut"):
                File.Exists(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),App.Worker?"实验算力.lnk":"实验台.lnk"));
            if(App.Worker)pairing.Text=App.Text(saved,"pairing_file");
            Font=new Font("Microsoft YaHei UI",10); BackColor=Color.White;
            var panel=new TableLayoutPanel { Dock=DockStyle.Fill,Padding=new Padding(24),ColumnCount=1,RowCount=8 };
            panel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
            panel.Controls.Add(new Label { Text=App.Title,Font=new Font(Font.FontFamily,19,FontStyle.Bold),AutoSize=true });
            panel.Controls.Add(new Label { Text="安装到当前用户，不需要管理员权限。运行环境和实验数据分别保存。",AutoSize=true,MaximumSize=new Size(530,0) });
            panel.Controls.Add(new Label { Text=App.Worker?"导入主控提供的配对凭证（已有节点可直接复用）":"主控数据目录（升级时可选择原实验台目录）",AutoSize=true });
            var row=new TableLayoutPanel { Dock=DockStyle.Top,ColumnCount=2,AutoSize=true };
            row.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100)); row.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            var browse=new Button { Text="选择…", AutoSize=true };
            if(App.Worker) { row.Controls.Add(pairing,0,0); browse.Click+=(s,e)=>{ using(var f=new OpenFileDialog { Filter="节点凭证 (*.json)|*.json" }) if(f.ShowDialog()==DialogResult.OK) pairing.Text=f.FileName; }; }
            else { data.Text=App.Arg(args,"--data-root")??App.Text(App.Read(App.SettingsFile),"data_root",Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"ExperimentManager","controller")); row.Controls.Add(data,0,0); browse.Click+=(s,e)=>{ using(var f=new FolderBrowserDialog { Description="选择实验台数据目录",SelectedPath=data.Text }) if(f.ShowDialog()==DialogResult.OK) data.Text=f.SelectedPath; }; }
            row.Controls.Add(browse,1,0); panel.Controls.Add(row); panel.Controls.Add(desktop); panel.Controls.Add(startup); panel.Controls.Add(status); panel.Controls.Add(install); Controls.Add(panel);
            install.Click+=async (s,e)=> {
                if(installing)return;installing=true;install.Enabled=false; status.Text="正在安装…";
                try {
                    string dataPath=data.Text,credential=pairing.Text; bool makeDesktop=desktop.Checked,autoStart=startup.Checked;
                    string destination=await Task.Run(()=>Install(dataPath,credential,makeDesktop,autoStart));
                    Process.Start(new ProcessStartInfo(Path.Combine(destination,App.Executable),App.Worker&&App.Has(args,"--resume-service")?"--background":""){WorkingDirectory=destination,UseShellExecute=true}); installing=false;Close();
                } catch(Exception ex) { status.Text=ex.Message; installing=false;install.Enabled=true; }
            };
            FormClosing+=(s,e)=>{if(installing){e.Cancel=true;status.Text="正在安装，请等待完成后再关闭。";}};
            if(App.Has(args,"--apply-update"))Shown+=(s,e)=>install.PerformClick();
            ResumeLayout(true);
        }
        internal static string Install(string dataPath,string credential,bool makeDesktop,bool autoStart) {
            Mutex active;
            if(Mutex.TryOpenExisting(App.InstanceKey,out active)) {active.Dispose();throw new Exception("此客户端已经运行。升级前请在原客户端中停止主控或代理，再从托盘菜单退出客户端，然后点击安装。已有 Docker 实验不会因此停止。");}
            using(var payload=Assembly.GetExecutingAssembly().GetManifestResourceStream("AppPayload")) using(var zip=new ZipArchive(payload,ZipArchiveMode.Read)) {
                var role=zip.GetEntry("release-role.json"); string version;
                using(var reader=new StreamReader(role.Open())) version=App.Text(App.Json.Deserialize<Dictionary<string,object>>(reader.ReadToEnd()),"version");
                foreach(char c in version) if(!(char.IsLetterOrDigit(c)||c=='.'||c=='-')) throw new Exception("安装包版本无效");
                if(string.IsNullOrEmpty(version)) throw new Exception("安装包版本无效");
                string destination=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Programs","ExperimentManager",App.Role,version);
                string ancestor=destination;
                while(ancestor!=null) {if(Directory.Exists(ancestor)&&(File.GetAttributes(ancestor)&FileAttributes.ReparsePoint)!=0)throw new Exception("安装目录不能包含目录链接");ancestor=Path.GetDirectoryName(ancestor);}
                Directory.CreateDirectory(destination);
                string prefix=Path.GetFullPath(destination)+Path.DirectorySeparatorChar;
                foreach(var entry in zip.Entries) {
                    if(string.IsNullOrEmpty(entry.Name)) continue;
                    string path=Path.GetFullPath(Path.Combine(destination,entry.FullName));
                    if(!path.StartsWith(prefix,StringComparison.OrdinalIgnoreCase)||entry.FullName.Contains(":")) throw new Exception("安装包路径不安全");
                    string current=Path.GetDirectoryName(path);
                    while(current!=null && current.StartsWith(destination,StringComparison.OrdinalIgnoreCase)) {
                        if(Directory.Exists(current)&&(File.GetAttributes(current)&FileAttributes.ReparsePoint)!=0) throw new Exception("安装目录不能包含目录链接");
                        current=Path.GetDirectoryName(current);
                    }
                    Directory.CreateDirectory(Path.GetDirectoryName(path));
                    using(var input=entry.Open()) using(var memory=new MemoryStream()) {
                        input.CopyTo(memory); byte[] bytes=memory.ToArray();
                        if(File.Exists(path)) { if((File.GetAttributes(path)&FileAttributes.ReparsePoint)!=0)throw new Exception("安装文件不能是链接");using(var hash=SHA256.Create()) if(Convert.ToBase64String(hash.ComputeHash(File.ReadAllBytes(path)))!=Convert.ToBase64String(hash.ComputeHash(bytes))) throw new Exception("同版本安装文件已被修改，请选择新的版本安装包："+entry.Name); }
                        else File.WriteAllBytes(path,bytes);
                    }
                }
                var settings=App.Read(App.SettingsFile);
                if(!App.Worker) settings["data_root"]=Path.GetFullPath(dataPath);
                if(App.Worker&&!string.IsNullOrWhiteSpace(credential)) settings["pairing_file"]=Path.GetFullPath(credential);
                settings["installed_version"]=version;settings["desktop_shortcut"]=makeDesktop; App.Write(App.SettingsFile,settings);
                string target=Path.Combine(destination,App.Executable);
                string menu=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.StartMenu),"Programs","Experiment Manager",App.Role+".lnk");
                App.Shortcut(menu,target,"");
                if(makeDesktop) App.Shortcut(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),App.Worker?"实验算力.lnk":"实验台.lnk"),target,"");
                using(var run=Registry.CurrentUser.OpenSubKey("Software\\Microsoft\\Windows\\CurrentVersion\\Run",true)) {
                    if(autoStart) run.SetValue("ExperimentManager-"+App.Role,App.Quote(target)+" --background");
                    else run.DeleteValue("ExperimentManager-"+App.Role,false);
                }
                return destination;
            }
        }
    }

    sealed class ClientForm : IconForm {
        readonly DesktopIcons trayIcons=new DesktopIcons();
        NotifyIcon tray; bool exiting,busy,background; Dictionary<string,object> settings; string lastLog=""; Process workerHold; string heldDistribution="";
        UpdateForm updateDialog;
        BrowserAppWindow browserWindowIcons;
        bool resumeWorkerAfterUpdate;
        Label summary=new Label { AutoSize=true, MaximumSize=new Size(810,0), Text="正在检查状态…" };
        TextBox log=new TextBox { Multiline=true,ReadOnly=true,ScrollBars=ScrollBars.Both,Dock=DockStyle.Fill,WordWrap=false,Font=new Font("Consolas",9) };
        TextBox data=new TextBox { Dock=DockStyle.Fill,ReadOnly=true };
        TextBox pairing=new TextBox { Dock=DockStyle.Fill,ReadOnly=true };
        ComboBox distro=new ComboBox { Dock=DockStyle.Fill,DropDownStyle=ComboBoxStyle.DropDownList };
        ComboBox existing=new ComboBox { Dock=DockStyle.Fill,DropDownStyle=ComboBoxStyle.DropDownList };
        List<string> configs=new List<string>();
        System.Windows.Forms.Timer timer=new System.Windows.Forms.Timer { Interval=5000 };
        internal ClientForm(string[] args) {
            settings=App.Read(App.SettingsFile); background=App.Has(args,"--background");
            string supplied=App.Arg(args,"--data-root"); if(supplied!=null) { settings["data_root"]=Path.GetFullPath(supplied); App.Write(App.SettingsFile,settings); }
            Text=App.Title; Size=new Size(900,650); MinimumSize=new Size(700,510); StartPosition=FormStartPosition.CenterScreen;
            Font=new Font("Microsoft YaHei UI",10); BackColor=Color.White;
            var layout=new TableLayoutPanel { Dock=DockStyle.Fill,Padding=new Padding(24),ColumnCount=1,RowCount=7 };
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.Controls.Add(new Label { Text=App.Title,Font=new Font(Font.FontFamily,21,FontStyle.Bold),AutoSize=true });
            layout.Controls.Add(summary);
            var controls=new TableLayoutPanel { Dock=DockStyle.Top,AutoSize=true,ColumnCount=3 };
            controls.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize)); controls.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100)); controls.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            if(App.Worker) {
                controls.Controls.Add(new Label {Text="Ubuntu",AutoSize=true},0,0); controls.Controls.Add(distro,1,0);
                controls.Controls.Add(new Label {Text="节点凭证",AutoSize=true},0,1); controls.Controls.Add(pairing,1,1);
                pairing.Text=App.Text(settings,"pairing_file"); var choose=new Button {Text="导入凭证…",AutoSize=true};
                choose.Click+=(s,e)=>{ using(var picker=new OpenFileDialog {Filter="主控配对凭证 (*.json)|*.json"}) if(picker.ShowDialog()==DialogResult.OK) { pairing.Text=picker.FileName; settings["pairing_file"]=pairing.Text; App.Write(App.SettingsFile,settings); } }; controls.Controls.Add(choose,2,1);
                controls.Controls.Add(new Label {Text="已有配置",AutoSize=true},0,2); controls.Controls.Add(existing,1,2);
                configs.Add(""); existing.Items.Add("自动选择 / 新节点使用凭证"); existing.SelectedIndex=0;
                distro.SelectedIndexChanged+=(s,e)=>{settings["distribution"]=Convert.ToString(distro.SelectedItem);App.Write(App.SettingsFile,settings);};
            } else {
                data.Text=App.Text(settings,"data_root",Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"ExperimentManager","controller"));
                controls.Controls.Add(new Label {Text="数据目录",AutoSize=true},0,0); controls.Controls.Add(data,1,0);
                var choose=new Button {Text="选择目录…",AutoSize=true}; choose.Click+=(s,e)=>{using(var picker=new FolderBrowserDialog {SelectedPath=data.Text,Description="选择已有或新建的主控数据目录"}) if(picker.ShowDialog()==DialogResult.OK) {data.Text=picker.SelectedPath;settings["data_root"]=data.Text;App.Write(App.SettingsFile,settings);RefreshState();}};controls.Controls.Add(choose,2,0);
            }
            layout.Controls.Add(controls);
            var actions=new FlowLayoutPanel {Dock=DockStyle.Top,AutoSize=true};
            AddButton(actions,App.Worker?"启动后台代理":"打开实验台",()=> { if(App.Worker) StartWorker(); else OpenController(); });
            AddButton(actions,App.Worker?"停止代理":"停止主控",()=>Stop());
            AddButton(actions,"刷新状态",()=>RefreshState()); AddButton(actions,"打开日志",()=>App.OpenFile(lastLog));
            AddButton(actions,"检查更新 · "+App.Version,()=>CheckUpdates());
            AddButton(actions,"转入后台",()=>Hide()); layout.Controls.Add(actions);
            layout.Controls.Add(new Label {AutoSize=true,MaximumSize=new Size(810,0),Text=App.Worker?"首次导入凭证后自动检查 GPU 和准备环境。关闭此窗口会保留后台运行；停止代理不会终止已有 Docker 训练容器。":"实验、算力和算法项目在同一应用窗口中管理。关闭实验台窗口后，主控继续后台运行。"});
            layout.Controls.Add(log); layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));layout.RowStyles.Add(new RowStyle(SizeType.Percent,100));
            Controls.Add(layout);
            tray=new NotifyIcon { Icon=trayIcons.TrayIcon,Text=App.Title,Visible=true };
            var menu=new ContextMenuStrip(); menu.Items.Add(App.Worker?"打开算力客户端":"打开实验台",null,(s,e)=>OpenFromTray());
            menu.Items.Add("检查更新…",null,(s,e)=>CheckUpdates());
            menu.Items.Add("状态与日志",null,(s,e)=>ShowStatus()); menu.Items.Add("退出客户端（后台继续运行）",null,(s,e)=>{exiting=true;Close();}); tray.ContextMenuStrip=menu;tray.DoubleClick+=(s,e)=>OpenFromTray();
            FormClosing+=(s,e)=>{if(!exiting){e.Cancel=true;Hide();}else{timer.Stop();tray.Visible=false;}};
            timer.Tick+=(s,e)=>{tray.Icon=trayIcons.TrayIcon;RefreshState();};
            Shown+=async (s,e)=> {
                if(App.Worker) {
                    await Execute(async()=>{
                        string inventory=await Task.Run(()=>App.Run("wsl.exe","--list","--quiet"));
                        foreach(string name in inventory.Split(new[]{'\n','\r'},StringSplitOptions.RemoveEmptyEntries)) if(!name.Trim().StartsWith("docker-desktop")) distro.Items.Add(name.Trim());
                        if(distro.Items.Count==0) throw new Exception("请先安装 WSL2 和 Ubuntu，并在 Ubuntu 中创建普通用户。");
                        string chosen=App.Text(settings,"distribution"); distro.SelectedItem=chosen; if(distro.SelectedIndex<0) distro.SelectedIndex=0;
                    });
                    if(background) { StartWorker(); Hide(); } else RefreshState();
                } else if(background) { await Execute(async()=>{await Controller("controller-start");Hide();}); } else OpenController();
                timer.Start();
            };
            ResumeLayout(true);
        }
        protected override void Dispose(bool disposing) {
            if(disposing) {
                timer.Dispose();
                if(browserWindowIcons!=null)browserWindowIcons.Dispose();
                if(tray!=null) { tray.Visible=false; tray.Dispose(); tray=null; }
            }
            base.Dispose(disposing);
            if(disposing) trayIcons.Dispose();
        }
        static void AddButton(Control parent,string text,Action action) {var button=new Button {Text=text,AutoSize=true,Margin=new Padding(0,8,12,8)};button.Click+=(s,e)=>action();parent.Controls.Add(button);}
        async Task Execute(Func<Task> action) { if(busy)return;busy=true;try {await action();}catch(Exception ex){summary.Text=ex.Message;try{App.Write(Path.Combine(App.SettingsRoot,"last-error.json"),new{time=DateTime.UtcNow.ToString("o"),detail=ex.Message});}catch(IOException){}}finally{busy=false;} }
        internal void ShowStatus(){Show();WindowState=FormWindowState.Normal;Activate();RefreshState();}
        void CheckUpdates(){
            if(updateDialog!=null){updateDialog.Activate();return;}
            if(busy){MessageBox.Show("当前操作完成后再检查更新。",App.Title);return;}
            try {using(var dialog=new UpdateForm(this)){updateDialog=dialog;dialog.ShowDialog(this);}}
            finally {updateDialog=null;}
        }
        internal async Task InstallUpdate(UpdateRelease release,string installer) {
            if(busy)throw new Exception("当前操作尚未完成，请稍后安装。");
            busy=true;timer.Stop();
            bool wasRunning=false,stopRequested=false;
            Exception failure=null;
            try {
                await Task.Run(()=>UpdateService.ValidateDownloaded(release,installer));
                string report=Path.Combine(App.SettingsRoot,"updates","verify-"+Guid.NewGuid().ToString("N")+".json");
                try {
                    await Task.Run(()=>App.Run(installer,"--self-test","--report",report));
                    var result=App.Read(report);
                    if(App.Text(result,"status")!="passed"||App.Text(result,"role")!=App.Role||App.Text(result,"version")!=release.Version||
                        !result.ContainsKey("payload_files")||Convert.ToInt32(result["payload_files"])<=0)
                        throw new Exception("安装包版本或角色不匹配，请重新下载。");
                } finally {if(File.Exists(report))File.Delete(report);}
                var ready=App.Worker?await WorkerCommand("update-status"):await Controller("controller-update-status");
                if(!App.Flag(ready,"ready_for_update"))throw new Exception(App.Text(ready,"detail","实验或文件回传尚未完成。"));
                wasRunning=App.Flag(ready,"running");
                resumeWorkerAfterUpdate=resumeWorkerAfterUpdate||(App.Worker&&wasRunning);
                stopRequested=true;
                var stopped=App.Worker?await WorkerCommand("stop-for-update"):await Controller("controller-stop-for-update");
                if(!App.Flag(stopped,"ready_for_update"))throw new Exception(App.Text(stopped,"detail","暂时无法安全停止服务。"));
                bool running=true;
                for(int attempt=0;attempt<40;attempt++) {
                    var state=App.Worker?await WorkerCommand("status"):await Controller("controller-status");
                    running=App.Flag(state,"running");if(!running)break;
                    await Task.Delay(250);
                }
                if(running)throw new Exception("服务仍在退出，请稍后再次安装。");
                ready=App.Worker?await WorkerCommand("update-status"):await Controller("controller-update-status");
                if(!App.Flag(ready,"ready_for_update"))throw new Exception(App.Text(ready,"detail","停止后检查未通过。"));
                await Task.Run(()=>UpdateService.ValidateDownloaded(release,installer));
                using(var self=Process.GetCurrentProcess()) {
                    var arguments=new List<string>{"--apply-update","--wait-pid",self.Id.ToString(),"--wait-start",self.StartTime.ToUniversalTime().Ticks.ToString()};
                    if(resumeWorkerAfterUpdate)arguments.Add("--resume-service");
                    var next=Process.Start(new ProcessStartInfo(installer,App.Arguments(arguments.ToArray())){UseShellExecute=false,CreateNoWindow=true});
                    if(next==null)throw new Exception("无法启动更新安装程序。");
                    next.Dispose();
                }
            } catch(Exception ex) {failure=ex;}
            if(failure!=null) {
                string recovery="";
                if(stopRequested&&wasRunning) {
                    try {
                        var current=App.Worker?await WorkerCommand("status"):await Controller("controller-status");
                        if(!App.Flag(current,"running")) {
                            var restored=App.Worker?await WorkerCommand("start"):await Controller("controller-start");
                            if(!App.Flag(restored,"running")&&App.Text(restored,"status")!="starting")
                                throw new Exception(App.Text(restored,"detail","服务未能启动。"));
                            recovery=" 原服务已重新启动。";
                        }
                    } catch(Exception restoreError) {recovery=" 原服务恢复失败，请在客户端重新启动："+restoreError.Message;}
                }
                busy=false;timer.Start();throw new Exception(failure.Message+recovery,failure);
            }
        }
        internal void FinishUpdateExit(){exiting=true;Close();}
        internal void OpenFromTray(){if(App.Worker)ShowStatus();else OpenController();}
        async Task<Dictionary<string,object>> Controller(string action) {
            string root=data.Text; var value=await Task.Run(()=>App.Command(Path.Combine(App.Package,"runtime","python.exe"),"-m","expman.desktop",action,"--root",root));
            Display(value);return value;
        }
        string SelectedDistribution {get{if(distro.SelectedItem==null)throw new Exception("选择原节点使用的 Ubuntu。");return Convert.ToString(distro.SelectedItem);}}
        async Task<Dictionary<string,object>> WorkerCommand(string action) {
            string distribution=SelectedDistribution,credential=pairing.Text;
            string selected=existing.SelectedIndex>0?configs[existing.SelectedIndex]:"";
            return await Task.Run(()=> {
                string package=App.Run("wsl.exe","-d",distribution,"--exec","wslpath","-a",App.Package).Trim();
                var argv=new List<string>{"-d",distribution,"--exec","bash",package+"/Client-Worker.sh",action};
                if(action=="start"||action=="install") {
                    if(action=="install") {argv.Add("--backend");argv.Add("detached");}
                    if(!string.IsNullOrEmpty(selected)) {argv.Add("--config");argv.Add(selected);}
                    else if(!string.IsNullOrEmpty(credential)&&File.Exists(credential)) {argv.Add("--pairing");argv.Add(App.Run("wsl.exe","-d",distribution,"--exec","wslpath","-a",credential).Trim());}
                }
                var result=App.Command("wsl.exe",argv.ToArray());
                if(action=="update-status")return result;
                if(action!="stop"&&action!="stop-for-update"&&(App.Flag(result,"running")||App.Text(result,"status")=="starting"||App.Text(result,"status")=="preparing")) {
                    if(heldDistribution!=distribution) {
                        if(workerHold!=null)workerHold.Dispose();
                        workerHold=Process.Start(new ProcessStartInfo("wsl.exe",App.Arguments("-d",distribution,"--exec","bash",package+"/Client-Worker.sh","_hold")){UseShellExecute=false,CreateNoWindow=true});
                        heldDistribution=distribution;
                    }
                } else heldDistribution="";
                return result;
            });
        }
        async void RefreshState(){await Execute(async()=>{var result=App.Worker?await WorkerCommand("status"):await Controller("controller-status");Display(result);if(App.Worker){var logs=await WorkerCommand("logs");object lines;if(logs.TryGetValue("lines",out lines)&&lines is IList){var shown=new List<string>();foreach(var line in (IList)lines)shown.Add(Convert.ToString(line));log.Lines=shown.ToArray();}}});}
        async void StartWorker(){await Execute(async()=>{summary.Text="正在安装或复用算力代理…";Display(await WorkerCommand("install"));});}
        async void Stop(){await Execute(async()=>{Display(App.Worker?await WorkerCommand("stop"):await Controller("controller-stop"));});}
        async void OpenController(){await Execute(async()=>{var result=await Controller("controller-open");string url=App.Text(result,"url");if(!string.IsNullOrEmpty(url)){OpenAppWindow(url);Hide();}});}
        void Display(Dictionary<string,object> value) {
            summary.Text=App.Text(value,"node_id",App.Worker?"算力客户端":"实验台")+" · "+App.Text(value,"status")+"\n"+App.Text(value,"detail");
            lastLog=App.Text(value,"log_path");
            if(App.Worker) {
                object raw;if(value.TryGetValue("candidates",out raw)&&raw is IList) foreach(object item in (IList)raw) {
                    var candidate=item as Dictionary<string,object>;if(candidate==null)continue;string path=App.Text(candidate,"path");
                    if(!configs.Contains(path)){configs.Add(path);existing.Items.Add(App.Text(candidate,"node_id")+" · "+path);}
                }
                if(lastLog.StartsWith("/"))lastLog="\\\\wsl.localhost\\"+SelectedDistribution+lastLog.Replace('/','\\');
            } else if(File.Exists(lastLog)) { using(var stream=new FileStream(lastLog,FileMode.Open,FileAccess.Read,FileShare.ReadWrite)) {if(stream.Length>48000)stream.Seek(-48000,SeekOrigin.End);using(var reader=new StreamReader(stream)){string text=reader.ReadToEnd();log.Text=text.Length>12000?text.Substring(text.Length-12000):text;}} }
        }
        void OpenAppWindow(string url) {
            string edge=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),"Microsoft","Edge","Application","msedge.exe");
            if(!File.Exists(edge))edge=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),"Microsoft","Edge","Application","msedge.exe");
            if(!File.Exists(edge)){Process.Start(new ProcessStartInfo(url){UseShellExecute=true});return;}
            string profile=Path.Combine(App.SettingsRoot,"web-profile");
            if(browserWindowIcons==null)browserWindowIcons=new BrowserAppWindow(edge,profile);
            browserWindowIcons.Start();
            Process.Start(new ProcessStartInfo(edge,App.Arguments("--app="+url,"--user-data-dir="+profile,"--no-first-run","--no-default-browser-check")){UseShellExecute=false,CreateNoWindow=true});
        }
    }
}
