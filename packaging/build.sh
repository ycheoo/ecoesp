#!/usr/bin/env bash
#
# Build the single-file binary. Usage, from anywhere:
#
#   packaging/build.sh
#
# The result lands in dist/ at the repository root, named after the
# package.
#
# PyInstaller runs in a throwaway venv holding exactly the app's
# dependencies plus PyInstaller itself. This is not just tidiness:
# dependencies probe for optional libraries at import time (PIL, numpy,
# ...), so freezing from a developer environment silently packs
# whatever else happens to be installed there. The venv lives under
# build/, which PyInstaller uses for its own work anyway and git
# ignores. packaging/constraints.txt pins every package in it but pip,
# and needs Python 3.11 or newer.
#
# The binary only runs on a glibc at least as new as the build
# machine's. Release binaries are built only by the release workflow,
# in its digest-pinned ubuntu:22.04 image (glibc 2.35); a local build
# is for verifying the spec and for personal use.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${ROOT}/build/venv"

# --clear because `python3 -m venv` over an existing directory keeps the
# scripts already in it: a venv left behind by an earlier checkout path
# or an older interpreter survives, and its pip then dies on a bad
# interpreter line. CI never sees this; a developer machine does.
python3 -m venv --clear "${VENV}"
"${VENV}/bin/pip" install --quiet \
  -r "${ROOT}/requirements.txt" -r "${ROOT}/packaging/requirements.txt" \
  -c "${ROOT}/packaging/constraints.txt"

cd "${ROOT}"
# PyInstaller writes base_library.zip in the order its module graph
# yields, which string-hash randomization reshuffles from run to run:
# two builds of one tree on 2026-10-24 differed only in that order and
# in the ELF section offset that follows from the archive's size. With
# a fixed seed the binary is a function of the tree and of the
# directory it is built in, since the RECORD files bundled with
# markdown's and websockets' metadata hash console scripts whose first
# line names the venv's path; CI builds each repository at one path.
PYTHONHASHSEED=0 "${VENV}/bin/pyinstaller" --clean --noconfirm packaging/app.spec

echo "Built:"
ls -lh "${ROOT}/dist/"
