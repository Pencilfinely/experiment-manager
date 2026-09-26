import { webview } from '@kit.ArkWeb';
import { url } from '@kit.ArkTS';
import { preferences } from '@kit.ArkData';

@Entry
@Component
struct Index {
  @State address: string = '';
  @State allowHttp: boolean = false;
  @State opened: boolean = false;
  @State error: string = '';
  private origin: string = '';
  private controller: webview.WebviewController = new webview.WebviewController();
  private store: preferences.Preferences | undefined = undefined;

  aboutToAppear(): void {
    const context = this.getUIContext().getHostContext();
    if (context) {
      try {
        this.store = preferences.getPreferencesSync(context, { name: 'controller' });
        this.address = this.store.getSync('origin', '') as string;
      } catch (_) { this.error = '无法读取上次连接地址，请重新输入。'; }
    }
  }

  private privateHost(host: string): boolean {
    if (host === 'localhost') return true;
    if (!/^\d{1,3}(\.\d{1,3}){3}$/.test(host)) return false;
    const parts: number[] = host.split('.').map((part: string): number => Number(part));
    if (parts.some((part: number): boolean => part > 255)) return false;
    return parts[0] === 10 || parts[0] === 127 ||
      (parts[0] === 192 && parts[1] === 168) ||
      (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31) ||
      (parts[0] === 100 && parts[1] >= 64 && parts[1] <= 127);
  }

  private connect(): void {
    try {
      const parsed = new url.URL(this.address.trim());
      if ((parsed.protocol !== 'https:' && parsed.protocol !== 'http:') || !parsed.hostname ||
        parsed.username || parsed.password || parsed.search || parsed.hash ||
        (parsed.pathname !== '/' && parsed.pathname !== '') ||
        (parsed.port !== '' && (Number(parsed.port) < 1 || Number(parsed.port) > 65535))) {
        this.error = '请输入完整管理端地址，不包含路径、凭证或查询参数。';
        return;
      }
      if (parsed.protocol === 'http:' && (!this.allowHttp || !this.privateHost(parsed.hostname))) {
        this.error = 'HTTP 仅允许已勾选的可信私网 IPv4 地址；其他地址请使用 HTTPS。';
        return;
      }
      this.origin = parsed.origin;
      this.store?.putSync('origin', this.origin);
      this.store?.flush().catch(() => {});
      this.error = '';
      this.opened = true;
    } catch (_) { this.error = '地址格式不正确，请填写 http:// 或 https:// 开头的管理端地址。'; }
  }

  private allowed(target: string): boolean {
    try {
      const parsed = new url.URL(target);
      return parsed.origin === this.origin && !parsed.username && !parsed.password;
    } catch (_) { return false; }
  }

  onBackPress(): boolean {
    if (!this.opened) return false;
    this.controller.runJavaScript('Boolean(window.ExperimentMobileBack && window.ExperimentMobileBack())')
      .then((handled: string) => { if (handled !== 'true') this.opened = false; })
      .catch(() => { this.opened = false; });
    return true;
  }

  build() {
    Column() {
      if (!this.opened) {
        Scroll() {
          Column({ space: 18 }) {
            Text('实验台 · 移动监控').fontSize(25).fontWeight(FontWeight.Bold).fontColor('#234F41')
            Text('填写手机能够访问的管理端地址。连接后，输入管理端签发的移动凭证。').fontSize(14).fontColor('#64756D')
            TextInput({ text: this.address, placeholder: 'https://你的管理端地址' })
              .type(InputType.Normal).height(52).onChange((value: string) => { this.address = value; })
            Row({ space: 8 }) {
              Checkbox({ name: 'private-http' }).select(this.allowHttp)
                .onChange((value: boolean) => { this.allowHttp = value; })
              Text('允许连接可信私网中的 HTTP 地址').fontSize(13).layoutWeight(1)
            }.width('100%')
            if (this.error) { Text(this.error).fontColor('#9C3A34').fontSize(13) }
            Button('连接管理端').width('100%').height(48).backgroundColor('#234F41').onClick(() => { this.connect(); })
            Text('HarmonyOS 6 · Android 共用移动界面').fontSize(12).fontColor('#64756D')
          }.padding(24).width('100%').alignItems(HorizontalAlign.Start)
        }.layoutWeight(1)
      } else {
        Row({ space: 12 }) {
          Button('更换管理端').onClick(() => { this.opened = false; this.error = ''; })
          Button('重新加载').onClick(() => { this.error = ''; this.controller.loadUrl(this.origin + '/mobile/'); })
        }.padding(8).width('100%')
        Text(this.error || this.origin).fontSize(11).fontColor(this.error ? '#9C3A34' : '#64756D').padding(8)
        Web({ src: this.origin + '/mobile/', controller: this.controller })
          .javaScriptAccess(true)
          .domStorageAccess(true)
          .fileAccess(false)
          .mixedMode(MixedMode.None)
          .onLoadIntercept((event) => { return event ? !this.allowed(event.data.getRequestUrl()) : true; })
          .onErrorReceive((event) => {
            if (event && event.request.isMainFrame()) this.error = '无法连接，请检查管理端服务、网络和 HTTPS 证书，再点击重新加载。';
          })
          .onHttpErrorReceive((event) => {
            if (event && event.request.isMainFrame()) this.error = '管理端返回错误，请确认已升级到支持移动监控的版本。';
          })
          .width('100%').layoutWeight(1)
      }
    }.width('100%').height('100%').backgroundColor('#F4F6F1')
  }
}
