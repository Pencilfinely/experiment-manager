#!/usr/bin/env bash
set -euo pipefail
package_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$package_dir"
export PYTHONPATH="$package_dir"
exec python3 -m expman.project_setup "$@"
