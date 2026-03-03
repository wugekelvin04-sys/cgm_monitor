#!/bin/bash
# CGM 血糖监控启动脚本（可双击运行）

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/venv"
PYTHON="$VENV/bin/python3"

# 如果 venv 不存在或依赖缺失，自动安装
if [ ! -f "$PYTHON" ] || ! "$PYTHON" -c "import rumps, pydexcom, keyring" 2>/dev/null; then
  echo "正在安装依赖..."
  python3.12 -m venv "$VENV"
  "$VENV/bin/pip" install -q -r "$DIR/requirements.txt"
fi

exec "$PYTHON" "$DIR/main.py"
