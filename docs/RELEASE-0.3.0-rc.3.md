# Experiment Manager 0.3.0-rc.3

## 图标清晰度与实验台窗口修复

- 修复 Windows 高 DPI 缩放下客户端窗口及托盘图标模糊：启用系统 DPI 感知，按所需尺寸选择图标。
- 从已定稿的 SVG 直接生成更多尺寸，覆盖常见缩放比例，包括 225% 的 36 px / 72 px。图标设计不变。
- 为独立实验台窗口设置 Center 图标，覆盖窗口和任务栏；仅作用于 Experiment Manager 专用 Edge 配置目录。
- 保留 rc.2 的检查更新、下载校验、安全停止和安装更新流程。

## 升级

rc.2 用户可以在 Center / Worker 的状态窗口或托盘菜单中选择“检查更新”。
rc.1 及更早版本需手动下载安装器。先升级两台机器的 Worker，再升级 Center；
安装前暂停接单，等待实验及待回传完成，沿用原数据目录、Ubuntu 和节点配置。

## English

Fixes blurry Windows client/tray icons at high display scaling and supplies the
Center icon to the dedicated experiment window. Additional icon frames are
rendered directly from the approved SVG artwork, including 36/72 px for 225%.
The rc.2 update workflow and existing application data remain compatible.
