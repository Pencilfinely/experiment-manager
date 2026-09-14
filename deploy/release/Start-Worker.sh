#!/usr/bin/env bash
set -euo pipefail
package_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$package_dir"
if [[ "$(id -u)" = 0 ]]; then
  printf '%s\n' 'Run this launcher as a normal user, not with sudo. / 请用普通用户运行。'
  exit 2
fi
missing=()
command -v python3 >/dev/null 2>&1 || missing+=(python3)
command -v git >/dev/null 2>&1 || missing+=(git)
if ((${#missing[@]})); then
  printf '%s\n' 'Installing missing Python/Git packages. sudo may ask for your Linux password.'
  command -v apt-get >/dev/null 2>&1 || { printf '%s\n' 'Install Python 3.10+ and Git, then retry.'; exit 2; }
  sudo apt-get update
  sudo apt-get install -y "${missing[@]}"
fi
if ! command -v docker >/dev/null 2>&1 || ! docker info --format '{{.ServerVersion}}' >/dev/null 2>&1; then
  printf '%s\n' 'Docker is not ready / Docker 尚未就绪。' \
    'Windows: start Docker Desktop and enable WSL integration for this Ubuntu.' \
    'Ubuntu: install Docker Engine and NVIDIA Container Toolkit; allow your user to run Docker.' \
    'https://docs.docker.com/desktop/features/wsl/' \
    'https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html'
  exit 2
fi
export PYTHONPATH="$package_dir"
export PYTHONUNBUFFERED=1
exec python3 -m expman.launcher worker "$@"
