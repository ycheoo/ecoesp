#!/usr/bin/env bash
#
# Build the single-file binary. Usage, from anywhere:
#
#   packaging/build.sh
#
# The result lands in dist/ at the repository root, named after the package.
#
# PyInstaller runs in a throwaway venv holding exactly the app's dependencies
# plus PyInstaller itself. This is not just tidiness: dependencies probe for
# optional libraries at import time (PIL, numpy, ...), so freezing from a
# developer environment silently packs whatever else happens to be installed
# there. The venv lives under build/, which PyInstaller uses for its own work
# anyway and git ignores.
#
# The binary only runs on a glibc at least as new as the build machine's, so
# release builds belong on the oldest supported distribution (in CI); a local
# build is for verifying the spec and for personal use.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${ROOT}/build/venv"

python3 -m venv "${VENV}"
"${VENV}/bin/pip" install --quiet --upgrade pip
"${VENV}/bin/pip" install --quiet \
  -r "${ROOT}/requirements.txt" -r "${ROOT}/packaging/requirements.txt"

cd "${ROOT}"
"${VENV}/bin/pyinstaller" --clean --noconfirm packaging/app.spec

echo "Built:"
ls -lh "${ROOT}/dist/"
