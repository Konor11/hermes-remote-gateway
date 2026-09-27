#!/usr/bin/env bash
# Install the SYSTEM dependencies that local-PC access needs.
#
# Why this exists: `hermes plugins install` only resolves the plugin's *Python*
# dependencies. The SSH server on this laptop and `sshpass` are system packages,
# and Hermes has no plugin hook to install them — so the plugin ships this
# script and also runs the same check automatically on
# `hermes remote pc setup` / `hermes remote connect` (local-PC mode).
#
# Safe to re-run: it checks first and only installs what is missing.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-$(command -v python3 || command -v python)}"

if [ -z "${PY}" ]; then
    echo "python3 not found — install Python first" >&2
    exit 1
fi

"${PY}" "${DIR}/deps.py" "$@"

echo
echo "Далее:"
echo "  hermes remote pc setup --server-password '<root-пароль сервера>'"
echo "  hermes remote connect"
