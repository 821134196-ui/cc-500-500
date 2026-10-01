#!/usr/bin/env bash
# 一条本地命令：重置演示库（含错版案例）→ 启动教材履约台。
# 用法：
#   ./run.sh            重新生成种子数据并启动（默认 http://127.0.0.1:8000）
#   ./run.sh --keep     保留现有数据直接启动
#   ./run.sh test       只跑规则测试（版次/地址截止/补发幂等/库存竞争/退课拦截）
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"

if [ "${1:-}" = "test" ]; then
  exec python3 -m unittest tests.test_rules -v
fi

if [ "${1:-}" != "--keep" ]; then
  echo "[1/2] 生成演示数据（含 S008 错版案例）……"
  python3 seed.py
fi
echo "[2/2] 启动教材履约台：http://127.0.0.1:${PORT}"
echo "  仓库 李仓管 / 前台 王老师 / 学员 S001~S008（登录页一键切换）"
exec env PORT="$PORT" python3 server.py
