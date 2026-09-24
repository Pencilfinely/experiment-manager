using System;
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.Threading.Tasks;
using System.Windows.Forms;

namespace ExperimentManagerDesktop {
    sealed class WorkerGpuForm : IconForm {
        readonly Func<string,string[],Task<Dictionary<string,object>>> command;
        readonly ListView cards=new ListView {Dock=DockStyle.Fill,View=View.Details,FullRowSelect=true,MultiSelect=false,HideSelection=false};
        readonly Label feedback=new Label {AutoSize=true,MaximumSize=new Size(760,0),Text="正在读取本机显卡…"};
        readonly Button enable=new Button {Text="检查并启用所选显卡",AutoSize=true,Enabled=false};
        readonly Button refresh=new Button {Text="刷新",AutoSize=true};
        bool busy;

        internal WorkerGpuForm(Func<string,string[],Task<Dictionary<string,object>>> command) {
            this.command=command;
            Text="显卡设置 · Experiment Worker";Size=new Size(860,500);MinimumSize=new Size(720,420);
            StartPosition=FormStartPosition.CenterParent;Font=new Font("Microsoft YaHei UI",10);BackColor=Color.White;
            var layout=new TableLayoutPanel {Dock=DockStyle.Fill,Padding=new Padding(20),ColumnCount=1,RowCount=5};
            layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,100));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent,100));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.Controls.Add(new Label {Text="选择一张显卡，检查通过后启用",AutoSize=true,Font=new Font(Font.FontFamily,15,FontStyle.Bold),Margin=new Padding(0,0,0,14)});
            cards.Columns.Add("显卡",235);cards.Columns.Add("UUID",350);cards.Columns.Add("本地状态",175);
            layout.Controls.Add(cards);
            var actions=new FlowLayoutPanel {Dock=DockStyle.Fill,AutoSize=true,Margin=new Padding(0,10,0,6)};
            actions.Controls.Add(enable);actions.Controls.Add(refresh);layout.Controls.Add(actions);
            layout.Controls.Add(feedback);
            layout.Controls.Add(new Label {AutoSize=true,MaximumSize=new Size(760,0),Margin=new Padding(0,12,0,0),
                Text="使用现有训练环境检查显卡。请等待本机实验与回传完成；运行中的空闲代理会自动暂停并恢复。启用后，在实验台设置每卡并发、节点并发和资源预算。"});
            Controls.Add(layout);
            cards.SelectedIndexChanged+=(s,e)=>UpdateSelection();
            refresh.Click+=async(s,e)=>await LoadCards();
            enable.Click+=async(s,e)=>await EnableSelected();
            Shown+=async(s,e)=>await LoadCards();
            FormClosing+=(s,e)=>{if(busy)e.Cancel=true;};
            ResumeLayout(true);
        }

        internal static bool CanEnable(Dictionary<string,object> gpu) {
            return gpu!=null&&!App.Flag(gpu,"local_enabled")&&App.Flag(gpu,"can_enable")&&!String.IsNullOrWhiteSpace(App.Text(gpu,"uuid"));
        }
        Dictionary<string,object> Selected {get{return cards.SelectedItems.Count==1?cards.SelectedItems[0].Tag as Dictionary<string,object>:null;}}
        void UpdateSelection(){enable.Enabled=!busy&&CanEnable(Selected);}
        void SetBusy(bool value){busy=value;cards.Enabled=!value;refresh.Enabled=!value;UpdateSelection();UseWaitCursor=value;}
        async Task ReadCards() {
            var result=await command("gpu-status",new string[0]);
            object rows;
            if(!result.TryGetValue("gpus",out rows)||!(rows is IList))throw new Exception(App.Text(result,"detail","无法读取显卡列表。"));
            string selected=Selected==null?"":App.Text(Selected,"uuid");
            cards.Items.Clear();
            foreach(object raw in (IList)rows) {
                var gpu=raw as Dictionary<string,object>;if(gpu==null)continue;
                var item=new ListViewItem(new[]{App.Text(gpu,"name"),App.Text(gpu,"uuid"),App.Text(gpu,"reason")}){Tag=gpu};
                cards.Items.Add(item);if(App.Text(gpu,"uuid")==selected)item.Selected=true;
            }
            if(cards.SelectedItems.Count==0&&cards.Items.Count>0)cards.Items[0].Selected=true;
        }
        async Task LoadCards() {
            if(busy)return;SetBusy(true);
            try {await ReadCards();feedback.Text=cards.Items.Count==0?"未发现 NVIDIA 显卡，请检查驱动和 Docker Desktop。":"已启用的显卡可在实验台调整并发；未启用的显卡可在这里检查并启用。";}
            catch(Exception ex){feedback.Text=ex.Message;}
            finally{SetBusy(false);}
        }
        async Task EnableSelected() {
            var gpu=Selected;if(busy||!CanEnable(gpu))return;
            string identity=App.Text(gpu,"uuid");SetBusy(true);
            feedback.Text="正在检查空闲状态、验证 CUDA 并保存设置，请稍候…";
            try {
                var result=await command("gpu-enable",new[]{"--gpu",identity});
                feedback.Text=App.Text(result,"detail");
                try{await ReadCards();}catch(Exception ex){feedback.Text+=" 刷新列表失败："+ex.Message;}
            }catch(Exception ex){feedback.Text=ex.Message;}
            finally{SetBusy(false);}
        }
    }
}
