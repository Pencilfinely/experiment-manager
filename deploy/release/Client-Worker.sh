#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
export PYTHONPATH="$PWD"
export PYTHONUNBUFFERED=1
exec python3 -m expman.worker_service "$@"
