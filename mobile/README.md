# 实验台移动监控

目标平台：Android 9+、原生 HarmonyOS 6.0（API 20）。主要鸿蒙验收机型为 **华为 Mate 70 Pro / HarmonyOS 6**。

两个轻量客户端加载管理端的同一套 `/mobile/` 网页。移动端负责连接、展示和操作确认，任务调度、状态持久化和命令执行仍由现有管理端 / 算力端负责。电脑网页可以关闭；管理端后台服务停止后，手机无法读取新状态或提交操作。

## 当前功能

- 实验概况、搜索和状态筛选，详情、累计时长、当前指标和最近回传的指标曲线。
- 节点状态、显存概况、最新日志片段、实验参数和结果文件清单。
- 独立只读 / 有限操作凭证。有限操作允许停止、取消、支持续训的实验恢复，以及节点暂停 / 恢复接单。
- 凭证有效期 1–90 天，默认 30 天。服务端只保存 SHA-256，原文创建时显示一次，可随时撤销。
- 前台每 5 秒同步，15 秒未更新则禁止操作；切回前台重新同步。超时不自动重发操作，命令版本检查阻止旧页面重复提交。

首版不包含手机新建实验、本地目录导入、完整结果下载、锁屏持续运行、后台报警推送或自动公网穿透。监控凭证可以查看参数和日志，应只交给获准查看这些内容的设备。

## 通过手机浏览器使用

1. 运行包含本次代码的管理端。旧版已安装程序不会因仓库修改自动升级。
2. 电脑管理页面进入 **设置 → 移动端访问**（`/mobile-access`）。
3. 创建设备，例如 `Mate 70 Pro`，选择权限和有效期。管理员及算力节点凭证不能用于移动页面。
4. 手机与电脑接入可互通的局域网或可信私网。手机访问 `http://管理电脑私网IP:端口/mobile/`，输入移动凭证。
5. 可主动勾选“在此设备记住凭证”，将其存入该站点的本地存储。未勾选时仅在页面会话保存；退出会清除保存的凭证。

默认端口通常为 8765，以电脑显示的实际地址为准。手机上的 localhost 指向手机自己。应用沿用现有监听和防火墙配置，不自动开放端口。

跨不可信网络使用有效 HTTPS 或可信私网连接。两个原生外壳的 HTTP 入口需主动勾选，且只接受私网 / 环回 / 100.64.0.0/10 的 IPv4 地址及开发用 localhost；其他域名用 HTTPS。暂不提供私网 IPv6 HTTP 入口。

## Android 构建

以下构建步骤在源码仓库中执行。管理端安装包只附此说明，不包含移动客户端源码、SDK 或签名材料。

打开 `mobile/android`。要求 JDK 17 或 21、Android SDK Platform 35；工程固定 Gradle 8.9、AGP 8.7.3、Kotlin 2.0.21，应用字节码目标为 Java 17。仓库提供由官方 Gradle 生成的 wrapper，首次构建自动下载并校验 Gradle 8.9 及构建依赖，无需手工安装 Gradle。仓库不内置 SDK，SDK 安装器要求接受 Android SDK 许可后才能安装平台组件。

```powershell
cd mobile/android
./build.ps1
```

也可通过 `-Gradle` 传入现有 Gradle 8.9 的 `gradle.bat` 完整路径。等价命令是 `./gradlew.bat --no-daemon :app:assembleDebug :app:lintDebug`；Linux / macOS 使用 `sh ./gradlew`。Android Studio 可直接打开工程并同步。

也可在仓库根目录显式指定工具和缓存路径，无需修改系统环境变量：

```powershell
./mobile/android/build.ps1 -Gradle 'E:/tools/gradle-8.9/bin/gradle.bat' `
  -JavaHome 'E:/tools/jdk-21' -AndroidSdk 'E:/tools/android-sdk' `
  -CacheDirectory './.runtime/gradle-cache'
```

脚本依次执行 APK 编译和 Android Lint，成功后输出 APK 的 SHA-256；退出时还原调用进程的环境变量。它不自动安装 SDK 或接受许可。

测试 APK 位于 `app/build/outputs/apk/debug/app-debug.apk`；用 `adb install -r app/build/outputs/apk/debug/app-debug.apk` 安装。正式分发须另行配置发布签名，仓库不含签名密钥。

本次已生成的交付副本：`dist/mobile/ExperimentMonitor-0.1.0-debug.apk`（相对仓库根目录），同目录有 `.sha256` 校验文件。它是 Android 调试签名测试包，尚未在 Android 真机上验收。

