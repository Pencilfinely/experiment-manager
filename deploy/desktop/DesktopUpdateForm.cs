using System;
using System.Drawing;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace ExperimentManagerDesktop {
    sealed class UpdateForm : IconForm {
        readonly ClientForm client;
        readonly Label versions=new Label { AutoSize=true };
        readonly Label status=new Label { AutoSize=true, MaximumSize=new Size(560,0) };
        readonly TextBox notes=new TextBox { Multiline=true,ReadOnly=true,ScrollBars=ScrollBars.Vertical,Dock=DockStyle.Fill };
        readonly ProgressBar progress=new ProgressBar { Dock=DockStyle.Top,Height=18 };
        readonly Button check=new Button { Text="检查更新",AutoSize=true };
        readonly Button download=new Button { Text="下载更新",AutoSize=true,Enabled=false };
        readonly Button install=new Button { Text="安装更新",AutoSize=true,Enabled=false };
        readonly Button cancel=new Button { Text="取消下载",AutoSize=true,Enabled=false };
        readonly LinkLabel releaseLink=new LinkLabel { Text="查看 GitHub 发布页",AutoSize=true };
        UpdateRelease available;
        string downloaded;
        bool working,closeWhenIdle;
        CancellationTokenSource cancellation;

        internal UpdateForm(ClientForm owner) {
            client=owner; Text="软件更新 · "+App.Title;
            Size=new Size(650,520); MinimumSize=new Size(570,440);
            StartPosition=FormStartPosition.CenterParent; Font=new Font("Microsoft YaHei UI",10);
            var layout=new TableLayoutPanel { Dock=DockStyle.Fill,Padding=new Padding(24),ColumnCount=1,RowCount=7 };
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent,100));
            for(int i=0;i<4;i++) layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            versions.Text="当前版本："+App.Version;
            layout.Controls.Add(versions);
            layout.Controls.Add(new Label { Text="可先下载；安装时会检查实验和文件回传，保留已有配置与数据。",AutoSize=true,MaximumSize=new Size(560,0),Margin=new Padding(0,12,0,12) });
            layout.Controls.Add(notes); layout.Controls.Add(progress); layout.Controls.Add(status);
            var buttons=new FlowLayoutPanel { AutoSize=true,Dock=DockStyle.Fill,Margin=new Padding(0,12,0,8) };
            buttons.Controls.Add(check);buttons.Controls.Add(download);buttons.Controls.Add(install);buttons.Controls.Add(cancel);
            layout.Controls.Add(buttons);layout.Controls.Add(releaseLink);Controls.Add(layout);
            ActiveControl=check;
            check.Click+=async(s,e)=>await Check();
            download.Click+=async(s,e)=>await Download();
            install.Click+=async(s,e)=>await Install();
            cancel.Click+=(s,e)=>{if(cancellation!=null)cancellation.Cancel();};
            releaseLink.LinkClicked+=(s,e)=>System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(
                available==null?"https://github.com/Pencilfinely/experiment-manager/releases":available.ReleaseUrl){UseShellExecute=true});
            Shown+=async(s,e)=>await Check();
            FormClosing+=(s,e)=>{if(working){e.Cancel=true;closeWhenIdle=true;if(cancellation!=null)cancellation.Cancel();}};
            ResumeLayout(true);
        }

        void Working(bool value) {
            working=value; check.Enabled=!value;download.Enabled=!value&&available!=null;
            install.Enabled=!value&&available!=null&&downloaded!=null;cancel.Enabled=value&&cancellation!=null;
            if(!value&&closeWhenIdle) Close();
        }

        async Task Check() {
            if(working)return;available=null;downloaded=null;Working(true);progress.Style=ProgressBarStyle.Marquee;status.Text="正在连接 GitHub 检查更新…";
            versions.Text="当前版本："+App.Version;notes.Clear();
            try {
                var result=await Task.Run(()=>UpdateService.Check(App.Version,App.Worker));
                available=result;
                if(result!=null&&downloaded==null) {
                    string cached=Path.Combine(App.SettingsRoot,"updates",result.AssetName);
                    try {await Task.Run(()=>UpdateService.ValidateDownloaded(result,cached));downloaded=cached;}
                    catch(IOException) {} catch(InvalidOperationException) {} catch(System.Security.Cryptography.CryptographicException) {}
                }
                versions.Text="当前版本："+App.Version+(result==null?"":"    新版本："+result.Version);
                notes.Text=result==null?"当前渠道暂无可用的新版本。":result.Notes;
                status.Text=result==null?"已是当前渠道的最新版本。":downloaded==null?"发现新版本，点击“下载更新”。":"已找到下载并校验过的安装包，可以安装更新。";
            } catch(Exception ex) {status.Text="检查失败："+ex.Message;}
            finally {progress.Style=ProgressBarStyle.Blocks;progress.Value=0;Working(false);}
        }

        async Task Download() {
            if(working||available==null)return;
            downloaded=null;
            cancellation=new CancellationTokenSource();Working(true);progress.Value=0;status.Text="正在下载更新…";
            var token=cancellation.Token;
            IProgress<Tuple<long,long>> reporter=new Progress<Tuple<long,long>>(value=>{
                if(IsDisposed||!working)return;
                progress.Value=value.Item2>0?(int)Math.Min(100,value.Item1*100/value.Item2):0;
                status.Text=string.Format("正在下载：{0:0.0} / {1:0.0} MiB",value.Item1/1048576.0,value.Item2/1048576.0);
            });
            try {
                downloaded=await Task.Run(()=>UpdateService.Download(available,Path.Combine(App.SettingsRoot,"updates"),
                    (done,total)=>reporter.Report(Tuple.Create(done,total)),token));
                progress.Value=100;status.Text="下载完成，文件校验通过。可以安装，或关闭窗口稍后安装。";
            } catch(OperationCanceledException) {status.Text="下载已取消，可以重新下载。";}
            catch(Exception ex) {status.Text="下载失败："+ex.Message;}
            finally {cancellation.Dispose();cancellation=null;Working(false);}
        }

        async Task Install() {
            if(working||available==null||downloaded==null)return;
            Working(true);status.Text="正在校验安装包并检查实验状态…";
            try {
                await client.InstallUpdate(available,downloaded);
                // The client closes only after the verified installer was started.
                working=false;Close();client.FinishUpdateExit();
            } catch(Exception ex) {status.Text="暂时无法安装："+ex.Message;Working(false);}
        }

        protected override void Dispose(bool disposing) {
            if(disposing&&cancellation!=null)cancellation.Cancel();
            base.Dispose(disposing);
        }
    }
}
