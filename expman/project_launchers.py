"""Text templates included in exported project ZIPs, not worker launch entries."""

FILES = {'Install-Project.cmd': '@echo off\n'
                        'powershell.exe -NoProfile -ExecutionPolicy Bypass -File '
                        '"%~dp0Install-Project.ps1"\n'
                        'pause\n',
 'Install-Project.ps1': "$ErrorActionPreference = 'Stop'\n"
                        '[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()\n'
                        '$inventory = & wsl.exe --list --quiet\n'
                        "if ($LASTEXITCODE -ne 0) { throw 'WSL is not ready. Install and configure the "
                        "worker first.' }\n"
                        '$distributions = @($inventory | ForEach-Object { ($_ -replace "`0", '
                        "'').Trim() } | Where-Object { $_ -and $_ -notmatch '^docker-desktop' })\n"
                        "if ($distributions.Count -eq 0) { throw 'No WSL distribution. Install and "
                        "configure the worker first.' }\n"
                        'if ($distributions.Count -eq 1) { $distribution = $distributions[0] }\n'
                        'else {\n'
                        '    for ($index = 0; $index -lt $distributions.Count; $index++) { Write-Host '
                        '"[$index] $($distributions[$index])" }\n'
                        "    $choice = Read-Host 'Choose the same WSL distribution as your worker'\n"
                        '    $number = 0\n'
                        '    if (-not [int]::TryParse($choice, [ref]$number) -or $number -lt 0 -or '
                        "$number -ge $distributions.Count) { throw 'Invalid distribution number.' }\n"
                        '    $distribution = $distributions[$number]\n'
                        '}\n'
                        '$packagePath = & wsl.exe -d $distribution --exec wslpath -a $PSScriptRoot\n'
                        "if ($LASTEXITCODE -ne 0) { throw 'Extract the entire project ZIP to a local "
                        "disk first.' }\n"
                        '& wsl.exe -d $distribution --exec python3 ($packagePath.Trim() + '
                        "'/Install-Project.pyz') install\n"
                        'exit $LASTEXITCODE\n',
 'Install-Project.sh': '#!/usr/bin/env bash\n'
                       'set -euo pipefail\n'
                       'package_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"\n'
                       'exec python3 "$package_dir/Install-Project.pyz" install "$@"\n',
 'READ-ME.txt': 'SASRec project transfer / SASRec 项目转移包\n'
                '\n'
                'This ZIP contains the selected Python source, research configuration and three\n'
                'dataset splits. It does not contain a worker identity or Docker image.\n'
                '本包包含所选算法源码、科研配置、train/valid/test 数据，不含节点令牌或 Docker 镜像。\n'
                '\n'
                '1. Copy this ZIP to the target computer and extract ALL files into one folder.\n'
                '   把这个 ZIP 传到目标电脑，完整解压到一个文件夹。\n'
                '2. The target worker must already be installed, paired and have a verified runtime.\n'
                '   目标电脑必须先装好算力端、完成配对和 GPU 环境验收；打开 Docker。\n'
                "3. Wait for that worker's jobs/uploads to finish, then close its worker window.\n"
                '   等该节点任务完成、待回传为 0，然后退出代理窗口。\n'
                "4. Windows: double-click Install-Project.cmd. Choose the worker's WSL distribution\n"
                '   and existing config if asked. The directory listing uses full paths.\n'
                '   Windows：双击 Install-Project.cmd；如有多套环境，选择代理使用的 Ubuntu 和配置。\n'
                '   Ubuntu: in this extracted folder, run bash Install-Project.sh.\n'
                '5. Wait for INSTALLED, then restart your usual worker launcher with its usual config.\n'
                '   出现 INSTALLED 后，按原来的方式、使用原来的配置启动该算力端。\n'
                '6. Refresh the controller. Under THIS node, choose the short template and submit\n'
                '   with parameter grid {}. After it succeeds, use the formal template.\n'
                '   刷新实验台，在这个节点下选择 short 模板，参数组合保持 {}，提交到队列。\n'
                '   短跑成功后再使用 formal 模板。安装包本身不会提交训练。\n'
                '\n'
                'Legacy/custom config (Ubuntu terminal in the extracted folder):\n'
                'python3 Install-Project.pyz install --node-config /absolute/path/to/node.ready.json\n'
                'Use the file following --config in the command that starts YOUR worker.\n'
                '旧版或自定义安装：--node-config 后填当前代理启动命令中 --config 后面的完整路径。\n'
                '\n'
                'Changing code or data on the source computer does not update this package.\n'
                'Export a new package, transfer it and install again. Existing tasks retain their\n'
                'original snapshot. Code/data/image must exist on every target node independently.\n'
                '修改原项目后需要重新导出、传输、安装；已有实验保留旧版本。不是实时自动同步。\n'
                'GPU compatibility is checked by a short run on each target; this package does not\n'
                'install extra algorithm dependencies or provide multi-GPU training automatically.\n'
                '每台目标机都先短跑。包不会自动补装额外依赖，也不会把单卡程序变成多卡程序。\n'}
