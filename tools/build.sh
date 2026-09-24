#!/bin/bash
# macOS / Linux 构建脚本
set -e
cd "$(dirname "$0")/.."
echo
echo "==============================================="
echo "  hrcloud-migrate macOS/Linux 打包"
echo "==============================================="
echo
python3 tools/build.py "$@"