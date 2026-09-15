#!/usr/bin/env bash
set -euo pipefail
package_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$package_dir"
export PYTHONUNBUFFERED=1
exec python3 -m expman.worker_service start "$@"
