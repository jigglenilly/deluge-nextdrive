#!/usr/bin/env sh
# Build the NextDrive .egg. Deluge only loads eggs built for its own Python
# version, so run this with the same Python minor version as your Deluge
# (check with: deluged --version).
#   ./build.sh            # uses python3
#   PYTHON=python3.12 ./build.sh
set -eu
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
rm -rf build ./*.egg-info
"$PYTHON" setup.py -q bdist_egg --dist-dir dist
rm -rf build ./*.egg-info
ls -1 dist/*.egg