Mate 70 Pro / HarmonyOS 6 也可以先尝试通过卓易通安装此 APK。[华为官方说明](https://consumer.huawei.com/cn/support/content/zh-cn16061787/)支持 HarmonyOS 5 及以上接收并安装 APK，但明确要求以卓易通实际支持为准。本项目尚未验证卓易通中的安装、WebView、私网连接和前后台恢复，不能保证当前 APK 可用。如果希望不依赖兼容工具，使用下方的原生 HAP 工程；直接通过手机浏览器访问 `/mobile/` 也是独立的使用方式。

应用仅请求网络权限，关闭系统备份，并在 Android 12+ 的云备份和设备迁移规则中排除应用数据；WebView 禁用文件访问、混合内容、第三方 Cookie 和跨管理端跳转，不注入 JS 原生桥，不忽略证书错误。原生偏好仅保存管理端地址。Android 11+ 显式处理系统栏、开孔和软键盘占用区域，Android 9–10 保留兼容处理，实际布局仍需真机验收。

## HarmonyOS 6 构建

使用 **DevEco Studio 6.0 系列或支持 API 20 的更高版本**打开 `mobile/harmonyos`，安装 HarmonyOS 6.0.0（API 20）SDK。工程采用 Stage 模型、ArkTS / ArkUI / ArkWeb，兼容和目标 SDK 均为 `6.0.0(20)`。从[华为官方下载中心](https://developer.huawei.com/consumer/cn/download/command-line-tools-for-hmos)获取工具；账号登录及下载协议按官方页面要求完成。

1. 同步工程及 Hvigor 配置。工程构建模型为 `6.0.0`；如较新 IDE 提示升级构建模型，使用升级向导并保留 API 20 兼容目标。
2. 在 Project Structure → Signing Configs 中用自己的华为开发者账号配置调试签名，绑定调试设备。证书、签名配置和设备信息不提交仓库。
3. Mate 70 Pro 开启开发者模式和 USB 调试，连接电脑并在手机确认调试授权。
4. 用 IDE Run 安装运行 `entry`；也可使用 DevEco 随附工具执行 `hvigorw --mode module -p module=entry@default -p product=default assembleHap --no-daemon`。
5. HAP 通常位于 `entry/build/default/outputs/default/`。真机安装须使用对应调试签名的 HAP。

仅声明 `ohos.permission.INTERNET`，不依赖 Android APK 或兼容容器。原生界面提供管理端地址、可信 HTTP 选择、重载及返回；实验 UI 由网页提供。证书错误采用 ArkWeb 默认拒绝行为。

## 真机验收

以下步骤须在 Mate 70 Pro 鸿蒙 6 和至少一台 Android 设备执行，桌面浏览器不能替代：

1. 安装签名 HAP / 测试 APK，分别检查私网 HTTP 和有效 HTTPS。
2. 检查竖横屏、字体放大、状态栏 / 底部手势区域、软键盘避让、系统返回，以及粘贴凭证。
3. 只读凭证可看实验、曲线、日志和节点，但无操作按钮。重开应用的登录状态符合“记住凭证”选择。
4. 对专门的演示实验提交停止请求，应先待执行再变化；不支持续训的实验不显示恢复。
5. 断网、管理端关闭、切后台和锁屏返回时显示过期数据、禁用操作；恢复后刷新，不重发旧操作。
6. 撤销后手机下次请求退出；手动退出再刷新不自动登录。两端并发操作拒绝旧命令。
7. 无效证书、跨站跳转不能继续加载，文件 URL 不能作为管理端地址。

## 验证边界

共用界面具备 Python HTTP 集成测试、JavaScript 逻辑测试和手机宽度浏览器验证。**Android 测试 APK 已生成并通过编译、Lint 和签名验证；鸿蒙工程尚未编译，签名 HAP 未生成，Android 和 Mate 70 Pro 真机验收均未执行。** 编译结果不代表真机兼容性验收完成。

2026-09-26 构建验证：

- 经用户同意接受 Android SDK 许可，安装 SDK Platform 35，使用 Microsoft OpenJDK 21.0.12.1、Gradle 8.9、AGP 8.7.3 完成 `:app:assembleDebug :app:lintDebug`；Lint 报告 `No issues found`。编译器仍提示一个系统窗口 API 的弃用警告，该调用用于旧版系统兼容。
- `apksigner verify --verbose` 校验通过（APK v2 签名）；`aapt dump badging` 核实版本 `0.1.0`、包名 `com.pencilfinely.expmonitor`、最低 API 28、目标 API 35，仅声明 `INTERNET` 权限。
- 交付 APK 大小 2,394,418 字节，SHA-256：`4c56041a4f3d9b973b4de00f59d40a8ea6fcbd8f046fa4ba742a099c48edd61f`。
- 工具均存放在忽略提交的 `.runtime/mobile-toolchains/`，未修改系统安装。本机尚无 DevEco / HarmonyOS SDK，需从官方入口下载后继续鸿蒙编译和设备签名。
- 当前 Windows 环境的 JDK 本地通信需要给构建进程指定 `-Djdk.net.unixdomain.tmpdir=项目内可写短路径`；该设置只用于此次本机构建，不影响客户端。

2026-09-25 本地验证记录：

- Python 回归共 411 项：404 项通过，7 项按环境条件跳过。其中两个已有 SASRec 打包用例最初受沙箱临时目录 ACL 限制，获准访问临时目录后单独重跑通过。
- 新增 9 项真实 HTTP 集成测试，覆盖角色隔离、只读限制、哈希存储、重启后有效性、过期撤销、命令版本冲突、原有操作语义、无效输入和发布包资源。
- 新增 JavaScript 权限 / 过期状态 / 指标历史测试通过；已有计时、矩阵和资源 UI 测试通过。文档进入发布包后再跑 19 项发布相关测试通过。
- 在本机隔离的合成数据服务中，以 412px 和 360px 视口检查登录、总览、搜索、指标与日志、停止请求待确认、只读无操作入口、管理员签发和隐藏凭证。经授权撤销模拟凭证后，手机退出并隐藏数据；停止 / 重启演示服务后，页面禁用操作、标记旧数据，并自动恢复。
- 当日原生工程只完成 JSON / XML 配置解析检查；后续 Android 编译结果见上方 2026-09-26 记录。鸿蒙工程仍只完成配置解析，不能替代 ArkTS 编译和设备测试。

实现参考：[Android WebView](https://developer.android.com/develop/ui/views/layout/webapps/webview)、[AGP 8.7](https://developer.android.com/build/releases/agp-8-7-0-release-notes)、[ArkWeb 加载页面](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/web-page-loading-with-web-components)、[鸿蒙调试签名](https://developer.huawei.com/consumer/cn/doc/HarmonyOS-Guides/ide-signing-auto)。
