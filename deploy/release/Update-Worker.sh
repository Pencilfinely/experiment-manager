#!/usr/bin/env bash
set -euo pipefail
package_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$package_dir"
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'Python 3.10+ is required / 请先安装 Python 3.10 或以上。'; exit 2; }
export PYTHONPATH="$package_dir"
export PYTHONUNBUFFERED=1
exec python3 -m expman.worker_upgrade "$@"
