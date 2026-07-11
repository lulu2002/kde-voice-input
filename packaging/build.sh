#!/usr/bin/env bash
# 用 nfpm 同時產出 RPM 與 DEB:packaging/build.sh <version>
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

VERSION="${1:?usage: packaging/build.sh <version>}"
export VERSION

mkdir -p dist
nfpm package -f nfpm.yaml -p rpm -t dist/
nfpm package -f nfpm.yaml -p deb -t dist/
ls -lh dist/
