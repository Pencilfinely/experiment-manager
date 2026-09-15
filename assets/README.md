# 应用图标

`center.svg`、`worker.svg` 是即时设计导出的定稿母版；原始的
`@1x.png`（48 px）、`@0.5x.png`（24 px）保留作为设计交付。

`center.ico`、`worker.ico` 由母版直接生成，分别供管理端与算力端使用。
每个 ICO 包含 16、20、24、28、32、36、40、44、48、56、64、72、80、88、96、112、128、256 px，透明 PNG
预览在 `generated/`。每个尺寸直接渲染 SVG，未放大原始 PNG。

## 重新生成

需要 Node.js 和 sharp（本次生成使用的版本见 `generated/renderer.json`）。
可在项目内的临时依赖目录安装，然后在 PowerShell 执行：

```powershell
npm install --prefix .runtime/icon-tools --no-package-lock sharp@0.35.4
$env:NODE_PATH = (Resolve-Path .runtime/icon-tools/node_modules).Path
node scripts/build_icons.cjs
```

生成脚本更新 ICO、`generated/` 和 `expman/static/favicon.ico`，
保留即时设计导出的文件。
修改母版后应重新生成并一同提交；常规发布直接使用已生成的 ICO，
无需安装 Node.js 或 sharp。

## 程序接入

`scripts/build_desktop.py` 按角色把 ICO 写入 EXE 的 Windows 图标资源，
同时嵌入 `AppIcon` 供窗口和托盘读取。安装程序使用相同构建入口；
桌面和开始菜单快捷方式引用目标 EXE 的第一个图标。
管理端网页通过 `/favicon.ico` 使用同一份 Center 图标。

48×48 是 SVG 母版的设计坐标，不限制输出分辨率，无需放大原始 PNG。
桌面程序声明系统 DPI 感知，按窗口/任务栏所需 DPI 选择图标帧；
225% 缩放使用独立渲染的 36 px 小图标和 72 px 大图标。
实验台独立 Edge 窗口也绑定 Center 图标，仅匹配本应用专用浏览器配置目录。
