#!/usr/bin/env bash
# 数据迁移工具启动脚本
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PY="${PY:-python3}"
PORT="${PORT:-8765}"
HOST="${HOST:-0.0.0.0}"

# 检查依赖
echo "==> 检查 Python 依赖"
$PY -c "import flask, pymysql" 2>/dev/null || {
    echo "    缺少依赖，正在安装 ..."
    $PY -m pip install -r requirements.txt
}

# 初始化元数据库
$PY -c "from db import init_sqlite; init_sqlite(); print('==> 元数据库初始化完成')"

echo "==> 启动数据迁移工具"
echo "    Web:  http://localhost:$PORT/"
echo "    日志: $SCRIPT_DIR/logs/migrate.log"
echo ""
exec $PY app.py
